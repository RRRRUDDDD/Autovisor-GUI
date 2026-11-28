# encoding=utf-8
"""统一验证码路由器：自顶向下编排易盾滑块、易盾VLM点选、腾讯滑块与腾讯点选，彻底消除模块间循环导入。"""
from __future__ import annotations

from types import ModuleType

from playwright.async_api import Page, TimeoutError

from modules import click_captcha, slider, tencent_slider, yidun_click
from modules.logger import Logger

logger = Logger()

SLIDER_APPEAR_SELECTOR = (
    f"{slider.YIDUN_PUZZLE_SELECTOR}, iframe[id^='tcaptcha'], iframe.tcaptcha-iframe, "
    "iframe[src*='captcha.qq.com'], [id^='tcaptcha_transform']"
)


async def is_tencent_slider_target(target) -> bool:
    """判断当前 tcaptcha 容器是否为滑块形态（供点选模块让出控制权）。"""
    return await tencent_slider.has_slider_thumb(target)


async def captcha_visible(page: Page) -> bool:
    """页面是否存在任意形态的易盾或腾讯验证码浮层。"""
    if await slider.yidun_captcha_visible(page):
        return True
    return await tencent_slider.tencent_captcha_visible(page)


async def resolve_slider_target(page: Page):
    """返回 (provider, target)；易盾与腾讯滑块都未出现时返回 (None, None)。"""
    if await yidun_click.resolve_yidun_click_target(page) is not None:
        return None, None
    target = await slider.resolve_captcha_target(page)
    if target is not None:
        return "yidun", target
    target = await tencent_slider.resolve_tencent_slider_target(page)
    if target is not None:
        return "tencent", target
    return None, None


async def describe_other_mode(page: Page) -> str | None:
    """识别暂不支持自动滑块的易盾或腾讯验证码形态。"""
    label = await slider.describe_unsupported_mode(page)
    if label:
        return f"易盾「{label}」"
    target = await tencent_slider.resolve_tencent_captcha_target(page)
    if target is None:
        return None
    label = await tencent_slider.describe_tencent_mode(target)
    return f"腾讯「{label}」" if label else None


async def solve_slider_captcha(
    page: Page,
    modules: list[ModuleType] | None,
    appear_timeout_ms: int = slider.LOGIN_APPEAR_TIMEOUT_MS,
    max_attempts: int = slider.MAX_ATTEMPTS,
) -> bool:
    """统一入口：等待并解决易盾（滑块/VLM点选）或腾讯滑块验证。"""
    click_attempts = min(max(1, int(max_attempts)), yidun_click.MAX_ATTEMPTS)
    if await yidun_click.resolve_yidun_click_target(page) is not None:
        return await yidun_click.solve_yidun_click(page, max_attempts=click_attempts)

    np_mod, cv2_mod = modules if modules and len(modules) >= 2 else (None, None)
    provider, target = await resolve_slider_target(page)
    if target is None:
        label = await describe_other_mode(page)
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
        if await yidun_click.resolve_yidun_click_target(page) is not None:
            return await yidun_click.solve_yidun_click(
                page, max_attempts=click_attempts
            )
        provider, target = await resolve_slider_target(page)
        if target is None:
            label = await describe_other_mode(page)
            if label:
                logger.warn(
                    f"检测到{label}验证,只能自动通过滑块拼图,请手动完成.", shift=True
                )
                logger.event("滑块验证", 结果="不支持", 类型=label)
            return False

    if not cv2_mod or not np_mod:
        logger.warn("OpenCV或Numpy导入失败,无法开启自动滑块验证.")
        return False

    if provider == "tencent":
        return await tencent_slider.solve_tencent_slider(
            target, modules, max_attempts=max_attempts
        )
    return await slider.solve_yidun(target, modules, max_attempts=max_attempts)


async def solve_any_captcha(
    page: Page,
    modules: list[ModuleType] | None,
    *,
    appear_timeout_ms: int = slider.IN_COURSE_APPEAR_TIMEOUT_MS,
    max_attempts: int = slider.MAX_ATTEMPTS,
) -> bool:
    """按优先级自动路由点选验证码（易盾VLM/腾讯）与滑块验证码。"""
    if await yidun_click.resolve_yidun_click_target(page) is not None:
        click_attempts = min(max(1, int(max_attempts)), yidun_click.MAX_ATTEMPTS)
        return await yidun_click.solve_yidun_click(page, max_attempts=click_attempts)
    if await click_captcha.resolve_click_captcha_target(page) is not None:
        if await click_captcha.solve_click_captcha(page, modules, attempts=max_attempts):
            return True
    return await solve_slider_captcha(
        page,
        modules,
        appear_timeout_ms=appear_timeout_ms,
        max_attempts=max_attempts,
    )
