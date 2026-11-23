from __future__ import annotations

import asyncio
import random
import time
from types import ModuleType

import requests
from playwright.async_api import Page
from playwright._impl._errors import TimeoutError

from modules.logger import Logger

YIDUN_PUZZLE_SELECTOR = ".yidun_bgimg, img.yidun_bg-img"
YIDUN_BG_SELECTOR = "img.yidun_bg-img"
YIDUN_REFRESH_SELECTOR = ".yidun_refresh, .yidun_icon-refresh"
YIDUN_TIPS_SELECTOR = ".yidun_tips__text"
# 易盾除拼图外还有图标/文字点选、短信验证等形态, 这些不归本模块处理。
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
# 模板匹配给出的是拼图条带左边缘, 实测鼠标位移要比缺口坐标再多约 16px 才落在
# 易盾判定窗口中央(偏 0 或 +32 均失败), 组件尺寸变化时需要重新校准。
GAP_BIAS_PX = 16
MAX_ATTEMPTS = 4
FAILURE_WORDS = ("失败", "重试", "错误")

# 定义全局变量
cv2: ModuleType
np: ModuleType
logger = Logger()


def image_to_display_point(point, image_width, image_height, display_width, display_height):
    """将 OpenCV 原图像素坐标转换为浏览器 CSS 显示坐标。"""
    return (
        point[0] * display_width / image_width,
        point[1] * display_height / image_height,
    )


async def download_image(url, page=None):
    """下载验证码图片; 优先走浏览器请求以复用 Cookie 和 UA。"""
    data = None
    if page is not None:
        try:
            response = await page.request.get(url, timeout=15000)
            if response.ok:
                data = await response.body()
        except Exception:
            data = None
    if data is None:
        data = await asyncio.to_thread(lambda: requests.get(url, timeout=15).content)
    # 图片字节需要先转为 NumPy 数组，再交给 OpenCV 解码。
    image_array = np.frombuffer(data, np.uint8)
    return cv2.imdecode(image_array, cv2.IMREAD_COLOR)


