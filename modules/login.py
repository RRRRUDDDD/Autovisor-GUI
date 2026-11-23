from urllib.parse import urlparse

from playwright.async_api import Error as PlaywrightError, Page, TimeoutError


LOGIN_HOSTS = {"login.zhihuishu.com", "passport.zhihuishu.com"}
# 手机号/账号登录选项卡: 兼容新版 login.zhihuishu.com 与旧版 passport.zhihuishu.com
LOGIN_TAB_SELECTOR = "[id='tab-1.1'], #qSignin"
LOGIN_USERNAME_SELECTOR = "input[name='mobile'], #lUsername"
LOGIN_PASSWORD_SELECTOR = "input[type='password'], #lPassword"
LOGIN_SUBMIT_SELECTOR = ".btn-block__grandient_login, .wall-sub-btn, button[type='submit']"
# 登录失败提示: 兼容新版 Element Plus 提示与旧版表单字段/弹层提示
LOGIN_ERROR_SELECTORS = (
    ".el-message__content",
    ".el-form-item__error",
    ".el-notification__content",
    ".error-tips-pop",
    ".form-ipt-error",
)


def is_login_page(url: str) -> bool:
    """判断 URL 是否仍处于智慧树登录流程。"""
    hostname = (urlparse(url).hostname or "").lower()
    return hostname in LOGIN_HOSTS


async def wait_for_login_complete(
    page: Page, timeout: float = 24 * 3600 * 1000
) -> None:
    """等待页面离开智慧树登录域名。"""
    if not is_login_page(page.url):
        return
    await page.wait_for_url(
        lambda url: not is_login_page(str(url)),
        wait_until="commit",
        timeout=timeout,
    )


async def wait_for_login_page_settled(page: Page, timeout: float = 5000) -> None:
    """旧版 passport.zhihuishu.com 会通过前端 JS 重定向到 login.zhihuishu.com, 先等跳转稳定。"""
    hostname = (urlparse(page.url).hostname or "").lower()
    if hostname != "passport.zhihuishu.com":
        return
    try:
        await page.wait_for_url(
            lambda url: (urlparse(str(url)).hostname or "").lower() != "passport.zhihuishu.com",
            wait_until="domcontentloaded",
            timeout=timeout,
        )
    except TimeoutError:
        # 若未重定向(例如旧版仍直接提供登录页), 则继续在当前页操作
        pass


async def accept_login_terms(page: Page) -> None:
    """新版登录页存在协议复选框时自动勾选。"""
    checkbox = page.locator("input.el-checkbox__original").first
    if not await checkbox.count():
        return

    if not await checkbox.is_checked():
        # Element UI / Element Plus 的原始 checkbox 通常被隐藏，不能使用 Playwright check。
        await checkbox.evaluate("element => element.click()")


async def perform_password_login(page: Page, username: str, password: str) -> None:
    """在登录页填写账号密码并提交; 控件未就绪时抛 TimeoutError。"""
    for attempt in range(2):
        await wait_for_login_page_settled(page)
        if not is_login_page(page.url):
            return
        try:
            tab = page.locator(LOGIN_TAB_SELECTOR).first
            try:
                if await tab.count() and await tab.is_visible():
                    await tab.click(timeout=5000)
            except Exception:
                # 已在账号密码 tab 时点击失败无影响, 继续走表单填写
                pass

            username_el = await page.wait_for_selector(
                LOGIN_USERNAME_SELECTOR, state="visible", timeout=30000
            )
            password_el = await page.wait_for_selector(
                LOGIN_PASSWORD_SELECTOR, state="visible", timeout=30000
            )
            await username_el.fill(username)
            await password_el.fill(password)
            await accept_login_terms(page)
            submit = await page.wait_for_selector(
                LOGIN_SUBMIT_SELECTOR, state="visible", timeout=30000
            )
            await page.wait_for_timeout(500)
            await submit.click()
            return
        except PlaywrightError as exc:
            # 页面在填写期间从 passport.zhihuishu.com 跳转到 login.zhihuishu.com 时自动重试一次
            msg = str(exc)
            is_nav_race = (
                "Execution context was destroyed" in msg
                or "most likely because of a navigation" in msg
                or "Element is not attached" in msg
            )
            if attempt == 0 and is_nav_race:
                await page.wait_for_timeout(800)
                continue
            raise


async def describe_login_error(page: Page) -> str | None:
    """提交后仍停留在登录页时, 收集页面可见的错误提示文本。"""
    texts = []
    for selector in LOGIN_ERROR_SELECTORS:
        try:
            locator = page.locator(f"{selector}:visible")
            count = await locator.count()
        except Exception:
            continue
        for index in range(min(count, 3)):
            try:
                text = " ".join((await locator.nth(index).text_content() or "").split())
            except Exception:
                continue
            if text and text not in texts:
                texts.append(text)
    if not texts:
        return None
    return "; ".join(texts)[:200]
