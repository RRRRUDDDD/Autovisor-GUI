from __future__ import annotations

import asyncio
import random
import time
from types import ModuleType

from playwright.async_api import Page, TimeoutError

from modules.captcha_common import (
    FAILURE_WORDS,  # noqa: F401  兼容导出供测试与外部调用方复用
    decode_image_bytes,
    drag_element_horizontally,
    fetch_image_bytes,
    gen_track,  # noqa: F401  兼容导出供测试与外部调用方复用
    image_to_display_point,
    is_failure_tip,
    iter_targets,
    page_of as _page_of,
    visible_locator,
)
from modules.logger import Logger

YIDUN_PUZZLE_SELECTOR = ".yidun_bgimg, img.yidun_bg-img"
YIDUN_BG_SELECTOR = "img.yidun_bg-img"
YIDUN_BLOCK_SELECTOR = "img.yidun_jigsaw"
YIDUN_SLIDER_SELECTOR = "div.yidun_slider"
YIDUN_TRACK_SELECTOR = "div.yidun_control"
YIDUN_REFRESH_SELECTOR = ".yidun_refresh, .yidun_icon-refresh"
YIDUN_TIPS_SELECTOR = ".yidun_tips__text"
# 易盾除拼图外还有图标/文字点选、短信验证等形态, 滑块模块不直接处理它们。
YIDUN_UNSUPPORTED_MODES = {
    "yidun--icon": "图标点选",
    "yidun--word": "文字点选",
    "yidun--sms": "短信验证",
}
IN_COURSE_APPEAR_TIMEOUT_MS = 8_000
LOGIN_APPEAR_TIMEOUT_MS = 24 * 3600 * 1000
# 腾讯验证码内容在 iframe 里, 主文档只能等 iframe 或易盾图片出现。
SLIDER_APPEAR_SELECTOR = (
    f"{YIDUN_PUZZLE_SELECTOR}, iframe[id^='tcaptcha'], iframe.tcaptcha-iframe, "
    "iframe[src*='captcha.qq.com'], [id^='tcaptcha_transform']"
)
# 当实时 DOM 轨道/滑块尺寸不可用时的回退位移补偿(px); 实时 DOM 可用时按
# (track_width - slider_width) / (bg_width - block_width) 比例换算。
GAP_BIAS_PX = 16
YIDUN_PIECE_OFFSET_LIMIT_PX = 80.0
MAX_ATTEMPTS = 4

logger = Logger()


async def download_image(
    url: str,
    page=None,
    *,
    np=None,
    cv2=None,
    proxy: str | None = None,
    unchanged: bool = False,
):
    """代理感知地下载易盾验证码图片并解码为 OpenCV 图像。"""
    if np is None or cv2 is None:
        import cv2 as _cv2
        import numpy as _np

        np = np or _np
        cv2 = cv2 or _cv2
    data = await fetch_image_bytes(page, url, allow_eval_fetch=False, proxy=proxy)
    return decode_image_bytes(data, np, cv2, unchanged=unchanged)


def process_background_image(image, cv2=None):
    """背景图直接转灰度后执行 Canny(50, 150) 提取缺口边缘。"""
    if cv2 is None:
        import cv2 as _cv2

        cv2 = _cv2
    if image.ndim == 2:
        gray = image
    elif image.shape[2] == 4:
        gray = cv2.cvtColor(image, cv2.COLOR_BGRA2GRAY)
    else:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return cv2.Canny(gray, 50, 150)


def process_block_image(image, cv2=None, np=None):
    """提取拼图前景图边缘：保留 Alpha 通道(>50 + 3x3 腐蚀)过滤透明外框边缘。"""
    if cv2 is None:
        import cv2 as _cv2

        cv2 = _cv2
    if np is None:
        import numpy as _np

        np = _np
    if image.ndim == 2:
        bgra = cv2.cvtColor(image, cv2.COLOR_GRAY2BGRA)
    elif image.shape[2] == 3:
        bgra = cv2.cvtColor(image, cv2.COLOR_BGR2BGRA)
    else:
        bgra = image

    rgb = bgra[:, :, :3]
    alpha = bgra[:, :, 3]
    _, mask = cv2.threshold(alpha, 50, 255, cv2.THRESH_BINARY)
    front_edge = cv2.Canny(cv2.cvtColor(rgb, cv2.COLOR_BGR2GRAY), 50, 150)
    eroded_mask = cv2.erode(mask, np.ones((3, 3), np.uint8))
    masked_edge = cv2.bitwise_and(front_edge, front_edge, mask=eroded_mask)
    if np.count_nonzero(masked_edge) == 0 and np.count_nonzero(front_edge) > 0:
        masked_edge = cv2.bitwise_and(front_edge, front_edge, mask=mask)
    return masked_edge


