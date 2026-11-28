# encoding=utf-8
"""易盾点选验证码（yidun--word / yidun--icon）VLM 自动求解模块。

- 未配置视觉模型时直接返回 False，保持原有人工兜底与推送提醒流程；
- 已配置视觉模型时复用 ``modules.llm.chat_vision_sync``（遵循 ``temperature=0.0``、
  ``max_tokens=8000`` 兼容思考模型高 token 预算），以 JSON + ``x/y`` 正则双解析器提取原图坐标，
  并按 ``img.yidun_bg-img`` 实时 ``bounding_box()`` 等比缩放后模拟人类点击；
- 当前提示词与点击流程面向智慧树常见 ``type=11`` 单点推理形态。
"""
from __future__ import annotations

import asyncio
import base64
import inspect
import json
import math
import random
import re
import time
from io import BytesIO
from typing import Any
from urllib.parse import unquote_to_bytes

from PIL import Image
from playwright.async_api import Page, TimeoutError

from modules import llm
from modules.captcha_common import (
    fetch_image_bytes,
    get_mouse as _mouse,
    image_to_display_point,
    is_failure_tip,
    iter_targets,
    page_of as _page_of,
    visible_locator,
)
from modules.logger import Logger

YIDUN_CLICK_MODES = {
    "yidun--word": "文字点选",
    "yidun--icon": "图标点选",
}
YIDUN_BG_SELECTOR = "img.yidun_bg-img"
YIDUN_TIPS_SELECTOR = ".yidun_tips__text"
YIDUN_TIPS_FALLBACK_SELECTOR = ".yidun_tips"
YIDUN_REFRESH_SELECTOR = ".yidun_refresh, .yidun_icon-refresh"
MAX_ATTEMPTS = 3
VISION_TEMPERATURE = 0.0
VISION_MAX_TOKENS = 8000

VISION_CLICK_PROMPT = """这是网易易盾的点选验证码图片，尺寸 {width}x{height} 像素。
提示语：{tips}

找出提示语要求点击的那个字符或图标（注意颜色、大小写、形状以及「朝向一样」这类约束），
给出它在图片中的中心坐标。

只输出 JSON，不要解释：
{{"x": <横坐标整数>, "y": <纵坐标整数>}}"""

_THINK_BLOCK_RE = re.compile(
    r"<(?:think|thought)>.*?</(?:think|thought)>", re.IGNORECASE | re.DOTALL
)
_UNCLOSED_THINK_RE = re.compile(r"<(?:think|thought)>.*$", re.IGNORECASE | re.DOTALL)
_JSON_OBJECT_RE = re.compile(r"\{[^{}]*\}")
_XY_PAIR_RE = re.compile(
    r"[xX]\s*[=:：]\s*(-?\d+(?:\.\d+)?).*?[yY]\s*[=:：]\s*(-?\d+(?:\.\d+)?)",
    re.S,
)

logger = Logger()


def detect_yidun_click_mode(class_name: str) -> str | None:
    """根据易盾根节点 class 判断是否为可由 VLM 处理的点选形态。"""
    if not class_name:
        return None
    for token, label in YIDUN_CLICK_MODES.items():
        if token in class_name or token.replace("--", "_") in class_name:
            return label
    return None


async def resolve_yidun_click_target(page: Page) -> tuple[Any, str] | None:
    """返回包含易盾文字/图标点选验证码的 ``(target, mode_label)``；否则返回 None。"""
    for target in iter_targets(page):
        try:
            roots = target.locator(".yidun")
            for index in range(await roots.count()):
                root = roots.nth(index)
                if not await root.is_visible():
                    continue
                label = detect_yidun_click_mode(await root.get_attribute("class") or "")
                if label:
                    return target, label
        except Exception:
            continue
    return None


async def yidun_click_visible(page: Page) -> bool:
    """页面是否存在可见的易盾点选验证码。"""
    return await resolve_yidun_click_target(page) is not None


def is_vision_available(conf_override: dict | None = None) -> bool:
    """判断当前是否已配置可用的视觉模型。"""
    if getattr(llm, "requests", None) is None:
        return False
    if conf_override is not None:
        normalized = llm.normalize_conf(conf_override)
        if not normalized.get("vision_model") or not normalized.get("api_base"):
            return False
        if normalized.get("api_key"):
            return True
        return llm.is_local_endpoint(
            normalized.get("api_base", ""),
            normalized.get("provider", ""),
            normalized.get("api_format", ""),
        )
    return bool(llm.vision_enabled())


