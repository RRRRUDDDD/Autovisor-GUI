# encoding=utf-8
"""登录认证流、Cookies 变更检测持久化、二维码登录与终端凭据引导。"""
from __future__ import annotations

import asyncio
import getpass
import re
import sys
import time

from playwright.async_api import BrowserContext, Page, TimeoutError

from modules.logger import Logger
from modules.login import (
    describe_login_error,
    is_login_page,
    perform_password_login,
    wait_for_login_complete,
)
from modules.qr_login import qr_login_for_cookies
from modules.runtime_bootstrap import RunContext
from modules.slider import captcha_visible, slider_verify
from modules.utils import (
    clear_cookies,
    filter_zhihuishu_cookies,
    get_runtime_path,
    save_cookies,
)
from modules.zhs_api import SessionStatus, ZhsApiClient

# 运行期间周期性落盘 Cookies 的间隔(秒): 服务器中途续期后, 崩溃/断电也不丢凭证
COOKIE_SYNC_INTERVAL_SECONDS = 300

_logger = Logger()
_saved_cookies_sig = None


def _resolve_av_attr(name: str, fallback):
    mod = sys.modules.get("Autovisor")
    if mod is not None and hasattr(mod, name):
        return getattr(mod, name)
    return fallback


def _get_logger():
    return _resolve_av_attr("logger", _logger)


def _get_cookie_path(ctx: RunContext | None = None) -> str:
    if ctx is not None and ctx.cookie_path:
        return ctx.cookie_path
    return _resolve_av_attr("COOKIE_PATH", get_runtime_path("data", "cookies.json"))


def _cookies_signature(cookies):
    if not cookies:
        return None
    return frozenset(
        (
            cookie.get("name"),
            cookie.get("domain"),
            cookie.get("path"),
            cookie.get("value"),
        )
        for cookie in cookies
    )


def remember_login_cookies(cookies, *, ctx: RunContext | None = None) -> None:
    """记录已经落盘的凭证, 用于跳过重复写入。"""
    global _saved_cookies_sig
    sig = _cookies_signature(cookies)
    _saved_cookies_sig = sig
    if ctx is not None:
        ctx.saved_cookies_sig = sig


async def persist_login_cookies(
    context: BrowserContext, *, ctx: RunContext | None = None
) -> None:
    """凭证有变化时才写盘: 登录完成、Cookie 续期后立即保存, 中断也不丢。"""
    global _saved_cookies_sig
    log = _get_logger()
    cookie_path = _get_cookie_path(ctx)
    save_fn = _resolve_av_attr("save_cookies", save_cookies)
    # 按域名筛选而非固定 URL 列表, 新子域下发 cookies 也能收进来
    cookies = filter_zhihuishu_cookies(await context.cookies())
    signature = _cookies_signature(cookies)
    prev_sig = ctx.saved_cookies_sig if ctx is not None else _saved_cookies_sig
    if signature is None or signature == prev_sig:
        return
    try:
        save_fn(cookies, cookie_path)
    except Exception as exc:
        log.log_exception("保存登录 Cookies 失败.", exc)
        return
    _saved_cookies_sig = signature
    if ctx is not None:
        ctx.saved_cookies_sig = signature
    log.event("保存登录凭证", 条数=len(cookies), 文件=cookie_path)


async def periodic_cookie_sync(
    context: BrowserContext, *, ctx: RunContext | None = None
) -> None:
    """运行期间周期性落盘 Cookies, 与 persist_login_cookies 共用变更检测, 无变化时不写盘。"""
    while True:
        await asyncio.sleep(COOKIE_SYNC_INTERVAL_SECONDS)
        try:
            await persist_login_cookies(context, ctx=ctx)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _get_logger().log_exception("周期保存登录 Cookies 失败.", exc)


async def report_login_error_later(page: Page) -> None:
    """提交账号密码后若仍停留在登录页, 收集并输出页面错误提示。"""
    log = _get_logger()
    await page.wait_for_timeout(6000)
    if not is_login_page(page.url):
        return
    if await captcha_visible(page):
        # 验证码还开着时提示"登录未完成"会误导, 交给滑块任务继续处理。
        return
    detail = await describe_login_error(page)
    if detail:
        log.warn(f"登录未完成, 页面提示: {detail}", shift=True)
    else:
        log.warn("登录未完成, 请检查账号密码是否正确, 或在浏览器中手动登录.", shift=True)