# 对背景图提取边缘，减少颜色和纹理对匹配的干扰。
def process_background_image(image):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    denoised = cv2.fastNlMeansDenoising(gray, None, 10, 7, 21)
    _, binary = cv2.threshold(denoised, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    edges = cv2.Canny(binary, 500, 900, apertureSize=3)
    return edges


def process_block_image(image):
    # 拼图块亮度方向与背景不同，因此先反色再提取边缘。
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    inverted = cv2.bitwise_not(gray)
    _, binary = cv2.threshold(inverted, 240, 255, cv2.THRESH_BINARY_INV)
    edges = cv2.Canny(binary, 500, 900, apertureSize=3)
    return edges


def _mouse(target):
    mouse = getattr(target, "mouse", None)
    if mouse is not None:
        return mouse
    return target.page.mouse


def _page_of(target):
    return getattr(target, "page", target)


async def resolve_captcha_target(page: Page):
    """返回包含易盾拼图的 page/frame; 找不到时返回 None。"""
    if await page.locator(YIDUN_PUZZLE_SELECTOR).count() > 0:
        return page
    for frame in getattr(page, "frames", []):
        try:
            if await frame.locator(YIDUN_PUZZLE_SELECTOR).count() > 0:
                return frame
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
    targets = [page, *getattr(page, "frames", [])]
    for target in targets:
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
    locator = target.locator(selector)
    count = await locator.count()
    for index in range(count):
        candidate = locator.nth(index)
        try:
            if await candidate.is_visible():
                return candidate
        except Exception:
            continue
    return locator.first if count else None


async def _puzzle_visible(target) -> bool:
    try:
        puzzle = target.locator(YIDUN_BG_SELECTOR)
        for index in range(await puzzle.count()):
            if await puzzle.nth(index).is_visible():
                return True
    except Exception:
        return False
    return False


async def captcha_visible(page: Page) -> bool:
    """登录页是否仍有验证码浮层(含暂不支持的易盾形态与腾讯验证码)。"""
    if await _puzzle_visible(page):
        return True
    for frame in getattr(page, "frames", []):
        try:
            if await _puzzle_visible(frame):
                return True
        except Exception:
            continue
    if await describe_unsupported_mode(page):
        return True
    from modules.tencent_slider import tencent_captcha_visible

    return await tencent_captcha_visible(page)


async def _wait_puzzle_ready(target, timeout_ms: int = 5000) -> None:
    """等加载动画结束、背景图可见, 避免读到空图或上一张图的残留。"""
    try:
        loading = target.locator("div.yidun--loading, div.yidun_loadicon").first
        if await loading.count() and await loading.is_visible():
            await loading.wait_for(state="hidden", timeout=timeout_ms)
    except Exception:
        pass
    await target.locator(YIDUN_BG_SELECTOR).first.wait_for(state="visible", timeout=timeout_ms)


async def _bg_src(target) -> str | None:
    try:
        locator = target.locator(YIDUN_BG_SELECTOR).first
        if await locator.count():
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


def is_failure_tip(text: str) -> bool:
    if not text:
        return False
    # 初始提示「向右拖动滑块填充拼图」包含"滑块"但不算失败。
    return any(word in text for word in FAILURE_WORDS)


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
            if not await locator.count():
                continue
            element = locator.first
            if not await element.is_visible():
                continue
            await element.click(timeout=1500)
            clicked = True
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


# 主函数，结合页面加载和图片处理
async def progress_img(page: Page):
    # 等待易盾验证码加载完成，避免读取到空图片或旧图片。
    loading = page.locator("div.yidun--loading").first
    try:
        if await loading.count() and await loading.is_visible():
            await loading.wait_for(state="hidden", timeout=5000)
    except Exception:
        pass

    # OpenCV 处理的是原图像素，浏览器拖动使用 CSS 显示像素。
    bg_locator = await _visible_locator(page, YIDUN_BG_SELECTOR)
    block_locator = await _visible_locator(page, "img.yidun_jigsaw")
    if bg_locator is None or block_locator is None:
        raise TimeoutError("yidun puzzle not visible")
    block_url = await block_locator.get_attribute("src")
    bg_url = await bg_locator.get_attribute("src")
    bg_box = await bg_locator.bounding_box()
    if not bg_url or not block_url or not bg_box:
        raise TimeoutError("yidun puzzle images not ready")
    image_size = await bg_locator.evaluate(
        'image => ({width: image.naturalWidth, height: image.naturalHeight})'
    )

    browser_page = _page_of(page)
    bg_img = await download_image(bg_url, browser_page)
    block_img = await download_image(block_url, browser_page)
    if bg_img is None or block_img is None:
        raise TimeoutError("yidun puzzle decode failed")

    # 分别处理背景图和拼图块。
    bg_edges = process_background_image(bg_img)
    block_edges = process_block_image(block_img)

    # 匹配结果仍是原图坐标，后面必须转换到浏览器显示坐标。
    result = cv2.matchTemplate(bg_edges, block_edges, cv2.TM_CCOEFF_NORMED)
    _, score, _, max_loc = cv2.minMaxLoc(result)
    if score < 0.05:
        raise TimeoutError(f"yidun puzzle match too weak: {score:.3f}")

    # 例如当前页面是 480x240 原图显示为 400x200，缩放比例为 0.8333。
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
    return round(display_point[0], 3), round(display_point[1], 3)


def gen_track(distance, duration_ms=None, rng=random):
    """生成拟人滑动轨迹: 先加速后减速, 末端轻微过冲再回正。

    返回 [(dx, dy, delay_ms)], dx 之和恰好等于 distance。
    """
    distance = float(distance)
    if duration_ms is None:
        duration_ms = rng.uniform(520, 780) + min(220.0, abs(distance) * 0.35)
    steps = max(10, min(46, int(abs(distance) / 2.4) + rng.randint(6, 12)))
    overshoot = rng.uniform(1.5, 3.5)

    points = []
    for index in range(1, steps + 1):
        t = index / steps
        points.append(distance * (1 - (1 - t) ** 3))
    points[-1] = distance + overshoot
    for index in range(1, 4):
        points.append(distance + overshoot * (1 - index / 3))

    track = []
    previous = 0.0
    for index, position in enumerate(points):
        dx = position - previous
        previous = position
        dy = 0.0 if index == 0 or index == len(points) - 1 else rng.uniform(-1.2, 1.2)
        delay = (duration_ms / len(points)) * rng.uniform(0.7, 1.35)
        track.append((dx, dy, max(6.0, delay)))
    return track


async def move_slider(page: Page, distance, bias=GAP_BIAS_PX):
    """按浏览器显示坐标拖动滑块。

    bias 是易盾判定的经验补偿(缺口坐标 -> 鼠标位移), 随组件版本变化需重新校准。
    轨迹带逐步延时和纵向抖动, 避免瞬移轨迹被行为检测判失败。
    """
    slider = await _visible_locator(page, "div.yidun_slider")
    if slider is None:
        raise TimeoutError("yidun slider not found")
    box = await slider.bounding_box()
    if not box:
        raise TimeoutError("yidun slider not found")

    mouse = _mouse(page)
    start_x = box["x"] + box["width"] / 2
    start_y = box["y"] + box["height"] / 2
    # 先把鼠标自然移到按钮附近, 再对准中心和按下, 让行为轨迹像真人。
    await mouse.move(start_x - random.uniform(40, 80), start_y + random.uniform(-6, 6))
    await asyncio.sleep(random.uniform(0.08, 0.16))
    await mouse.move(start_x, start_y)
    await asyncio.sleep(random.uniform(0.10, 0.20))
    await mouse.down()
    await asyncio.sleep(random.uniform(0.08, 0.16))

    x = start_x
    for dx, dy, delay in gen_track(distance + bias):
        x += dx
        await mouse.move(x, start_y + dy)
        await asyncio.sleep(delay / 1000)
    await asyncio.sleep(random.uniform(0.08, 0.15))
    await mouse.up()


async def _resolve_slider_target(page: Page):
    """返回 (provider, target); 易盾与腾讯滑块都未出现时返回 (None, None)。"""
    target = await resolve_captcha_target(page)
    if target is not None:
        return "yidun", target
    from modules.tencent_slider import resolve_tencent_slider_target

    target = await resolve_tencent_slider_target(page)
    if target is not None:
        return "tencent", target
    return None, None


async def _describe_other_mode(page: Page) -> str | None:
    label = await describe_unsupported_mode(page)
    if label:
        return f"易盾「{label}」"
    from modules.tencent_slider import describe_tencent_mode, resolve_tencent_captcha_target

    target = await resolve_tencent_captcha_target(page)
    if target is None:
        return None
    label = await describe_tencent_mode(target)
    return f"腾讯「{label}」" if label else None


async def _solve_yidun(target, max_attempts: int) -> bool:
    for attempt in range(1, max_attempts + 1):
        try:
            await _wait_puzzle_ready(target)
            gap_x, _ = await progress_img(target)
            # 重试时轻微左右试探, 规避识别端与判定窗口的系统性偏差。
            bias = GAP_BIAS_PX if attempt == 1 else GAP_BIAS_PX + random.choice((-2, 0, 2))
            logger.debug(f"滑块缺口识别: x={gap_x:.1f}px, 拖动补偿={bias}px")
            logger.info(f"第{attempt}次尝试过滑块验证...")
            await move_slider(target, gap_x, bias)
            if await _wait_verify_result(target):
                logger.info("滑块验证已成功通过.")
                logger.event("滑块验证", 结果="通过", 尝试=attempt, 缺口=f"{gap_x:.1f}px")
                return True
            if not await _puzzle_visible(target):
                # 判定超时与浮层关闭同时发生时, 以关闭为准。
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
    global cv2, np
    np, cv2 = modules
    if not cv2 or not np:
        logger.warn("OpenCV或Numpy导入失败,无法开启自动滑块验证.")
        return False

    provider, target = await _resolve_slider_target(page)
    if target is None:
        label = await _describe_other_mode(page)
        if label:
            logger.warn(f"检测到{label}验证,只能自动通过滑块拼图,请手动完成.", shift=True)
            logger.event("滑块验证", 结果="不支持", 类型=label)
            return False
        logger.debug("等待登录滑块出现...")
        try:
            await page.wait_for_selector(
                SLIDER_APPEAR_SELECTOR, state="visible", timeout=appear_timeout_ms
            )
        except TimeoutError:
            return False
        provider, target = await _resolve_slider_target(page)
        if target is None:
            label = await _describe_other_mode(page)
            if label:
                logger.warn(
                    f"检测到{label}验证,只能自动通过滑块拼图,请手动完成.", shift=True
                )
                logger.event("滑块验证", 结果="不支持", 类型=label)
            return False

    if provider == "tencent":
        from modules.tencent_slider import solve_tencent_slider

        return await solve_tencent_slider(target, modules, max_attempts=max_attempts)
    return await _solve_yidun(target, max_attempts)