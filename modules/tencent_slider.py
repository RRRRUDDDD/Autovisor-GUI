from __future__ import annotations

import asyncio
import base64
import random
import re
import time
from types import ModuleType
from urllib.parse import unquote_to_bytes, urljoin

from playwright.async_api import Page, TimeoutError

from modules.captcha_common import (
    decode_image_bytes,
    drag_element_horizontally,
    fetch_image_bytes,
    gen_track,
    get_mouse as _mouse,
    is_failure_tip,
    iter_targets as _iter_targets,
    page_of as _page_of,
    visible_locator as _visible_locator,
)
from modules.logger import Logger

logger = Logger()

# 腾讯 TCaptcha 新旧两代并存:
# 旧版 iframe 内是 #slideBg/#slideBlock 两张 <img>, 滑块是 #tcaptcha_drag_thumb;
# 新版 (tcaptcha_iframe_dy) 用 background-image 渲染, 滑块、轨道、缺失块在
# #tcOperation 下的位置随机, 只能靠类名和尺寸区分。
THUMB_SELECTORS = (
    "#tcaptcha_drag_thumb",
    "#tcaptcha_drag_button",
    "#tcOperation .tc-slider-normal",
    "#tcOperation div.tc-fg-item.tc-slider-normal",
    "div.tc-drag-thumb",
    ".tcaptcha-drag-icon",
)
BACKGROUND_SELECTORS = (
    "#slideBg",
    ".tc-captcha-mobile.tc-slide",
    ".tcaptcha-bg",
)
BLOCK_SELECTORS = (
    "#slideBlock",
    ".tcaptcha-block",
)
REFRESH_SELECTORS = (
    "#reload",
    ".tc-refresh",
    "button[title*='刷新']",
    "[class*='refresh' i]",
)
INSTRUCTION_SELECTORS = (
    "#instructionText",
    ".tcaptcha-title",
)
SLIDER_HINT = "拖动"
CLICK_HINT = "请依次点击"
MAX_ATTEMPTS = 3
RESULT_TIMEOUT_MS = 4_000
GAP_SCORE_THRESHOLD = 0.06
# 拼图块初始位置理论上在背景图内部; 超出这个范围说明取错了元素, 不再补偿。
PIECE_OFFSET_LIMIT_PX = 120.0
_BACKGROUND_URL_RE = re.compile(r"""url\(["']?([^"')]+)["']?\)""")


def parse_background_url(style_value: str) -> str | None:
    """从 computed style 的 background-image 中取出图片地址。"""
    if not style_value:
        return None
    match = _BACKGROUND_URL_RE.search(style_value)
    if not match:
        return None
    url = match.group(1).strip()
    return url or None


def resolve_media_url(base_url: str, url: str) -> str:
    if not url:
        return url
    if url.startswith(("data:", "http://", "https://", "blob:")):
        return url
    return urljoin(base_url or "", url)


def classify_drag_items(widths: list[float]) -> tuple[int, int, int] | None:
    """按宽度区分新版三件套: 轨道最宽, 滑块次之, 缺失块最窄。"""
    if len(widths) < 3:
        return None
    ordered = sorted(range(len(widths)), key=lambda index: widths[index], reverse=True)
    return ordered[0], ordered[1], ordered[2]


def gap_to_drag_distance(
    gap_x: float,
    background_natural_width: int,
    background_display_width: float,
    piece_offset: float,
) -> float:
    """缺口原图坐标 -> 鼠标拖动距离。

    拼图块随滑块 1:1 平移, 因此拖动距离 = 缺口显示坐标 - 拼图块初始显示偏移。
    """
    if background_natural_width <= 0 or background_display_width <= 0:
        return 0.0
    gap_display_x = float(gap_x) * background_display_width / background_natural_width
    offset = float(piece_offset)
    if offset < 0 or offset > PIECE_OFFSET_LIMIT_PX:
        offset = 0.0
    return round(max(0.0, gap_display_x - offset), 3)