async def resolve_captcha_target(page: Page):
    """返回包含易盾拼图的 page/frame; 找不到时返回 None。"""
    for target in iter_targets(page):
        try:
            if await target.locator(YIDUN_PUZZLE_SELECTOR).count() > 0:
                return target
        except Exception:
            continue
    return None


def unsupported_mode_label(class_name: str) -> str | None:
    """易盾根节点带验证类型类名, 返回当前形态的中文名; 拼图形态返回 None。"""
    for token, label in YIDUN_UNSUPPORTED_MODES.items():
        if token in class_name or token.replace("--", "_") in class_name:
            return label
    return None


async def describe_unsupported_mode(page: Page) -> str | None:
    for target in iter_targets(page):
        try:
            roots = target.locator(".yidun")
            for index in range(await roots.count()):
                root = roots.nth(index)
                if not await root.is_visible():
                    continue
                label = unsupported_mode_label(await root.get_attribute("class") or "")
                if label:
                    return label
        except Exception:
            continue
    return None


async def _visible_locator(target, selector):
    return await visible_locator(target, selector, fallback_first=True)


async def _puzzle_visible(target) -> bool:
    try:
        puzzle = target.locator(YIDUN_BG_SELECTOR)
        for index in range(await puzzle.count()):
            if await puzzle.nth(index).is_visible():
                return True
    except Exception:
        return False
    return False


async def yidun_captcha_visible(page: Page) -> bool:
    """页面是否存在易盾验证码浮层（含暂不支持的易盾形态）。"""
    for target in iter_targets(page):
        try:
            if await _puzzle_visible(target):
                return True
        except Exception:
            continue
    return bool(await describe_unsupported_mode(page))


async def captcha_visible(page: Page) -> bool:
    """兼容入口：登录页是否仍有验证码浮层(含易盾与腾讯验证码)。"""
    from modules.captcha_router import captcha_visible as _router_captcha_visible

    return await _router_captcha_visible(page)


async def _wait_puzzle_ready(target, timeout_ms: int = 5000) -> None:
    """等加载动画结束、背景图可见, 避免读到空图或上一张图的残留。"""
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
    raise TimeoutError("yidun puzzle background image not visible")


async def _bg_src(target) -> str | None:
    try:
        locator = await _visible_locator(target, YIDUN_BG_SELECTOR)
        if locator is not None:
            return await locator.get_attribute("src")
    except Exception:
        return None
    return None


async def _visible_tip(target) -> str:
    try:
        tips = target.locator(YIDUN_TIPS_SELECTOR)
        texts = []
        for index in range(await tips.count()):
            tip = tips.nth(index)
            if await tip.is_visible():
                text = " ".join((await tip.inner_text()).split())
                if text:
                    texts.append(text)
        return "; ".join(texts)
    except Exception:
        return ""


async def _wait_verify_result(target, timeout_ms: int = 3500) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if not await _puzzle_visible(target):
            return True
        if is_failure_tip(await _visible_tip(target)):
            return False
        await asyncio.sleep(0.15)
    return False


async def _refresh_puzzle(target) -> None:
    """点击刷新换一张拼图, 并等待新图加载出来。"""
    old_src = await _bg_src(target)
    clicked = False
    for locator in (target.locator(YIDUN_REFRESH_SELECTOR), target.get_by_text("刷新", exact=True)):
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
        if not await _puzzle_visible(target):
            return
        src = await _bg_src(target)
        if src and src != old_src:
            break
    await _wait_puzzle_ready(target)


def _match_yidun_gap(bg_img, block_img, cv2, np=None):
    """在后台线程执行 OpenCV 边缘提取与模板匹配，避免阻塞 asyncio 事件循环。"""
    if np is None:
        import numpy as _np

        np = _np
    bg_edges = process_background_image(bg_img, cv2=cv2)
    block_edges = process_block_image(block_img, cv2=cv2, np=np)
    if (
        block_edges.shape[0] > bg_edges.shape[0]
        or block_edges.shape[1] > bg_edges.shape[1]
    ):
        raise ValueError("yidun block larger than background")
    if int(np.count_nonzero(block_edges)) == 0:
        return 0.0, (0, 0)
    result = cv2.matchTemplate(bg_edges, block_edges, cv2.TM_CCOEFF_NORMED)
    _, score, _, max_loc = cv2.minMaxLoc(result)
    return float(score), max_loc