async def auto_login(
    context: BrowserContext,
    page: Page,
    config,
    modules=None,
    *,
    ctx: RunContext | None = None,
) -> None:
    log = _get_logger()
    cookie_path = _get_cookie_path(ctx)
    wait_start = time.time()
    if not is_login_page(page.url):
        await page.goto(config.login_url, wait_until="domcontentloaded")
    if not is_login_page(page.url):
        log.info("检测到已登录,跳过登录步骤.")
        return

    error_task = None
    if config.username and config.password:
        try:
            log.event("自动登录", 方式="账号密码", 账号="已填写")
            await perform_password_login(page, config.username, config.password)
            log.info("已自动填写账号密码并提交登录表单.")
            error_task = asyncio.create_task(report_login_error_later(page))
        except TimeoutError:
            if is_login_page(page.url):
                log.warn("未找到自动登录控件,请在浏览器中手动完成登录.", shift=True)
                log.event("自动登录", 方式="手动", 原因="未找到登录控件")
                log.event("人工介入", 状态="需要", 原因="未找到自动登录控件,请在浏览器中手动完成登录.")
        except Exception as exc:
            if is_login_page(page.url):
                log.log_exception("自动填写登录表单失败, 请在浏览器中手动完成登录.", exc)
                log.event("自动登录", 方式="手动", 原因=type(exc).__name__)
                log.event("人工介入", 状态="需要", 原因="自动填写登录表单失败, 请在浏览器中手动完成登录.")
    else:
        log.info("配置文件未填写账号密码,请在浏览器中手动登录.", shift=True)
        log.event("自动登录", 方式="手动", 原因="未配置账号密码")
        log.event("人工介入", 状态="需要", 原因="配置文件未填写账号密码,请在浏览器中手动登录.")

    captcha_task = None
    if config.enableAutoCaptcha and modules:
        log.event("滑块任务", 状态="启动")
        captcha_task = asyncio.create_task(slider_verify(page, modules))

    try:
        await wait_for_login_complete(page)
    finally:
        for task in (error_task, captcha_task):
            if task and not task.done():
                task.cancel()
        pending = [task for task in (error_task, captcha_task) if task]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    log.event("登录完成", 耗时=f"{time.time() - wait_start:.1f}s", 地址=page.url)
    log.event("人工介入", 状态="已解除", 原因="登录完成")
    await persist_login_cookies(context, ctx=ctx)
    log.info(f"已保存登录凭证到: {cookie_path},下次可免密登录.")


def _verify_cookie_status(cookies, proxy: str = "") -> SessionStatus:
    """使用 CAS + getLoginUserInfo 二次校验 Cookie 状态，异常时视为不可用避免误删凭证。"""
    try:
        with ZhsApiClient(cookies, proxy=proxy) as client:
            return client.verify_session_status()
    except Exception:
        return SessionStatus.UNAVAILABLE


async def ensure_login(
    context: BrowserContext,
    page: Page,
    cookies,
    config,
    modules=None,
    *,
    ctx: RunContext | None = None,
) -> bool:
    log = _get_logger()
    cookie_path = _get_cookie_path(ctx)
    if cookies:
        log.info("正在校验 Cookies 登录状态...")
        await page.goto(config.login_url, wait_until="domcontentloaded")
        try:
            await wait_for_login_complete(page, timeout=10000)
        except TimeoutError:
            pass
        if not is_login_page(page.url):
            log.info("使用Cookies登录成功!")
            log.event("登录状态", 结果="Cookies 有效", 地址=page.url)
            await persist_login_cookies(context, ctx=ctx)
            return True
        status = await asyncio.to_thread(
            _verify_cookie_status, cookies, getattr(config, "proxy", "")
        )
        if status is SessionStatus.EXPIRED:
            log.warn("检测到 Cookies 已失效, 将重新登录.", shift=True)
            log.event("登录状态", 结果="Cookies 失效", 地址=page.url)
            clear_cookies(cookie_path)
            remember_login_cookies(None, ctx=ctx)
            cookies = None
        elif status is SessionStatus.UNAVAILABLE:
            log.warn(
                "校验登录状态时网络波动或服务端异常, 已保留本地 Cookies 不予删除, 将继续登录流程.",
                shift=True,
            )
            log.event("登录状态", 结果="校验不可用(保留Cookies)", 地址=page.url)
        else:
            log.warn(
                "服务端会话仍有效但登录页跳转超时, 已保留本地 Cookies 不予删除, 将继续等待登录.",
                shift=True,
            )
            log.event("登录状态", 结果="页面超时(保留Cookies)", 地址=page.url)

    if not config.username or not config.password:
        log.info("请手动填写账号密码...")
    log.info("正在等待登录完成...")
    await auto_login(context, page, config, modules, ctx=ctx)
    log.info("登录成功!")
    return False