def locate_gap(background, piece, np, cv2) -> tuple[float, float, float]:
    """用边缘模板匹配定位缺口, 返回 (x, y, score), 单位为背景图原图像素。"""
    if background is None or piece is None or background.size == 0 or piece.size == 0:
        raise ValueError("captcha image empty")

    mask = None
    offset_x = 0
    offset_y = 0
    if piece.ndim == 3 and piece.shape[2] == 4:
        alpha = piece[:, :, 3]
        ys, xs = np.where(alpha > 16)
        if len(xs) == 0:
            raise ValueError("piece alpha empty")
        x0, x1 = int(xs.min()), int(xs.max())
        y0, y1 = int(ys.min()), int(ys.max())
        # 裁剪时保留 2px 透明边距, 否则拼图外轮廓贴着数组边界, Canny 取不到。
        height, width = alpha.shape[:2]
        crop_x0, crop_x1 = max(0, x0 - 2), min(width - 1, x1 + 2)
        crop_y0, crop_y1 = max(0, y0 - 2), min(height - 1, y1 + 2)
        piece = piece[crop_y0 : crop_y1 + 1, crop_x0 : crop_x1 + 1]
        mask = (piece[:, :, 3] > 16).astype(np.uint8) * 255
        piece = piece[:, :, :3]
        offset_x = x0 - crop_x0
        offset_y = y0 - crop_y0

    background_gray = (
        cv2.cvtColor(background, cv2.COLOR_BGR2GRAY) if background.ndim == 3 else background
    )
    piece_gray = cv2.cvtColor(piece, cv2.COLOR_BGR2GRAY) if piece.ndim == 3 else piece
    if mask is not None:
        # 透明区域不参与边缘检测, 只保留拼图本身的轮廓。
        piece_gray = cv2.bitwise_and(piece_gray, piece_gray, mask=mask)

    background_edges = cv2.Canny(background_gray, 100, 200)
    piece_edges = cv2.Canny(piece_gray, 100, 200)
    if mask is not None:
        piece_edges = cv2.bitwise_and(piece_edges, piece_edges, mask=mask)
    if (
        piece_edges.shape[0] > background_edges.shape[0]
        or piece_edges.shape[1] > background_edges.shape[1]
    ):
        raise ValueError("piece larger than background")
    if np.count_nonzero(piece_edges) < 8:
        raise ValueError("piece edges too few")

    result = cv2.matchTemplate(background_edges, piece_edges, cv2.TM_CCOEFF_NORMED)
    _, score, _, location = cv2.minMaxLoc(result)
    # 匹配点是模板(含透明边距)的左上角, 换算回拼图可见轮廓的左上角。
    return float(location[0] + offset_x), float(location[1] + offset_y), float(score)


async def _fallback_thumb(target):
    try:
        items = target.locator("#tcOperation > div")
        count = await items.count()
    except Exception:
        return None
    sized = []
    for index in range(count):
        element = items.nth(index)
        try:
            if not await element.is_visible():
                continue
            box = await element.bounding_box()
            if not box:
                continue
            class_name = (await element.get_attribute("class") or "").lower()
            style = await element.evaluate("el => getComputedStyle(el).backgroundImage") or ""
        except Exception:
            continue
        if any(token in class_name for token in ("slider", "drag", "thumb")):
            return element
        if (
            not parse_background_url(style)
            and 20 <= box["width"] <= 90
            and 20 <= box["height"] <= 80
        ):
            return element
        sized.append((box["width"], element))
    if len(sized) >= 3:
        # 类名缺失时按宽度降序: 轨道 > 滑块 > 缺失块。
        mid_index = classify_drag_items([item[0] for item in sized])[1]
        return sized[mid_index][1]
    return None


async def _thumb(target):
    for selector in THUMB_SELECTORS:
        candidate = await _visible_locator(target, selector)
        if candidate is not None:
            return candidate
    return await _fallback_thumb(target)


async def has_slider_thumb(target) -> bool:
    """点选模块用它判断当前 tcaptcha 其实是滑块形态。"""
    return await _thumb(target) is not None


async def _instruction_text(target) -> str:
    for selector in INSTRUCTION_SELECTORS:
        try:
            locator = target.locator(selector)
            for index in range(await locator.count()):
                item = locator.nth(index)
                if not await item.is_visible():
                    continue
                text = " ".join((await item.inner_text()).split())
                if text:
                    return text
        except Exception:
            continue
    return ""


async def _frame_is_slider(target) -> bool:
    text = await _instruction_text(target)
    if text:
        if CLICK_HINT in text or ("点击" in text and SLIDER_HINT not in text):
            return False
        if SLIDER_HINT in text:
            return True
    return await _thumb(target) is not None


async def _frame_has_captcha(target) -> bool:
    if await _thumb(target) is not None:
        return True
    if await _instruction_text(target):
        return True
    return await _visible_locator(target, "#slideBg") is not None


async def resolve_tencent_slider_target(page: Page):
    """返回包含腾讯滑块拼图的 page/frame; 点选等其它形态返回 None。"""
    for target in _iter_targets(page):
        try:
            if await _frame_is_slider(target):
                return target
        except Exception:
            continue
    return None


async def resolve_tencent_captcha_target(page: Page):
    """返回任意形态的腾讯验证码 page/frame; 找不到时返回 None。"""
    for target in _iter_targets(page):
        try:
            if await _frame_has_captcha(target):
                return target
        except Exception:
            continue
    return None


async def tencent_captcha_visible(page: Page) -> bool:
    return await resolve_tencent_captcha_target(page) is not None