def gap_to_slider_distance(
    gap_display_x: float,
    bg_box: dict[str, float],
    block_box: dict[str, float] | None = None,
    track_box: dict[str, float] | None = None,
    slider_box: dict[str, float] | None = None,
    *,
    fallback_bias: float = GAP_BIAS_PX,
) -> tuple[float, bool]:
    """根据实时 DOM bounding_box 将缺口 CSS 坐标换算为滑块拖动距离。

    返回 ``(drag_distance, used_dom_geometry)``:
    - 当背景、拼图块、轨道与滑块几何尺寸均有效时，扣减拼图初始偏移后按
      ``(track_width - slider_width) / (bg_width - block_width)`` 换算滑块位移；
    - 当任意 DOM 盒子缺失或比例超出合理区间时，回退至 ``jigsaw_delta + fallback_bias``。
    """
    bg_width = float(bg_box.get("width") or 0.0)
    bg_x = float(bg_box.get("x") or 0.0)
    piece_offset = 0.0
    if block_box is not None:
        raw_offset = float(block_box.get("x", bg_x)) - bg_x
        if 0.0 <= raw_offset <= YIDUN_PIECE_OFFSET_LIMIT_PX and (
            bg_width <= 0 or raw_offset < bg_width * 0.4
        ):
            piece_offset = raw_offset

    jigsaw_delta = max(0.0, float(gap_display_x) - piece_offset)

    if (
        bg_width > 0
        and block_box is not None
        and track_box is not None
        and slider_box is not None
    ):
        block_width = float(block_box.get("width") or 0.0)
        track_width = float(track_box.get("width") or 0.0)
        slider_width = float(slider_box.get("width") or 0.0)
        track_x = float(track_box.get("x") or 0.0)
        slider_x = float(slider_box.get("x", track_x))
        raw_inset = slider_x - track_x
        slider_inset = (
            raw_inset
            if 0.0 < raw_inset <= YIDUN_PIECE_OFFSET_LIMIT_PX
            and track_width > 0
            and raw_inset < track_width * 0.2
            else 0.0
        )
        max_jigsaw = bg_width - block_width
        max_slider = track_width - slider_width - 2.0 * slider_inset
        if max_slider <= 0:
            max_slider = track_width - slider_width
        if (
            block_width > 0
            and slider_width > 0
            and max_jigsaw > 0
            and max_slider > 0
            and track_width >= slider_width * 2
        ):
            ratio = max_slider / max_jigsaw
            if 0.5 <= ratio <= 1.5:
                distance = min(max_slider, max(0.0, jigsaw_delta * ratio))
                return round(distance, 3), True

    return round(max(0.0, jigsaw_delta + float(fallback_bias)), 3), False


async def _optional_box(target, selector: str) -> dict[str, float] | None:
    try:
        locator = await _visible_locator(target, selector)
        if locator is None:
            return None
        return await locator.bounding_box()
    except Exception:
        return None


async def _measure_yidun_puzzle(page: Page, modules: list[ModuleType] | None = None):
    if modules is not None:
        np_mod, cv2_mod = modules
    else:
        import cv2 as cv2_mod
        import numpy as np_mod

    # 等待易盾验证码加载完成，避免读取到空图片或旧图片。
    loading = page.locator("div.yidun--loading").first
    try:
        if await loading.count() and await loading.is_visible():
            await loading.wait_for(state="hidden", timeout=5000)
    except Exception:
        pass

    # OpenCV 处理的是原图像素，浏览器拖动使用 CSS 显示像素。
    bg_locator = await _visible_locator(page, YIDUN_BG_SELECTOR)
    block_locator = await _visible_locator(page, YIDUN_BLOCK_SELECTOR)
    if bg_locator is None or block_locator is None:
        raise TimeoutError("yidun puzzle not visible")
    block_url = await block_locator.get_attribute("src")
    bg_url = await bg_locator.get_attribute("src")
    bg_box = await bg_locator.bounding_box()
    if not bg_url or not block_url or not bg_box:
        raise TimeoutError("yidun puzzle images not ready")
    try:
        block_box = await block_locator.bounding_box()
    except Exception:
        block_box = None
    track_box = await _optional_box(page, YIDUN_TRACK_SELECTOR)
    slider_box = await _optional_box(page, YIDUN_SLIDER_SELECTOR)
    image_size = await bg_locator.evaluate(
        "image => ({width: image.naturalWidth, height: image.naturalHeight})"
    )

    browser_page = _page_of(page)
    bg_img = await download_image(
        bg_url, browser_page, np=np_mod, cv2=cv2_mod, unchanged=False
    )
    block_img = await download_image(
        block_url, browser_page, np=np_mod, cv2=cv2_mod, unchanged=True
    )
    if bg_img is None or block_img is None:
        raise TimeoutError("yidun puzzle decode failed")

    # 匹配结果仍是原图坐标，后面必须转换到浏览器显示坐标。
    score, max_loc = await asyncio.to_thread(
        _match_yidun_gap, bg_img, block_img, cv2_mod, np_mod
    )
    if score < 0.05:
        raise TimeoutError(f"yidun puzzle match too weak: {score:.3f}")

    natural_width = image_size.get("width") or bg_img.shape[1]
    natural_height = image_size.get("height") or bg_img.shape[0]
    display_point = image_to_display_point(
        max_loc,
        natural_width,
        natural_height,
        bg_box["width"],
        bg_box["height"],
    )
    if not -2 <= display_point[0] <= bg_box["width"] - 4:
        raise TimeoutError(f"yidun puzzle offset out of range: {display_point[0]:.1f}")

    drag_distance, used_dom = gap_to_slider_distance(
        display_point[0],
        bg_box,
        block_box,
        track_box,
        slider_box,
    )
    return (
        round(display_point[0], 3),
        round(display_point[1], 3),
        drag_distance,
        used_dom,
        float(score),
    )