async def maybe_qr_login(config, cookies, *, ctx: RunContext | None = None):
    """启用二维码登录且本地无可用 Cookie 时，先通过终端/图片扫码换取 Cookies。"""
    if cookies or not getattr(config, "qr_login", False):
        return cookies
    log = _get_logger()
    cookie_path = _get_cookie_path(ctx)
    save_fn = _resolve_av_attr("save_cookies", save_cookies)
    log.info("已启用二维码登录，正在获取智慧树登录二维码...", shift=True)
    show_in_terminal = bool(
        getattr(config, "qr_terminal", False) or not getattr(config, "qr_save_dir", "")
    )
    new_cookies = await asyncio.to_thread(
        qr_login_for_cookies,
        show_in_terminal=show_in_terminal,
        ensure_unicode=bool(getattr(config, "qr_unicode", False)),
        save_dir=getattr(config, "qr_save_dir", ""),
        proxy=getattr(config, "proxy", ""),
        logger=log,
    )
    if new_cookies:
        try:
            save_fn(new_cookies, cookie_path)
            remember_login_cookies(new_cookies, ctx=ctx)
            log.info(
                f"二维码登录成功，已保存 {len(new_cookies)} 条凭证到: {cookie_path}",
                shift=True,
            )
        except Exception as exc:
            log.log_exception("保存二维码登录 Cookies 失败.", exc)
    return new_cookies


def save_credentials_to_config(config_path: str, username: str, password: str) -> bool:
    """把账号密码写回 config.ini, 只替换目标行, 保留文件中的其余内容和注释。"""
    log = _get_logger()
    raw = None
    used_encoding = "utf-8"
    for encoding in ("utf-8-sig", "gbk"):
        try:
            with open(config_path, "r", encoding=encoding) as file:
                raw = file.read()
            # utf-8-sig 仅用于剥离 BOM, 回写统一用 utf-8, 避免给原文件追加 BOM
            used_encoding = "utf-8" if encoding == "utf-8-sig" else encoding
            break
        except UnicodeDecodeError:
            continue
    if raw is None:
        log.warn("配置文件读取失败,账号密码仅本次运行生效.", shift=True)
        return False

    def replace_field(text: str, key: str, value: str) -> str:
        # 只匹配行内空白: \s 会吞掉换行, 导致下一行被整体替换掉
        pattern = re.compile(rf"(?m)^([ \t]*{key}[ \t]*=[ \t]*).*$")
        if pattern.search(text):
            return pattern.sub(lambda match: match.group(1) + value, text, count=1)
        if "[user-account]" in text:
            return text.replace("[user-account]", f"[user-account]\n{key} = {value}", 1)
        return text.rstrip("\r\n") + f"\n[user-account]\n{key} = {value}\n"

    raw = replace_field(raw, "username", username)
    raw = replace_field(raw, "password", password)
    try:
        with open(config_path, "w", encoding=used_encoding) as file:
            file.write(raw)
    except OSError as exc:
        log.log_exception("保存账号密码到配置文件失败.", exc)
        return False
    return True


def ensure_credentials(config) -> None:
    """配置文件未填账号密码时, 在终端引导输入并回存, 之后即可全自动登录。"""
    if config.username and config.password:
        return
    if not config.config_path or not sys.stdin.isatty() or not sys.stdout.isatty():
        return
    log = _get_logger()
    try:
        log.info("配置文件未填写账号密码,现在输入一次即可永久自动登录(直接回车可跳过).", shift=True)
        username = input("请输入账号(手机号): ").strip()
        if not username:
            return
        password = getpass.getpass("请输入密码: ").strip()
        if not password:
            return
    except (EOFError, KeyboardInterrupt):
        log.info("", shift=True)
        return
    if save_credentials_to_config(config.config_path, username, password):
        log.event("自动登录", 方式="账号密码", 状态="已保存到配置文件", 文件=config.config_path)
    config.username = username
    config.password = password
    log.info("已记录本次输入的账号密码,正在自动登录...")
