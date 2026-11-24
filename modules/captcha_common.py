# encoding=utf-8
"""验证码公共基础层：目标遍历、可见元素定位、代理感知图片下载、坐标换算与拟人拖拽。"""
from __future__ import annotations

import asyncio
import base64
import random

import requests
from playwright.async_api import Page, TimeoutError

from modules.http_utils import build_requests_proxies

FAILURE_WORDS = ("失败", "重试", "错误")

_FETCH_IMAGE_JS = """async (url) => {
    const resp = await fetch(url);
    const buffer = await resp.arrayBuffer();
    const bytes = new Uint8Array(buffer);
    let binary = '';
    for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
    return btoa(binary);
}"""

_RUNTIME_PROXY = ""


def set_captcha_proxy(proxy: str | None) -> None:
    """设置验证码回退下载使用的代理地址。"""
    global _RUNTIME_PROXY
    _RUNTIME_PROXY = str(proxy or "").strip()


def _effective_proxy(proxy: str | None = None) -> str:
    explicit = str(proxy or "").strip()
    if explicit:
        return explicit
    if _RUNTIME_PROXY:
        return _RUNTIME_PROXY
    try:
        from modules import llm

        return str(getattr(llm, "_RUNTIME_PROXY", "") or "").strip()
    except Exception:
        return ""


def is_failure_tip(text: str) -> bool:
    """判断提示文案是否表示验证失败（初始引导提示不算失败）。"""
    if not text:
        return False
    return any(word in text for word in FAILURE_WORDS)


def get_mouse(target):
    """返回 page 或 frame 所属页面的 mouse 控制器。"""
    mouse = getattr(target, "mouse", None)
    if mouse is not None:
        return mouse
    return target.page.mouse


def page_of(target):
    """返回 frame 所属的 Page，或直接返回 Page 本身。"""
    return getattr(target, "page", target)


def iter_targets(page: Page):
    """遍历主页面及所有子 frame（跳过重复的 main_frame）。"""
    yield page
    main_frame = getattr(page, "main_frame", None)
    for frame in getattr(page, "frames", []):
        if frame is page or frame is main_frame:
            continue
        if main_frame is None and getattr(frame, "parent_frame", None) is None:
            continue
        yield frame


async def visible_locator(target, selector: str, *, fallback_first: bool = False):
    """返回选择器下首个可见元素；未找到时按 fallback_first 决定回退 first 或 None。"""
    try:
        locator = target.locator(selector)
        count = await locator.count()
    except Exception:
        return None
    for index in range(count):
        candidate = locator.nth(index)
        try:
            if await candidate.is_visible():
                return candidate
        except Exception:
            continue
    if fallback_first and count:
        return locator.first
    return None


def image_to_display_point(
    point,
    image_width: float,
    image_height: float,
    display_width: float,
    display_height: float,
) -> tuple[float, float]:
    """将 OpenCV 原图像素坐标转换为浏览器 CSS 显示坐标。"""
    return (
        point[0] * display_width / image_width,
        point[1] * display_height / image_height,
    )


def gen_track(distance: float, duration_ms: float | None = None, rng=random) -> list[tuple[float, float, float]]:
    """生成拟人滑动轨迹：先加速后减速，末端轻微过冲再回正。

    返回 [(dx, dy, delay_ms)]，dx 之和精确等于 distance。
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


async def drag_element_horizontally(
    target,
    element,
    distance: float,
    *,
    approach_range: tuple[float, float] = (40.0, 80.0),
    not_found_message: str = "slider element not found",
) -> None:
    """以拟人轨迹水平拖动指定滑块元素。"""
    box = await element.bounding_box()
    if not box:
        raise TimeoutError(not_found_message)
    mouse = get_mouse(target)
    start_x = box["x"] + box["width"] / 2
    start_y = box["y"] + box["height"] / 2
    await mouse.move(
        start_x - random.uniform(*approach_range),
        start_y + random.uniform(-6, 6),
    )
    await asyncio.sleep(random.uniform(0.08, 0.16))
    await mouse.move(start_x, start_y)
    await asyncio.sleep(random.uniform(0.10, 0.20))
    await mouse.down()
    await asyncio.sleep(random.uniform(0.08, 0.16))

    x = start_x
    for dx, dy, delay in gen_track(distance):
        x += dx
        await mouse.move(x, start_y + dy)
        await asyncio.sleep(delay / 1000)
    await asyncio.sleep(random.uniform(0.08, 0.15))
    await mouse.up()


def _download_bytes_requests(url: str, *, proxy: str | None = None) -> bytes:
    proxies = build_requests_proxies(_effective_proxy(proxy))
    kwargs = {"timeout": 15}
    if proxies:
        kwargs["proxies"] = proxies
    return requests.get(url, **kwargs).content


async def fetch_image_bytes(
    target,
    url: str,
    *,
    allow_eval_fetch: bool = False,
    proxy: str | None = None,
) -> bytes | None:
    """代理感知地下载验证码图片：优先浏览器上下文请求，其次 iframe 同源 fetch，最后带代理 requests 回退。"""
    if not url:
        return None
    page = page_of(target) if target is not None else None
    data: bytes | None = None
    if page is not None and not url.startswith("blob:"):
        try:
            response = await page.request.get(url, timeout=15000)
            if response.ok:
                data = await response.body()
        except Exception:
            data = None
    if data is None and allow_eval_fetch and target is not None:
        try:
            encoded = await target.evaluate(_FETCH_IMAGE_JS, url)
            data = base64.b64decode(encoded)
        except Exception:
            data = None
    if data is None and not url.startswith("blob:"):
        try:
            data = await asyncio.to_thread(_download_bytes_requests, url, proxy=proxy)
        except Exception:
            return None
    return data


def decode_image_bytes(data: bytes | None, np, cv2, *, unchanged: bool = False):
    """将图片原始字节解码为 OpenCV 图像矩阵。"""
    if not data:
        return None
    flag = cv2.IMREAD_UNCHANGED if unchanged else cv2.IMREAD_COLOR
    array = np.frombuffer(data, np.uint8)
    return cv2.imdecode(array, flag)
