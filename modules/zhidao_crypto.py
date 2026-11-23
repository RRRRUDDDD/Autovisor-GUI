# encoding=utf-8
"""智慧树接口的 AES-CBC 报文加密与 AI 问答签名."""
import hashlib
import json
import random
import string
from base64 import b64decode, b64encode
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from Crypto.Cipher import AES

IV = b"1g3qqdh4jvbskb9x"
HOME_KEY = b"7q9oko0vqb3la20r"
AI_KEY = b"hw2fdlwcj4cs1mx7"
VIDEO_KEY = b"azp53h0kft7qi78q"
QA_KEY = b"kcGOlISPkYKRksSK"
EXAM_KEY = b"onbfhdyvz8x7otrp"

ZHIDAO_AI_SIGN_PREFIX = "8ZflKEagfL"


def _pad(data: str) -> bytes:
    raw = data.encode()
    padding = 16 - len(raw) % 16
    return raw + bytes([padding]) * padding


def _unpad(data: bytes) -> str:
    return data[: -data[-1]].decode()


def encrypt(data: str, key: bytes = EXAM_KEY, iv: bytes = IV) -> str:
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return b64encode(cipher.encrypt(_pad(data))).decode()


def decrypt(data: str, key: bytes = EXAM_KEY, iv: bytes = IV) -> str:
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return _unpad(cipher.decrypt(b64decode(data)))


def random_session_nid() -> str:
    chars = string.ascii_lowercase + string.digits
    return "chatcmpl-" + "".join(random.choice(chars) for _ in range(24))


def _js_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ",".join(
            "[object Object]" if isinstance(item, dict) else _js_value(item)
            for item in value
        )
    if isinstance(value, dict):
        return "[object Object]"
    return str(value)


def _sign_input(data: dict) -> str:
    """按智慧树前端 sortASCII 规则构造待签名字符串: 非空字段按 ASCII 排序后以 key=value& 拼接."""
    parts = [
        f"{key}={_js_value(data[key])}"
        for key in sorted(data.keys())
        if data[key] not in ("", None)
    ]
    return "&".join(parts)


def sign_zhidao_ai(url: str, data: dict, prefix: str = ZHIDAO_AI_SIGN_PREFIX) -> tuple[str, dict]:
    """给智慧树 AI 流式问答地址补 sign 参数, 返回新地址与请求报文体."""
    scheme, netloc, path, query_string, fragment = urlsplit(url)
    query_params = dict(parse_qsl(query_string))
    body = dict(data)
    signature = hashlib.md5(
        (prefix + _sign_input(body)).encode("utf-8")
    ).hexdigest()
    query_params["sign"] = signature
    signed_url = urlunsplit(
        (scheme, netloc, path, urlencode(query_params), fragment)
    )
    return signed_url, body