async def describe_tencent_mode(target) -> str | None:
    text = await _instruction_text(target)
    if not text:
        return None
    if CLICK_HINT in text or "点击" in text:
        return "点选"
    if "短信" in text or "手机" in text:
        return "短信验证"
    return None


async def _background(target):
    for selector in BACKGROUND_SELECTORS:
        candidate = await _visible_locator(target, selector)
        if candidate is not None:
            return candidate
    return None


async def _piece(target):
    for selector in BLOCK_SELECTORS:
        candidate = await _visible_locator(target, selector)
        if candidate is not None:
            return candidate
    # 新版缺失块与滑块、轨道混在 #tcOperation 下, 取带背景图且最窄的那个。
    try:
        items = target.locator("#tcOperation > div")
        count = await items.count()
    except Exception:
        return None
    candidates = []
    for index in range(count):
        element = items.nth(index)
        try:
            if not await element.is_visible():
                continue
            box = await element.bounding_box()
            if not box or not 20 <= box["width"] <= 120 or not 20 <= box["height"] <= 120:
                continue
            class_name = (await element.get_attribute("class") or "").lower()
            if any(token in class_name for token in ("slider", "drag", "thumb", "track")):
                continue
            style = await element.evaluate("el => getComputedStyle(el).backgroundImage") or ""
            if not parse_background_url(style):
                continue
            candidates.append((box["width"], element))
        except Exception:
            continue
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def _decode_data_uri(uri: str) -> bytes | None:
    try:
        header, data = uri.split(",", 1)
        if ";base64" in header:
            return base64.b64decode(data)
        return unquote_to_bytes(data)
    except Exception:
        return None


async def _element_media(target, locator) -> tuple[bytes | None, str]:
    base_url = getattr(target, "url", "") or ""
    try:
        tag_name = await locator.evaluate("el => el.tagName")
    except Exception:
        tag_name = ""
    if tag_name == "CANVAS":
        try:
            uri = await locator.evaluate("el => el.toDataURL('image/png')")
            return _decode_data_uri(uri), ""
        except Exception:
            return None, ""
    try:
        src = await locator.get_attribute("src")
    except Exception:
        src = None
    if src:
        if src.startswith("data:"):
            return _decode_data_uri(src), ""
        return None, resolve_media_url(base_url, src)
    try:
        style = await locator.evaluate("el => getComputedStyle(el).backgroundImage") or ""
    except Exception:
        style = ""
    url = parse_background_url(style)
    if not url:
        return None, ""
    if url.startswith("data:"):
        return _decode_data_uri(url), ""
    return None, resolve_media_url(base_url, url)


async def _fetch_image_bytes(target, url: str) -> bytes | None:
    return await fetch_image_bytes(target, url, allow_eval_fetch=True)


async def _load_image(target, locator, np, cv2):
    data, url = await _element_media(target, locator)
    if data is None:
        data = await _fetch_image_bytes(target, url)
    return decode_image_bytes(data, np, cv2, unchanged=True)


def _resize_piece(image, expected_width: float, np, cv2):
    if not expected_width or expected_width <= 0:
        return image
    height, width = image.shape[:2]
    if abs(width - expected_width) <= 3:
        return image
    ratio = expected_width / width
    size = (max(1, int(round(width * ratio))), max(1, int(round(height * ratio))))
    return cv2.resize(image, size, interpolation=cv2.INTER_AREA)


async def _measure_gap(target, background, piece, background_box, piece_box, modules):
    np, cv2 = modules
    background_image = await _load_image(target, background, np, cv2)
    piece_image = await _load_image(target, piece, np, cv2)
    if background_image is None or piece_image is None:
        raise TimeoutError("tencent captcha image unavailable")

    background_height, background_width = background_image.shape[:2]
    if background_width <= 0 or background_height <= 0 or background_box["width"] <= 0:
        raise TimeoutError("tencent captcha image size invalid")

    scale = background_box["width"] / background_width
    piece_image = await asyncio.to_thread(
        _resize_piece, piece_image, piece_box["width"] / scale, np, cv2
    )
    gap_x, _gap_y, score = await asyncio.to_thread(
        locate_gap, background_image, piece_image, np, cv2
    )
    if score < GAP_SCORE_THRESHOLD:
        raise TimeoutError(f"tencent gap match too weak: {score:.3f}")

    offset = piece_box["x"] - background_box["x"]
    distance = gap_to_drag_distance(gap_x, background_width, background_box["width"], offset)
    details = {
        "gap": round(gap_x * scale, 1),
        "score": round(score, 3),
        "offset": round(offset, 1),
    }
    return distance, details


async def _drag(target, thumb, distance: float) -> None:
    await drag_element_horizontally(
        target,
        thumb,
        distance,
        approach_range=(30.0, 70.0),
        not_found_message="tencent slider thumb not found",
    )