def build_vision_prompt(width: int, height: int, tips: str) -> str:
    """构造带图片像素尺寸与提示文案的 VLM 坐标提示词。"""
    return VISION_CLICK_PROMPT.format(width=int(width), height=int(height), tips=tips.strip())


def _is_valid_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    try:
        num = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(num)


def _in_bounds(
    x: float,
    y: float,
    width: int | None = None,
    height: int | None = None,
) -> bool:
    if not (math.isfinite(x) and math.isfinite(y)):
        return False
    if x < 0.0 or y < 0.0:
        return False
    if width is not None and x > float(width):
        return False
    if height is not None and y > float(height):
        return False
    return True


def parse_click_point(
    content: str,
    *,
    width: int | None = None,
    height: int | None = None,
) -> tuple[float, float] | None:
    """从 VLM 回复中解析点击坐标（JSON 优先，失败回退 ``x/y`` 正则）。"""
    if not content:
        return None
    stripped = _THINK_BLOCK_RE.sub("", content)
    stripped = _UNCLOSED_THINK_RE.sub("", stripped).strip()
    cleaned = stripped or content.strip()

    # 思考模型可能在最终 JSON 前给出分析；优先检查清理后的 JSON 对象（取最后一个合法对象）。
    json_candidates = _JSON_OBJECT_RE.findall(cleaned)
    for raw_obj in reversed(json_candidates):
        try:
            data = json.loads(raw_obj)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "x" in data and "y" in data:
            raw_x, raw_y = data["x"], data["y"]
            if _is_valid_number(raw_x) and _is_valid_number(raw_y):
                x, y = float(raw_x), float(raw_y)
                if _in_bounds(x, y, width=width, height=height):
                    return x, y

    for pair in reversed(list(_XY_PAIR_RE.finditer(cleaned))):
        x, y = float(pair.group(1)), float(pair.group(2))
        if _in_bounds(x, y, width=width, height=height):
            return x, y

    logger.warn(f"易盾点选 VLM 返回结果无法解析: {cleaned[:120]}")
    return None


def _inspect_image(image_bytes: bytes) -> tuple[int, int, str]:
    with Image.open(BytesIO(image_bytes)) as img:
        width, height = img.size
        fmt = (img.format or "png").lower()
        if fmt == "jpg":
            fmt = "jpeg"
        mime = f"image/{fmt}"
    if width <= 0 or height <= 0:
        raise ValueError("验证码图片尺寸无效")
    return int(width), int(height), mime


def _invoke_chat_vision(
    image_bytes: bytes,
    prompt: str,
    *,
    mime: str,
    conf_override: dict | None = None,
    proxy: str | None = None,
) -> str:
    """调用 ``modules.llm.chat_vision_sync`` 并显式透传 temperature=0.0 与 max_tokens=8000。"""
    vision_fn = llm.chat_vision_sync
    kwargs: dict[str, Any] = {
        "mime": mime,
        "conf_override": conf_override,
        "proxy": proxy,
    }
    try:
        params = inspect.signature(vision_fn).parameters
        accepts_varkw = any(
            param.kind == inspect.Parameter.VAR_KEYWORD for param in params.values()
        )
        if accepts_varkw or "temperature" in params:
            kwargs["temperature"] = VISION_TEMPERATURE
        if accepts_varkw or "max_tokens" in params:
            kwargs["max_tokens"] = VISION_MAX_TOKENS
    except (TypeError, ValueError):
        kwargs["temperature"] = VISION_TEMPERATURE
        kwargs["max_tokens"] = VISION_MAX_TOKENS
    return vision_fn(image_bytes, prompt, **kwargs)


def request_click_point(
    image_bytes: bytes,
    tips: str,
    *,
    conf_override: dict | None = None,
    proxy: str | None = None,
) -> tuple[float, float, int, int] | None:
    """调用已配置视觉模型识别点击坐标，返回 ``(x, y, img_width, img_height)``。"""
    if not image_bytes or not tips.strip():
        return None
    try:
        width, height, mime = _inspect_image(image_bytes)
    except Exception as exc:
        logger.warn(f"易盾点选图片解析失败: {exc}")
        return None

    prompt = build_vision_prompt(width, height, tips)
    try:
        content = _invoke_chat_vision(
            image_bytes,
            prompt,
            mime=mime,
            conf_override=conf_override,
            proxy=proxy,
        )
    except Exception as exc:
        logger.warn(f"易盾点选 VLM 识图请求失败: {exc}")
        return None

    if not content:
        logger.warn("易盾点选 VLM 返回空内容")
        return None

    point = parse_click_point(content, width=width, height=height)
    if point is None:
        return None
    return point[0], point[1], width, height


