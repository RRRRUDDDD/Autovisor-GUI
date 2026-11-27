# encoding=utf-8
"""纯 HTTP 易盾验证码协议与求解封装（零 Playwright 依赖）。

移植自 ``zhs`` 的易盾自定义加密原语与三段式 JSONP 协议（``getconf`` → ``get`` → ``check``），
并直接复用 Phase 2 已实现的滑块 OpenCV 缺口识别（``modules.slider._match_yidun_gap``）
与 VLM 点选坐标解析（``modules.yidun_click.request_click_point``），避免重复代码。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import random
import re
from typing import Any, Literal
from collections.abc import Callable
import urllib.parse

import httpx

from modules import slider as slider_mod
from modules import yidun_click
from modules.captcha_common import decode_image_bytes
from modules.http_utils import DEFAULT_BROWSER_USER_AGENT, normalize_proxy_url
from modules.logger import Logger
from modules.redaction import register_secret
from modules.yidun_fp import get_session_fp, is_valid_fp, normalize_fp

CaptchaKind = Literal["slide", "click"]

# ---------------------------------------------------------------- 加密原语常量

BASE64_ALPHABET = "i/x1XgU0z7k8N+lCpOnPrv6\\qu2Gj9HRcwTYZ4bfSJBhaWstAeoMIEQ5mDdVFLKy"
BASE64_PADDING = "3"

PRIVATE_B64_ALPHABET = "MB.CfHUzEeJpsuGkgNwhqiSaI4Fd9L6jYKZAxn1/Vml0c5rbXRP+8tD3QTO2vWyo"
PRIVATE_B64_PADDING = "7"

_SEED_KEY = "fd6a43ae25f74398b61c03c83be37449"
_ROUND_KEY = "037606da0296055c"

_SBOX = (
    "a7be3f3933fa8c5fcf86c4b6908b569ba1e26c1a6d7cfbf60ae4b00e074a194dac4b73e7f8985411"
    "59a39d08183b76eedee3ed341e6685d2357440158394b1ff03a9004cbbb5ca7dcb7f41489a16e03d"
    "cc9c71eb3c9796685b1d01b4d56193a6e1f1a2470445c191ae49c5d82765dc82c350f263387a24a5"
    "02fcbf442e2dddaad0e936d9ea22b89275307b42518fbc3a626ba806d4ecd6d725f50cc8c72fefa4"
    "551ccd6fc9b2b7ab954f815c7264c6e51f4eaf99885a79892b1b60a0b3526e57ba5d178d3709588"
    "47eb9fd28f9ce0bc023f4148a2adfe632126769057043d3bd8eda0df7872629f3809ef05310e8311"
    "3216afe202c460fc23e789f77d1addb5e"
)

_B64_GROUP = 64
SAMPLE_NUM = 50
_CB_POS = (1, 10, 12, 13, 26, 31)
_CB_CODE = "vfnv46"
_UUID_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

# ---------------------------------------------------------------- 易盾协议常量

CAPTCHA_SLIDE = "75f9f716460a422f89a628f50fd8cc2b"
CAPTCHA_CLICK = "4da3050565514a35a50541b0e1f54538"

ZONE_ID = "CN31"
SDK_VERSION = "2.28.5"
LOAD_VERSION = "2.5.0"
RUN_ENV = "10"
BASE_URL = "https://c.dun.163.com"

SLIDE_WIDTH = 400
CLICK_WIDTH = 320
SLIDE_TYPE = 2
CLICK_TYPE = 11

DEFAULT_ZHIDAO_REFERER = "https://onlineexamh5new.zhihuishu.com/stuExamWeb.html"

logger = Logger()


# ---------------------------------------------------------------- 加密与编码


def _to_byte(value: int) -> int:
    value &= 0xFF
    return value - 256 if value > 127 else value


def _hex_to_byte(text: str) -> int:
    return _to_byte(int(text, 16))


def _int_to_bytes(value: int) -> list[int]:
    return [_to_byte(value >> shift) for shift in (24, 16, 8, 0)]


def _string_to_bytes(text: str) -> list[int]:
    encoded = urllib.parse.quote(text, safe="")
    result: list[int] = []
    index = 0
    while index < len(encoded):
        if encoded[index] == "%" and index + 2 < len(encoded):
            result.append(_hex_to_byte(encoded[index + 1 : index + 3]))
            index += 3
        else:
            result.append(_to_byte(ord(encoded[index])))
            index += 1
    return result


def _bytes_to_hex(values: list[int]) -> str:
    return "".join(f"{value & 0xFF:02x}" for value in values)


def _xor_byte(first: int, second: int) -> int:
    return _to_byte(_to_byte(first) ^ _to_byte(second))


def _xors(data: list[int], key: list[int]) -> list[int]:
    if not key:
        return data[:]
    return [_xor_byte(value, key[index % len(key)]) for index, value in enumerate(data)]


def _shift_add(first: int, second: int) -> int:
    return _to_byte(first + second)


def _shifts(data: list[int], key: list[int]) -> list[int]:
    if not key:
        return data[:]
    return [_shift_add(value, key[index % len(key)]) for index, value in enumerate(data)]


def _padding_zero(length: int) -> list[int]:
    return [0] * length


def _copy_to(
    source: list[int],
    source_offset: int,
    target: list[int],
    target_offset: int,
    count: int,
) -> None:
    for index in range(count):
        if source_offset + index < len(source):
            target[target_offset + index] = source[source_offset + index]


def _expand_key(key_bytes: list[int]) -> list[int]:
    if not key_bytes:
        return _padding_zero(_B64_GROUP)
    if len(key_bytes) >= _B64_GROUP:
        return key_bytes[:_B64_GROUP]
    return [key_bytes[index % len(key_bytes)] for index in range(_B64_GROUP)]


def _b64_encode_3to4(chunk: list[int], alphabet: str, padding: str) -> str:
    length = len(chunk)
    b0 = chunk[0]
    b1 = chunk[1] if length > 1 else 0
    b2 = chunk[2] if length > 2 else 0
    if length == 1:
        return (
            alphabet[(b0 >> 2) & 0x3F]
            + alphabet[((b0 << 4) & 0x30) + ((b1 >> 4) & 0xF)]
            + padding
            + padding
        )
    if length == 2:
        return (
            alphabet[(b0 >> 2) & 0x3F]
            + alphabet[((b0 << 4) & 0x30) + ((b1 >> 4) & 0xF)]
            + alphabet[((b1 << 2) & 0x3C) + ((b2 >> 6) & 0x3)]
            + padding
        )
    return (
        alphabet[(b0 >> 2) & 0x3F]
        + alphabet[((b0 << 4) & 0x30) + ((b1 >> 4) & 0xF)]
        + alphabet[((b1 << 2) & 0x3C) + ((b2 >> 6) & 0x3)]
        + alphabet[b2 & 0x3F]
    )


def _b64_decode_4to3(indices: list[int]) -> list[int]:
    length = len(indices)
    result: list[int] = []
    if length >= 2:
        result.append(_to_byte(((indices[0] << 2) & 0xFF) + ((indices[1] >> 4) & 0x3)))
    if length >= 3:
        result.append(_to_byte(((indices[1] << 4) & 0xFF) + ((indices[2] >> 2) & 0xF)))
    if length >= 4:
        result.append(_to_byte(((indices[2] << 6) & 0xFF) + (indices[3] & 0x3F)))
    return result


def _base64_encode_core(values: list[int], alphabet: str, padding: str) -> str:
    if not values:
        return ""
    unsigned = [value & 0xFF for value in values]
    parts: list[str] = []
    index = 0
    while index < len(unsigned):
        parts.append(_b64_encode_3to4(unsigned[index : index + 3], alphabet, padding))
        index += 3
    return "".join(parts)


def _base64_decode_core(value: str, alphabet: str, padding: str) -> list[int]:
    padding_index = value.find(padding)
    characters = value[:padding_index] if padding_index != -1 else value
    indices = [alphabet.index(character) for character in characters]
    result: list[int] = []
    index = 0
    while index < len(indices):
        result.extend(_b64_decode_4to3(indices[index : index + 4]))
        index += 4
    return result


def base64_encode(values: list[int]) -> str:
    return _base64_encode_core(values, BASE64_ALPHABET, BASE64_PADDING)


def base64_encode_private(values: list[int]) -> str:
    return _base64_encode_core(values, PRIVATE_B64_ALPHABET, PRIVATE_B64_PADDING)


def base64_decode(value: str) -> list[int]:
    return _base64_decode_core(value, BASE64_ALPHABET, BASE64_PADDING)


def base64_decode_private(value: str) -> list[int]:
    return _base64_decode_core(value, PRIVATE_B64_ALPHABET, PRIVATE_B64_PADDING)


_CRC32_TABLE: list[int] = []
for _index in range(256):
    _crc = _index
    for _ in range(8):
        _crc = (_crc >> 1) ^ 0xEDB88320 if _crc & 1 else _crc >> 1
    _CRC32_TABLE.append(_crc)


def gen_crc32(values: list[int]) -> str:
    """返回 8 位十六进制 CRC32 字符串。"""
    crc = 0xFFFFFFFF
    for value in values:
        crc = (crc >> 8) ^ _CRC32_TABLE[(crc ^ (value & 0xFF)) & 0xFF]
    return _bytes_to_hex(_int_to_bytes((crc ^ 0xFFFFFFFF) & 0xFFFFFFFF))


_SBOX_BYTES = [
    _to_byte(int(_SBOX[index : index + 2], 16)) for index in range(0, len(_SBOX), 2)
]


def _sub_bytes(block: list[int]) -> list[int]:
    return [
        _to_byte(_SBOX_BYTES[16 * ((value >> 4) & 0xF) + (value & 0xF)])
        for value in block
    ]


def _apply_round_key(block: list[int]) -> list[int]:
    round_key = _ROUND_KEY
    index = 0
    while index < len(round_key):
        operation = _hex_to_byte(round_key[index : index + 2])
        argument = _hex_to_byte(round_key[index + 2 : index + 4])
        if operation == 1:
            block = [_xor_byte(value, argument) for value in block] if block else []
        elif operation == 2:
            block = [_shift_add(value, argument) for value in block] if block else []
        elif operation in (3, 4, 5, 6):
            updated: list[int] = []
            for value in block:
                if operation == 3:
                    updated.append(_xor_byte(value, argument))
                    argument = _to_byte(argument + 1)
                elif operation == 4:
                    updated.append(_shift_add(value, argument))
                    argument = _to_byte(argument + 1)
                elif operation == 5:
                    updated.append(_xor_byte(value, argument))
                    argument = _to_byte(argument - 1)
                else:
                    updated.append(_shift_add(value, argument))
                    argument = _to_byte(argument - 1)
            block = updated
        index += 4
    return block


def _pad_pkcs7(data: list[int]) -> list[int]:
    if not data:
        return _padding_zero(_B64_GROUP)
    data_length = len(data)
    remainder = data_length % _B64_GROUP
    padding_length = _B64_GROUP - remainder - 4 if remainder <= 60 else 128 - remainder - 4
    padded = [0] * (data_length + padding_length + 4)
    _copy_to(data, 0, padded, 0, data_length)
    _copy_to(_int_to_bytes(data_length), 0, padded, data_length + padding_length, 4)
    return padded


def _blocks_from_bytes(values: list[int]) -> list[list[int]]:
    if len(values) % _B64_GROUP != 0:
        return []
    count = len(values) // _B64_GROUP
    return [values[index * _B64_GROUP : (index + 1) * _B64_GROUP] for index in range(count)]


def _generate_iv() -> tuple[list[int], list[int]]:
    seed_bytes = _expand_key(_string_to_bytes(_SEED_KEY))
    random_bytes = [_to_byte(random.randint(0, 255)) for _ in range(4)]
    seed_bytes = _xors(seed_bytes, _expand_key(random_bytes))
    return _expand_key(seed_bytes), random_bytes


def _aes_core(text: str, *, with_crc: bool) -> str:
    data_bytes = _string_to_bytes(text)
    iv, raw_iv = _generate_iv()
    combined = (
        data_bytes + _string_to_bytes(gen_crc32(data_bytes)) if with_crc else data_bytes
    )
    blocks = _blocks_from_bytes(_pad_pkcs7(combined))
    result = raw_iv[:] + [0] * (len(blocks) * _B64_GROUP)
    previous = iv[:]
    for index, block in enumerate(blocks):
        block = _xors(_apply_round_key(block), iv)
        block = _shifts(block, previous)
        block = _xors(block, previous)
        previous = _sub_bytes(_sub_bytes(block))
        _copy_to(previous, 0, result, _B64_GROUP * index + 4, _B64_GROUP)
    return base64_encode_private(result)


def aes(text: str) -> str:
    """滑块使用的自定义加密：明文后拼接 crc32。"""
    return _aes_core(text, with_crc=True)


def aes_click(text: str) -> str:
    """点选使用的自定义加密：不拼接 crc32。"""
    return _aes_core(text, with_crc=False)


def xor_encode(key: str, text: str) -> str:
    """异或后用易盾标准 base64 变体字母表编码。"""
    return base64_encode(_xors(_string_to_bytes(text), _string_to_bytes(key)))


def generate_uuid(length: int = 32) -> str:
    return "".join(random.choice(_UUID_CHARS) for _ in range(length))


def generate_cb() -> str:
    """生成易盾请求参数 ``cb``。"""
    characters = list(generate_uuid(32))
    for index, position in enumerate(_CB_POS):
        if position < len(characters):
            characters[position] = _CB_CODE[index]
    return aes("".join(characters))


def with_crc(text: str) -> str:
    """在 ``xor_encode`` 结果后追加 8 位 crc32（点选 p/ext 使用）。"""
    return text + gen_crc32(_string_to_bytes(text))


# ---------------------------------------------------------------- 滑块/点选载荷


def _slide_cv_available() -> bool:
    try:
        import cv2  # noqa: F401
        import numpy  # noqa: F401

        return True
    except ImportError:
        return False


def find_slide_gap_x(background_bytes: bytes, front_bytes: bytes) -> tuple[float, int] | None:
    """复用 Phase 2 ``slider._match_yidun_gap`` 返回 ``(gap_x, bg_width)``。"""
    if not background_bytes or not front_bytes:
        return None
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None

    background = decode_image_bytes(background_bytes, np, cv2, unchanged=False)
    front = decode_image_bytes(front_bytes, np, cv2, unchanged=True)
    if background is None or front is None or getattr(background, "shape", (0, 0))[1] <= 0:
        return None
    _, max_loc = slider_mod._match_yidun_gap(background, front, cv2, np)
    return float(max_loc[0]), int(background.shape[1])


def build_slide_track(distance: float) -> list[tuple[float, float, int, int]]:
    """生成易盾纯 HTTP 滑块四段轨迹 ``[(x, y, elapsed_ms, 1), ...]``。"""
    distance = max(0.0, float(distance))
    points: list[tuple[float, float, int, int]] = []
    current = 0.0
    elapsed = 0
    middle = distance * random.uniform(0.50, 0.60)

    while current < middle:
        acceleration = 2.0 + random.uniform(0, 3.0) * (current / max(middle, 1))
        current = min(middle, current + acceleration + random.uniform(0, 1))
        y = random.gauss(0, 0.8) + math.sin(current / 25) * 2
        elapsed += random.randint(6, 12)
        points.append((current, y, elapsed, 1))

    fast = distance * random.uniform(0.88, 0.94)
    while current < fast:
        velocity = 4.0 + random.uniform(0, 4) * (
            1 - (current - middle) / max(fast - middle, 1)
        )
        current = min(fast, current + velocity + random.uniform(-0.3, 0.8))
        y = random.gauss(0, 0.6) + math.sin(current / 18) * 1.2
        elapsed += random.randint(7, 14)
        points.append((current, y, elapsed, 1))

    while current < distance - 3:
        velocity = max(0.3, 2.5 * (distance - current) / max(distance, 1))
        current = min(distance - 3, current + velocity + random.uniform(-0.3, 0.2))
        y = random.gauss(0, 0.5) + math.sin(current / 12) * 1.0
        elapsed += random.randint(15, 30)
        points.append((current, y, elapsed, 1))

    while current < distance:
        current = min(distance, current + random.uniform(0.2, 0.5))
        elapsed += random.randint(20, 45)
        points.append((current, random.gauss(0, 0.3), elapsed, 1))

    if not points or points[-1][0] != distance:
        elapsed += random.randint(5, 10)
        points.append((distance, 0.0, elapsed, 1))
    return points


def _sample_trace(values: list[str], target_length: int) -> list[str]:
    if len(values) <= target_length:
        return values
    result: list[str] = []
    for index, value in enumerate(values):
        if index >= (len(result) * (len(values) - 1)) / (target_length - 1):
            result.append(value)
    return result


def build_slide_data(
    token: str,
    trace: list[tuple[float, float, int, int]],
    distance: float,
    width: int,
) -> dict[str, Any]:
    """构造易盾滑块 ``check`` 载荷。"""
    trace_data = [
        xor_encode(token, f"{round(x)},{round(y)},{elapsed},1")
        for x, y, elapsed, _ in trace
    ]
    timestamps = sorted({elapsed for _, _, elapsed, _ in trace})
    position_percent = int(distance) / width * 100
    return {
        "d": aes(":".join(_sample_trace(trace_data, SAMPLE_NUM))),
        "m": "",
        "p": aes(xor_encode(token, str(position_percent))),
        "f": aes(xor_encode(token, ",".join(str(value) for value in timestamps))),
        "ext": aes(xor_encode(token, f"1,{len(trace_data)}")),
    }


def to_click_css(
    x: float,
    y: float,
    image_width: int,
    css_width: int = CLICK_WIDTH,
) -> tuple[int, int]:
    """将原图像素坐标等比换算为点选画布 CSS 坐标。"""
    if image_width <= 0:
        raise ValueError("图片宽度非法")
    scale = css_width / image_width
    return round(x * scale), round(y * scale)


def build_click_data(token: str, x: int, y: int) -> dict[str, Any]:
    """构造易盾点选 ``check`` 载荷（单点题：``t=0``、``ext=1,0``）。"""
    position = with_crc(xor_encode(token, f"{x},{y},0"))
    extent = with_crc(xor_encode(token, "1,0"))
    return {
        "d": "",
        "m": aes_click("00000000"),
        "p": aes_click(position),
        "ext": aes_click(extent),
    }


# ---------------------------------------------------------------- JSONP 协议层


def create_client(
    *,
    proxy: str = "",
    timeout: float = 20.0,
    user_agent: str = "",
) -> httpx.Client:
    """易盾独立客户端：不携带智慧树 Cookie，仅复用代理设置。"""
    normalized_proxy = normalize_proxy_url(proxy) or None
    return httpx.Client(
        proxy=normalized_proxy,
        timeout=timeout,
        headers={
            "User-Agent": user_agent or DEFAULT_BROWSER_USER_AGENT,
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        },
    )


def parse_jsonp(text: str) -> dict[str, Any]:
    """剥掉 ``__JSONP_xxx({...});`` 外壳。"""
    match = re.search(r"\((\{.*\})\)", str(text or ""), re.S)
    if not match:
        raise ValueError("易盾响应不是合法 JSONP")
    parsed = json.loads(match.group(1))
    if not isinstance(parsed, dict):
        raise ValueError("易盾 JSONP 顶层不是对象")
    return parsed


def _callback(index: int = 0) -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    return "__JSONP_" + "".join(random.choice(alphabet) for _ in range(7)) + f"_{index}"


def _get(
    client: httpx.Client,
    path: str,
    params: dict[str, Any],
    referer: str,
) -> dict[str, Any]:
    payload = {"referer": referer}
    payload.update({key: value for key, value in params.items() if value is not None})
    response = client.get(f"{BASE_URL}{path}", params=payload)
    response.raise_for_status()
    result = parse_jsonp(response.text)
    logger.debug(f"易盾 {path} -> error={result.get('error')} msg={result.get('msg')}")
    return result


def getconf(client: httpx.Client, captcha_id: str, referer: str) -> dict[str, Any]:
    """第一步：获取 ``dt`` / ``irToken``。"""
    return _get(
        client,
        "/api/v2/getconf",
        {
            "zoneId": "",
            "dt": "",
            "id": captcha_id,
            "ipv6": "false",
            "runEnv": RUN_ENV,
            "iv": "5",
            "loadVersion": LOAD_VERSION,
            "lang": "zh-CN",
            "callback": _callback(0),
        },
        referer,
    )


def fetch(
    client: httpx.Client,
    captcha_id: str,
    fp: str,
    width: int,
    dt: str = "",
    ir_token: str = "",
    referer: str = "",
) -> dict[str, Any]:
    """第二步：取题，返回 token / bg / front / type。"""
    return _get(
        client,
        "/api/v3/get",
        {
            "zoneId": ZONE_ID,
            "dt": dt,
            "irToken": ir_token,
            "id": captcha_id,
            "fp": fp,
            "https": "true",
            "type": "",
            "version": SDK_VERSION,
            "dpr": "2",
            "dev": "3",
            "cb": generate_cb(),
            "ipv6": "false",
            "runEnv": RUN_ENV,
            "group": "",
            "scene": "",
            "lang": "zh-CN",
            "sdkVersion": "",
            "loadVersion": LOAD_VERSION,
            "iv": "4",
            "user": "",
            "width": str(width),
            "audio": "false",
            "sizeType": "10",
            "smsVersion": "v3",
            "token": "",
            "callback": _callback(1),
        },
        referer,
    )


def check(
    client: httpx.Client,
    captcha_id: str,
    token: str,
    data: dict[str, Any],
    width: int,
    type_: int,
    dt: str = "",
    referer: str = "",
) -> dict[str, Any]:
    """第三步：提交求解载荷，成功时 ``data.validate`` 为裸 validate。"""
    return _get(
        client,
        "/api/v3/check",
        {
            "zoneId": ZONE_ID,
            "dt": dt,
            "id": captcha_id,
            "token": token,
            "data": json.dumps(data, separators=(",", ":"), ensure_ascii=True),
            "width": str(width),
            "type": str(type_),
            "version": SDK_VERSION,
            "cb": generate_cb(),
            "user": "",
            "extraData": "",
            "bf": "5001",
            "runEnv": RUN_ENV,
            "sdkVersion": "",
            "loadVersion": LOAD_VERSION,
            "iv": "4",
            "callback": _callback(2),
        },
        referer,
    )


def compose_validate(raw_validate: str, fp: str, zone_id: str = ZONE_ID) -> str:
    """SDK 二次组装：``{zoneId}_{aes(raw + '::' + fp)}_v_i_1``。"""
    return f"{zone_id}_{aes(raw_validate + '::' + fp)}_v_i_1"


# ---------------------------------------------------------------- 数据模型与编排


@dataclass(frozen=True)
class CaptchaTrigger:
    """一次易盾纯 HTTP 求解的题型、来源页与画布宽度配置。"""

    kind: CaptchaKind = "click"
    referer: str = DEFAULT_ZHIDAO_REFERER
    width_override: int | None = None
    captcha_id_override: str | None = None

    @property
    def captcha_id(self) -> str:
        if self.captcha_id_override:
            return self.captcha_id_override
        return CAPTCHA_CLICK if self.kind == "click" else CAPTCHA_SLIDE

    @property
    def width(self) -> int:
        if self.width_override:
            return int(self.width_override)
        return CLICK_WIDTH if self.kind == "click" else SLIDE_WIDTH

    @property
    def type_code(self) -> int:
        return CLICK_TYPE if self.kind == "click" else SLIDE_TYPE


@dataclass(frozen=True)
class CaptchaChallenge:
    """一次取题结果。"""

    captcha_id: str
    kind: CaptchaKind
    token: str
    bg_url: str
    front: str | list[str]
    type_code: int
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CaptchaSolution:
    """可直接上报给业务接口（validateSlideToken）的结果。"""

    validate: str
    raw_validate: str


class YidunHttpSolver:
    """纯 HTTP 易盾验证码求解器：失败/缺依赖/无 fp 一律返回 None，由调用方静默回退。"""

    def __init__(
        self,
        *,
        fp_provider: Callable[[], str | None] | None = None,
        proxy: str = "",
        timeout: float = 20.0,
        max_retry: int = 3,
        conf_override: dict | None = None,
        user_agent: str = "",
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._fp_provider = fp_provider
        self.proxy = str(proxy or "").strip()
        self.timeout = float(timeout)
        self.max_retry = max(1, int(max_retry))
        self.conf_override = conf_override
        self.user_agent = str(user_agent or "").strip()
        self._client_factory = client_factory

    def resolve_fp(self, explicit_fp: str | None = None) -> str | None:
        raw = explicit_fp
        if raw is None and callable(self._fp_provider):
            try:
                raw = self._fp_provider()
            except Exception:
                raw = None
        if raw is None:
            raw = get_session_fp()
        fp_value = normalize_fp(raw)
        if not is_valid_fp(fp_value):
            return None
        register_secret(fp_value)
        return fp_value

    def solve(
        self,
        trigger: CaptchaTrigger | None = None,
        *,
        fp: str | None = None,
    ) -> CaptchaSolution | None:
        effective_trigger = trigger or CaptchaTrigger()
        fp_value = self.resolve_fp(fp)
        if not fp_value:
            logger.debug("未获取到有效易盾 fp，跳过纯 HTTP 求解")
            return None

        if effective_trigger.kind == "slide" and not _slide_cv_available():
            logger.warn("OpenCV 不可用，跳过纯 HTTP 滑块求解")
            return None
        if effective_trigger.kind == "click" and not yidun_click.is_vision_available(
            self.conf_override
        ):
            logger.debug("视觉模型未配置，跳过纯 HTTP 点选求解")
            return None

        try:
            client = (
                self._client_factory()
                if callable(self._client_factory)
                else create_client(
                    proxy=self.proxy,
                    timeout=self.timeout,
                    user_agent=self.user_agent,
                )
            )
            with client:
                return self._solve_with_client(client, effective_trigger, fp_value)
        except Exception as exc:
            logger.warn(f"纯 HTTP 易盾验证码求解异常，将回退浏览器处理: {exc}")
            return None

    def _solve_with_client(
        self,
        client: httpx.Client,
        trigger: CaptchaTrigger,
        fp_value: str,
    ) -> CaptchaSolution | None:
        conf = getconf(client, trigger.captcha_id, trigger.referer)
        conf_data = conf.get("data") if isinstance(conf.get("data"), dict) else {}
        dt = str(conf_data.get("dt") or "")
        ir_token = str(conf_data.get("irToken") or "")

        for attempt in range(1, self.max_retry + 1):
            challenge = self._fetch(client, trigger, fp_value, dt, ir_token)
            if challenge is None:
                continue

            data = self._build_data(client, trigger, challenge)
            if data is None:
                logger.warn(
                    f"纯 HTTP 易盾验证码求解失败（第 {attempt}/{self.max_retry} 次）"
                )
                continue

            result = check(
                client,
                challenge.captcha_id,
                challenge.token,
                data,
                trigger.width,
                challenge.type_code,
                dt,
                trigger.referer,
            )
            payload = result.get("data") if isinstance(result.get("data"), dict) else {}
            if result.get("error") == 0 and payload.get("result") is True:
                raw_validate = str(payload.get("validate") or "")
                if raw_validate:
                    logger.debug(
                        f"纯 HTTP 易盾验证码通过 (raw_validate_len={len(raw_validate)})"
                    )
                    return CaptchaSolution(
                        validate=compose_validate(raw_validate, fp_value),
                        raw_validate=raw_validate,
                    )

            logger.warn(
                f"纯 HTTP 易盾验证码未通过（第 {attempt}/{self.max_retry} 次）: "
                f"error={result.get('error')} msg={result.get('msg')}"
            )
        return None

    def _fetch(
        self,
        client: httpx.Client,
        trigger: CaptchaTrigger,
        fp_value: str,
        dt: str,
        ir_token: str = "",
    ) -> CaptchaChallenge | None:
        got = fetch(
            client,
            trigger.captcha_id,
            fp_value,
            trigger.width,
            dt,
            ir_token,
            trigger.referer,
        )
        if got.get("error") != 0:
            logger.warn(
                f"纯 HTTP 易盾取题失败: error={got.get('error')} msg={got.get('msg')}"
            )
            return None

        payload = got.get("data") if isinstance(got.get("data"), dict) else {}
        bg = payload.get("bg")
        bg_url = (
            bg[0]
            if isinstance(bg, list) and bg
            else (bg if isinstance(bg, str) else "")
        )
        front = payload.get("front")
        token = str(payload.get("token") or "")
        if not token or not bg_url:
            logger.warn("纯 HTTP 易盾取题响应缺少 token 或背景图")
            return None

        raw_type = payload.get("type")
        try:
            type_code = int(raw_type) if raw_type is not None else trigger.type_code
        except (TypeError, ValueError):
            type_code = trigger.type_code

        effective_kind: CaptchaKind = trigger.kind
        if type_code == SLIDE_TYPE or isinstance(front, list):
            effective_kind = "slide"
        elif type_code == CLICK_TYPE or isinstance(front, str):
            effective_kind = "click"

        return CaptchaChallenge(
            captcha_id=trigger.captcha_id,
            kind=effective_kind,
            token=token,
            bg_url=bg_url,
            front=front if isinstance(front, (str, list)) else "",
            type_code=type_code,
            raw=payload,
        )

    def _build_data(
        self,
        client: httpx.Client,
        trigger: CaptchaTrigger,
        challenge: CaptchaChallenge,
    ) -> dict[str, Any] | None:
        if challenge.kind == "slide":
            return self._build_slide_data(client, trigger, challenge)
        return self._build_click_data(client, trigger, challenge)

    def _build_slide_data(
        self,
        client: httpx.Client,
        trigger: CaptchaTrigger,
        challenge: CaptchaChallenge,
    ) -> dict[str, Any] | None:
        front_url = (
            challenge.front[0]
            if isinstance(challenge.front, list) and challenge.front
            else ""
        )
        if not front_url:
            return None

        background_bytes = self._download(client, challenge.bg_url)
        front_bytes = self._download(client, front_url)
        matched = find_slide_gap_x(background_bytes, front_bytes)
        if matched is None:
            logger.warn("纯 HTTP 易盾滑块图片解码或匹配失败")
            return None

        gap_x, bg_width = matched
        distance = gap_x * trigger.width / bg_width
        trace = build_slide_track(distance)
        return build_slide_data(challenge.token, trace, distance, trigger.width)

    def _build_click_data(
        self,
        client: httpx.Client,
        trigger: CaptchaTrigger,
        challenge: CaptchaChallenge,
    ) -> dict[str, Any] | None:
        tips = challenge.front if isinstance(challenge.front, str) else ""
        if not tips.strip():
            return None
        if not yidun_click.is_vision_available(self.conf_override):
            return None

        image_bytes = self._download(client, challenge.bg_url)
        resolved = yidun_click.request_click_point(
            image_bytes,
            tips,
            conf_override=self.conf_override,
            proxy=self.proxy or None,
        )
        if resolved is None:
            return None

        px, py, image_width, _image_height = resolved
        x, y = to_click_css(px, py, image_width, trigger.width)
        logger.debug(
            f"纯 HTTP 易盾点选坐标: 原图({px:.0f},{py:.0f}) -> css({x},{y})"
        )
        return build_click_data(challenge.token, x, y)

    @staticmethod
    def _download(client: httpx.Client, url: str) -> bytes:
        response = client.get(url)
        response.raise_for_status()
        return response.content


__all__ = [
    "BASE64_ALPHABET",
    "BASE64_PADDING",
    "BASE_URL",
    "CAPTCHA_CLICK",
    "CAPTCHA_SLIDE",
    "CLICK_TYPE",
    "CLICK_WIDTH",
    "DEFAULT_ZHIDAO_REFERER",
    "PRIVATE_B64_ALPHABET",
    "PRIVATE_B64_PADDING",
    "SAMPLE_NUM",
    "SLIDE_TYPE",
    "SLIDE_WIDTH",
    "ZONE_ID",
    "CaptchaChallenge",
    "CaptchaKind",
    "CaptchaSolution",
    "CaptchaTrigger",
    "YidunHttpSolver",
    "aes",
    "aes_click",
    "base64_decode",
    "base64_decode_private",
    "base64_encode",
    "base64_encode_private",
    "build_click_data",
    "build_slide_data",
    "build_slide_track",
    "check",
    "compose_validate",
    "create_client",
    "fetch",
    "find_slide_gap_x",
    "gen_crc32",
    "generate_cb",
    "generate_uuid",
    "getconf",
    "parse_jsonp",
    "to_click_css",
    "with_crc",
    "xor_encode",
]