async def _success_ticket(target) -> bool:
    # 票据 #randstr 通常由宿主页面持有, 验证码后位于 iframe 之外。
    roots = [target]
    host = _page_of(target)
    if host is not target:
        roots.append(host)
    for root in roots:
        try:
            locator = root.locator("#randstr")
            if await locator.count():
                element = locator.first
                text = ""
                try:
                    text = (await element.inner_text()).strip()
                except Exception:
                    text = ""
                if not text:
                    try:
                        text = (await element.input_value()).strip()
                    except Exception:
                        text = ""
                if text:
                    return True
        except Exception:
            pass
        try:
            status = root.locator("#tcStatus")
            if await status.count():
                text = " ".join((await status.first.inner_text()).split())
                if "成功" in text:
                    return True
        except Exception:
            pass
    return False


async def _wait_result(target, timeout_ms: int = RESULT_TIMEOUT_MS) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        try:
            if await _success_ticket(target):
                return True
            # 浮层关闭(iframe 被移除)即视为通过; 刷新换图时滑块会短暂消失,
            # 因此间隔复查一次, 避免把自动刷新误判成通过。
            if await _thumb(target) is None:
                await asyncio.sleep(0.3)
                if await _thumb(target) is None:
                    return True
                continue
            if is_failure_tip(await _instruction_text(target)):
                return False
        except Exception:
            return True
        await asyncio.sleep(0.15)
    return False


async def _media_signature(target, locator) -> str:
    if locator is None:
        return ""
    try:
        src = await locator.get_attribute("src")
    except Exception:
        src = None
    if src:
        return src
    try:
        return await locator.evaluate("el => getComputedStyle(el).backgroundImage") or ""
    except Exception:
        return ""


async def _refresh(target) -> None:
    before = await _media_signature(target, await _background(target))
    clicked = False
    for selector in REFRESH_SELECTORS:
        try:
            locator = target.locator(selector)
            for index in range(await locator.count()):
                element = locator.nth(index)
                if not await element.is_visible():
                    continue
                await element.click(timeout=1500)
                clicked = True
                break
        except Exception:
            continue
        if clicked:
            break
    if not clicked:
        return
    # 等新图加载, 避免下一次识别读到旧图。
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        await asyncio.sleep(0.2)
        current = await _media_signature(target, await _background(target))
        if current and current != before:
            return


async def solve_tencent_slider(
    page,
    modules: list[ModuleType],
    max_attempts: int = MAX_ATTEMPTS,
) -> bool:
    np, cv2 = modules
    if not np or not cv2:
        logger.warn("OpenCV或Numpy导入失败,无法开启腾讯滑块验证.")
        return False

    for attempt in range(1, max_attempts + 1):
        try:
            background = await _background(page)
            thumb = await _thumb(page)
            if background is None or thumb is None:
                raise TimeoutError("tencent captcha not ready")
            piece = await _piece(page)
            if piece is None:
                raise TimeoutError("tencent missing block not found")
            background_box = await background.bounding_box()
            piece_box = await piece.bounding_box()
            if not background_box or not piece_box:
                raise TimeoutError("tencent captcha layout not ready")

            distance, details = await _measure_gap(
                page, background, piece, background_box, piece_box, modules
            )
            logger.debug(
                f"腾讯滑块缺口识别: x={details['gap']}px, 初始偏移={details['offset']}px, "
                f"score={details['score']}, 拖动={distance}px"
            )
            # 重试时轻微左右试探, 规避缩放换算的系统性偏差。
            bias = (0.0, 2.0, -2.0, 4.0)[min(attempt - 1, 3)]
            drag_distance = max(0.0, distance + bias)
            if bias:
                logger.debug(f"腾讯滑块第{attempt}次尝试补偿 {bias:+.0f}px")
            logger.info(f"第{attempt}次尝试过腾讯滑块验证...")
            await _drag(page, thumb, drag_distance)
            if await _wait_result(page):
                logger.info("腾讯滑块验证已成功通过.")
                logger.event(
                    "滑块验证",
                    类型="腾讯",
                    结果="通过",
                    尝试=attempt,
                    缺口=f"{details['gap']}px",
                )
                return True
            logger.warn(f"第{attempt}次腾讯滑块验证未通过,正在刷新...")
        except TimeoutError as exc:
            logger.debug(f"腾讯滑块识别超时: {exc}")
        except Exception as exc:
            logger.log_exception("腾讯滑块验证异常.", exc)
        if attempt < max_attempts:
            await asyncio.sleep(0.5)
            await _refresh(page)

    logger.warn("自动过腾讯滑块验证失败,请手动验证!")
    logger.event("滑块验证", 类型="腾讯", 结果="失败", 尝试=max_attempts)
    return False