# 主函数，结合页面加载和图片处理
async def progress_img(page: Page, modules: list[ModuleType] | None = None):
    gap_x, gap_y, _, _, _ = await _measure_yidun_puzzle(page, modules=modules)
    return gap_x, gap_y


async def move_slider(page: Page, distance, bias=GAP_BIAS_PX):
    """按浏览器显示坐标拖动滑块。"""
    slider_el = await _visible_locator(page, YIDUN_SLIDER_SELECTOR)
    if slider_el is None:
        raise TimeoutError("yidun slider not found")
    total_distance = max(0.0, float(distance) + float(bias))
    await drag_element_horizontally(
        page,
        slider_el,
        total_distance,
        approach_range=(40.0, 80.0),
        not_found_message="yidun slider not found",
    )


async def solve_yidun(
    target,
    modules: list[ModuleType],
    max_attempts: int = MAX_ATTEMPTS,
) -> bool:
    for attempt in range(1, max_attempts + 1):
        try:
            await _wait_puzzle_ready(target)
            gap_x, _, drag_distance, used_dom, score = await _measure_yidun_puzzle(
                target, modules=modules
            )
            if used_dom:
                probe = (0.0, 2.0, -2.0, 4.0)[min(attempt - 1, 3)]
                clamped_distance = max(0.0, float(drag_distance) + probe)
                logger.debug(
                    f"滑块缺口识别: x={gap_x:.1f}px, DOM换算位移={drag_distance:.1f}px, "
                    f"微调={probe:+.1f}px, 实际位移={clamped_distance:.1f}px, score={score:.3f}"
                )
                logger.info(f"第{attempt}次尝试过滑块验证...")
                await move_slider(target, clamped_distance, bias=0.0)
            else:
                # 无法获取完整 DOM 轨道尺寸时保留 piece_offset 扣除后的基线位移再叠加回退偏移。
                base_distance = max(0.0, float(drag_distance) - float(GAP_BIAS_PX))
                bias = (
                    GAP_BIAS_PX
                    if attempt == 1
                    else GAP_BIAS_PX + random.choice((-2, 0, 2))
                )
                logger.debug(
                    f"滑块缺口识别: x={gap_x:.1f}px, 基线位移={base_distance:.1f}px, "
                    f"回退补偿={bias}px, score={score:.3f}"
                )
                logger.info(f"第{attempt}次尝试过滑块验证...")
                await move_slider(target, base_distance, bias)
            if await _wait_verify_result(target):
                logger.info("滑块验证已成功通过.")
                logger.event("滑块验证", 结果="通过", 尝试=attempt, 缺口=f"{gap_x:.1f}px")
                return True
            if not await _puzzle_visible(target):
                logger.info("滑块验证已通过.")
                logger.event("滑块验证", 结果="通过", 尝试=attempt, 缺口=f"{gap_x:.1f}px")
                return True
            logger.warn(f"第{attempt}次滑块验证未通过,正在刷新拼图...")
        except TimeoutError as exc:
            logger.debug(f"滑块识别超时: {exc}")
        except Exception as exc:
            logger.log_exception("滑块验证异常.", exc)
        if attempt < max_attempts:
            await _refresh_puzzle(target)
    logger.warn("自动过滑块验证失败,请手动验证!")
    logger.event("滑块验证", 结果="失败", 尝试=max_attempts)
    return False


async def slider_verify(
    page: Page,
    modules: list[ModuleType],
    appear_timeout_ms: int = LOGIN_APPEAR_TIMEOUT_MS,
    max_attempts: int = MAX_ATTEMPTS,
) -> bool:
    """兼容入口：委托给 captcha_router.solve_slider_captcha 统一调度易盾与腾讯滑块。"""
    from modules.captcha_router import solve_slider_captcha

    return await solve_slider_captcha(
        page,
        modules,
        appear_timeout_ms=appear_timeout_ms,
        max_attempts=max_attempts,
    )