def _decode_data_uri(uri: str) -> bytes | None:
    try:
        header, payload = uri.split(",", 1)
        if ";base64" in header:
            return base64.b64decode(payload)
        return unquote_to_bytes(payload)
    except Exception:
        return None


async def _wait_click_ready(target, timeout_ms: int = 5000) -> None:
    try:
        loading = await visible_locator(
            target, "div.yidun--loading, div.yidun_loadicon", fallback_first=False
        )
        if loading is not None:
            await loading.wait_for(state="hidden", timeout=timeout_ms)
    except Exception:
        pass
    bg_all = target.locator(YIDUN_BG_SELECTOR)
    try:
        count = await bg_all.count()
    except Exception:
        count = 0
    if count <= 1:
        await bg_all.first.wait_for(state="visible", timeout=timeout_ms)
        return
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if await visible_locator(target, YIDUN_BG_SELECTOR, fallback_first=False) is not None:
            return
        await asyncio.sleep(0.1)
    raise TimeoutError("yidun click background image not visible")


async def _read_tips(target) -> str:
    for selector in (YIDUN_TIPS_SELECTOR, YIDUN_TIPS_FALLBACK_SELECTOR):
        try:
            locator = target.locator(selector)
            texts = []
            for index in range(await locator.count()):
                item = locator.nth(index)
                if await item.is_visible():
                    text = " ".join((await item.inner_text()).split())
                    if text:
                        texts.append(text)
            if texts:
                return "; ".join(texts)
        except Exception:
            continue
    return ""


async def _bg_src(target) -> str | None:
    try:
        locator = await visible_locator(target, YIDUN_BG_SELECTOR, fallback_first=True)
        if locator is not None:
            return await locator.get_attribute("src")
    except Exception:
        return None
    return None


async def _capture_bg_bytes(target, bg_locator, proxy: str | None = None) -> bytes | None:
    try:
        src = await bg_locator.get_attribute("src")
    except Exception:
        src = None
    data: bytes | None = None
    if src:
        if src.startswith("data:"):
            data = _decode_data_uri(src)
        else:
            data = await fetch_image_bytes(
                _page_of(target), src, allow_eval_fetch=False, proxy=proxy
            )
    if data:
        return data
    try:
        return await bg_locator.screenshot(type="png")
    except Exception:
        return None


async def click_yidun_point(
    target,
    bg_locator,
    pixel_x: float,
    pixel_y: float,
    image_width: int,
    image_height: int,
) -> tuple[float, float] | None:
    """按 ``img.yidun_bg-img`` 实时 ``bounding_box()`` 缩放坐标并模拟人类鼠标移动点击。"""
    box = await bg_locator.bounding_box()
    if (
        not box
        or box.get("width", 0) <= 0
        or box.get("height", 0) <= 0
        or image_width <= 0
        or image_height <= 0
    ):
        return None

    css_x, css_y = image_to_display_point(
        (pixel_x, pixel_y),
        image_width,
        image_height,
        box["width"],
        box["height"],
    )
    css_x = min(max(css_x, 1.0), max(1.0, box["width"] - 1.0))
    css_y = min(max(css_y, 1.0), max(1.0, box["height"] - 1.0))
    target_x = round(float(box["x"]) + css_x, 2)
    target_y = round(float(box["y"]) + css_y, 2)

    mouse = _mouse(target)
    start_x = target_x + random.uniform(-32.0, 32.0)
    start_y = target_y + random.uniform(-24.0, 24.0)
    await mouse.move(start_x, start_y)
    await asyncio.sleep(random.uniform(0.04, 0.09))
    mid_x = (start_x + target_x) / 2 + random.uniform(-4.0, 4.0)
    mid_y = (start_y + target_y) / 2 + random.uniform(-4.0, 4.0)
    await mouse.move(mid_x, mid_y)
    await asyncio.sleep(random.uniform(0.03, 0.07))
    await mouse.move(target_x, target_y)
    await asyncio.sleep(random.uniform(0.04, 0.09))
    await mouse.down()
    await asyncio.sleep(random.uniform(0.04, 0.08))
    await mouse.up()
    return target_x, target_y


async def _bg_visible(target) -> bool:
    try:
        locator = target.locator(YIDUN_BG_SELECTOR)
        for index in range(await locator.count()):
            if await locator.nth(index).is_visible():
                return True
    except Exception:
        return False
    return False


async def _wait_verify_result(target, timeout_ms: int = 3500) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if not await _bg_visible(target):
            return True
        if is_failure_tip(await _read_tips(target)):
            return False
        await asyncio.sleep(0.15)
    return False


async def _refresh_click_captcha(target) -> None:
    old_src = await _bg_src(target)
    clicked = False
    for locator in (
        target.locator(YIDUN_REFRESH_SELECTOR),
        target.get_by_text("刷新", exact=True),
    ):
        try:
            for index in range(await locator.count()):
                element = locator.nth(index)
                if not await element.is_visible():
                    continue
                await element.click(timeout=1500)
                clicked = True
                break
            if clicked:
                break
        except Exception:
            continue
    if not clicked:
        return
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        await asyncio.sleep(0.2)
        if not await _bg_visible(target):
            return
        src = await _bg_src(target)
        if src and src != old_src:
            break
    await _wait_click_ready(target)


async def solve_yidun_click(
    page: Page,
    *,
    max_attempts: int = MAX_ATTEMPTS,
    proxy: str | None = None,
    conf_override: dict | None = None,
) -> bool:
    """自动求解易盾文字/图标点选验证码；未配置视觉模型或重试失败时返回 False 兜底人工。"""
    found = await resolve_yidun_click_target(page)
    if found is None:
        return False
    target, mode_label = found
    mode_name = f"易盾「{mode_label}」"

    if not is_vision_available(conf_override):
        logger.warn(
            f"检测到{mode_name}验证,未配置视觉模型,请手动完成.",
            shift=True,
        )
        logger.event("滑块验证", 结果="不支持", 类型=mode_name)
        return False

    attempts = max(1, int(max_attempts))
    for attempt in range(1, attempts + 1):
        try:
            await _wait_click_ready(target)
            tips = await _read_tips(target)
            bg_locator = await visible_locator(target, YIDUN_BG_SELECTOR, fallback_first=True)
            if bg_locator is None or not tips:
                raise TimeoutError("yidun click captcha not ready")

            image_bytes = await _capture_bg_bytes(target, bg_locator, proxy=proxy)
            if not image_bytes:
                raise TimeoutError("yidun click image unavailable")

            resolved = await asyncio.to_thread(
                request_click_point,
                image_bytes,
                tips,
                conf_override=conf_override,
                proxy=proxy,
            )
            if not await _bg_visible(target):
                logger.info(f"{mode_name}弹窗在识图期间已消失, 跳过点击.")
                logger.event("点选验证", 类型=mode_name, 结果="通过", 尝试=attempt)
                return True
            if resolved is None:
                logger.warn(f"第{attempt}次{mode_name}VLM识图失败,正在刷新...")
            else:
                px, py, img_w, img_h = resolved
                logger.info(
                    f"第{attempt}次尝试{mode_name}验证: {tips} -> ({px:.0f}, {py:.0f})"
                )
                logger.event(
                    "点选验证",
                    类型=mode_name,
                    尝试=attempt,
                    题目=tips,
                    坐标=f"({px:.0f},{py:.0f})",
                )
                clicked = await click_yidun_point(
                    target, bg_locator, px, py, img_w, img_h
                )
                if clicked is None:
                    raise TimeoutError("yidun click bounding box unavailable")

                if await _wait_verify_result(target) or not await _bg_visible(target):
                    logger.info(f"{mode_name}验证已成功通过.")
                    logger.event("点选验证", 类型=mode_name, 结果="通过", 尝试=attempt)
                    return True
                logger.warn(f"第{attempt}次{mode_name}验证未通过,正在刷新...")
        except TimeoutError as exc:
            logger.debug(f"{mode_name}识别超时: {exc}")
        except Exception as exc:
            logger.log_exception(f"{mode_name}验证异常.", exc)
        if attempt < attempts:
            await _refresh_click_captcha(target)

    logger.warn(f"自动过{mode_name}验证失败,请手动验证!", shift=True)
    logger.event("点选验证", 类型=mode_name, 结果="失败", 尝试=attempts)
    return False
