import json
import os
import os.path
import sys
import tempfile
from playwright.async_api import Page, Locator
from playwright.async_api import TimeoutError
from playwright._impl._errors import TargetClosedError
from modules.configs import Config
from modules.lesson_navigation import (
    CatalogSelectors,
    get_lesson_title,
    lesson_is_complete,
)
import time
from modules.logger import Logger
from modules.popups import dismiss_course_popups
from modules.video_state import COURSE_VIDEO_SELECTOR
from modules.window_control import (
    display_window,
    get_browser_window,
    hide_window,
    should_hide_window,
)

if sys.platform == "win32":
    import ctypes
else:
    ctypes = None

logger = Logger()

# 可选元素(弹窗等)的等待上限: 这类元素只在特定条件下出现, 不存在时必须尽快跳过。
# 打包版把页面默认超时设为 24 小时, 沿用默认值会让整轮学习一直卡在原地。
# 后台 dismiss_popups 任务会持续清理延迟弹出的各类弹窗, 首屏这里只需短暂等待即可。
OPTIONAL_POPUP_TIMEOUT_MS = 5_000


def get_runtime_root():
    return logger.runtime_root


def get_runtime_path(*parts):
    return os.path.join(get_runtime_root(), *parts)


def get_resource_path(*parts):
    """定位只读内置资源（如 stealth.min.js），支持数据目录与程序目录分离。"""
    res_override = os.environ.get("AUTOVISOR_RESOURCES_DIR", "").strip()
    if res_override:
        candidate = os.path.join(os.path.abspath(res_override), *parts)
        if os.path.exists(candidate):
            return candidate
        # 若 AUTOVISOR_RESOURCES_DIR 指向 resources 目录本身，则去掉前缀 resources 再查一次
        if parts and parts[0] == "resources":
            candidate = os.path.join(os.path.abspath(res_override), *parts[1:])
            if os.path.exists(candidate):
                return candidate
    if getattr(sys, "frozen", False):
        exe_candidate = os.path.join(os.path.dirname(sys.executable), *parts)
        if os.path.exists(exe_candidate):
            return exe_candidate
    repo_candidate = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), *parts)
    if os.path.exists(repo_candidate):
        return repo_candidate
    return get_runtime_path(*parts)

def save_cookies(cookies, filename="cookies.json"):
    """保存登录Cookies到文件"""
    filename = os.fspath(filename)
    directory = os.path.dirname(os.path.abspath(filename))
    fd, temp_path = tempfile.mkstemp(prefix=".cookies-", dir=directory)
    try:
        if os.name != "nt":
            os.chmod(temp_path, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(cookies, file)
        os.replace(temp_path, filename)
        if os.name != "nt":
            os.chmod(filename, 0o600)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise

def is_zhihuishu_domain(domain) -> bool:
    """判断 cookie 域名是否属于智慧树(含所有子域)。"""
    host = str(domain or "").strip().lstrip(".").lower()
    return host == "zhihuishu.com" or host.endswith(".zhihuishu.com")


def filter_zhihuishu_cookies(cookies, now=None):
    """筛出智慧树域下未过期的 Cookies, 其余(过期/无效/第三方)全部丢弃。"""
    now = time.time() if now is None else now
    filtered = []
    for cookie in cookies or []:
        if not isinstance(cookie, dict):
            continue
        name = str(cookie.get("name") or "").strip()
        if not name or not is_zhihuishu_domain(cookie.get("domain")):
            continue
        expires = cookie.get("expires", cookie.get("expirationDate"))
        if isinstance(expires, (int, float)) and expires > 0 and expires <= now:
            continue
        filtered.append(cookie)
    return filtered


def load_cookies(filename="cookies.json"):
    """从文件加载 Cookies, 顺带过滤已过期与非智慧树域名的记录。"""
    try:
        with open(filename, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.warn("Cookies 文件已损坏, 本次将忽略并重新登录.", shift=True)
        return None
    except OSError as exc:
        logger.log_exception("读取 Cookies 文件失败.", exc)
        return None
    if not isinstance(data, list):
        return None
    cookies = filter_zhihuishu_cookies(data)
    dropped = len(data) - len(cookies)
    if dropped:
        logger.info(f"已忽略 {dropped} 条过期或无效的 Cookies.")
    return cookies or None


def normalize_zhihuishu_cookies(cookies, now=None):
    now = time.time() if now is None else now
    normalized = []
    for cookie in cookies:
        domain = str(cookie.get("domain") or "").strip()
        name = str(cookie.get("name") or "").strip()
        valid_domain = is_zhihuishu_domain(domain)
        if not name or not valid_domain:
            continue
        expires = cookie.get("expires", cookie.get("expirationDate"))
        if isinstance(expires, (int, float)) and expires > 0 and expires <= now:
            continue
        item = {
            "name": name,
            "value": str(cookie.get("value") or ""),
            "domain": domain,
            "path": cookie.get("path") or "/",
            "secure": bool(cookie.get("secure", False)),
        }
        if isinstance(expires, (int, float)) and expires > now:
            item["expires"] = float(expires)
        rest = cookie.get("rest") or {}
        if rest.get("HttpOnly") is True or cookie.get("httpOnly") is True:
            item["httpOnly"] = True
        same_site = rest.get("SameSite") or cookie.get("sameSite")
        if same_site in {"Strict", "Lax", "None"}:
            item["sameSite"] = same_site
        normalized.append(item)
    return normalized


def import_zhihuishu_cookies(source, destination="cookies.json") -> int:
    with open(source, "r", encoding="utf-8") as file:
        raw = json.load(file)
    if not isinstance(raw, list):
        raise ValueError("Cookie 文件必须是列表格式")
    cookies = normalize_zhihuishu_cookies(raw)
    if not cookies:
        raise ValueError("Cookie 文件中没有可用的智慧树 Cookie")
    save_cookies(cookies, destination)
    return len(cookies)


def clear_cookies(filename="cookies.json"):
    if os.path.exists(filename):
        os.remove(filename)

# 将python终端前置
def bring_console_to_front():
    if sys.platform != "win32":
        return False
    # 获取当前控制台窗口句柄
    hwnd = ctypes.windll.kernel32.GetConsoleWindow()
    if hwnd:
        ctypes.windll.user32.ShowWindow(hwnd, 5)  # SW_SHOW
        ctypes.windll.user32.SetForegroundWindow(hwnd)
        return True
    return False


async def evaluate_js(page: Page, wait_selector, js: str, timeout=None, is_hike_class=False) -> None:
    """等待可选元素(如弹窗)后执行页面脚本; 元素未出现时跳过, 不无期限等待。"""
    if timeout is None:
        timeout = OPTIONAL_POPUP_TIMEOUT_MS
    try:
        if wait_selector and is_hike_class is False:
            await page.wait_for_selector(wait_selector, timeout=timeout)
        if is_hike_class is False:
            await page.evaluate(js)
    except TargetClosedError as e:
        logger.debug(f"浏览器关闭时停止执行页面脚本: {logger.summarize_exception(e)}")
        return
    except TimeoutError:
        # 弹窗等可选元素未出现属于正常情况: 记节流日志即可, 不能当成异常
        logger.debug_throttled(
            f"evaluate_js:{wait_selector}",
            f"页面脚本跳过: 等待元素超时 {wait_selector}",
        )
        return
    except Exception as e:
        logger.log_exception(f"执行页面脚本失败. Selector: {wait_selector} JS: {js}", e)
        return


async def evaluate_on_element(page: Page, selector: str, js: str, timeout: float = None,
                              is_hike_class=False) -> None:
    """在可选元素上执行脚本; 元素未出现时跳过, 不无期限等待。"""
    if timeout is None:
        timeout = OPTIONAL_POPUP_TIMEOUT_MS
    try:
        if selector and is_hike_class is False:
            element = page.locator(selector).first
            await element.evaluate(js, timeout=timeout)
    except TargetClosedError as e:
        logger.debug(f"浏览器关闭时停止执行元素脚本: {logger.summarize_exception(e)}")
        return
    except TimeoutError:
        logger.debug_throttled(
            f"evaluate_on_element:{selector}",
            f"元素脚本跳过: 等待元素超时 {selector}",
        )
        return
    except Exception as e:
        logger.log_exception(f"执行元素脚本失败. Selector: {selector} JS: {js}", e)
        return


async def optimize_page(
    page: Page, config: Config, catalog: CatalogSelectors
) -> None:
    try:
        closed_initial = await dismiss_course_popups(
            page,
            max_rounds=5,
            settle_ms=250,
            remove_floating_tips=(catalog.name == "legacy"),
        )
        for item in closed_initial:
            logger.info(f"已关闭课程弹窗: {item['title']} ({item['button']})")
            logger.event("关闭弹窗", 标题=item["title"], 操作=item["button"], 方式=item.get("strategy", "button"))

        preread_close = page.locator(
            ".ss2077-custom-modal .ss2077-custom-dialog:visible "
            ".ss2077-custom-title > img.icon"
        ).first
        if await preread_close.count() and await preread_close.is_visible():
            await preread_close.click(timeout=2000)
            logger.info("已关闭学前必读弹窗.")
            logger.event("关闭弹窗", 标题="学前必读", 操作="icon", 方式="close_icon")

        if catalog.name == "legacy":
            # "学习时长/学前必读"弹窗(.studytime-div)只在未读过时显示: 节点常驻 DOM,
            # 平台不提示时它是 0x0 的隐藏节点, 等它可见会一直等下去。
            await evaluate_js(
                page,
                ".studytime-div",
                config.pop_js,
                timeout=OPTIONAL_POPUP_TIMEOUT_MS,
            )
            hour = time.localtime().tm_hour
            if hour >= 18 or hour < 7:
                await evaluate_on_element(page, ".Patternbtn-div", "el=>el.click()", timeout=1500)
            await evaluate_on_element(page, ".exploreTip", "el=>el.remove()", timeout=1500)
            await evaluate_on_element(page, ".ai-helper-Index2", "el=>el.remove()", timeout=1500)
            await evaluate_on_element(page, ".aiMsg.once", "el=>el.remove()", timeout=1500)

        closed_after = await dismiss_course_popups(
            page,
            max_rounds=4,
            settle_ms=250,
            remove_floating_tips=(catalog.name == "legacy"),
        )
        for item in closed_after:
            logger.info(f"已关闭课程弹窗: {item['title']} ({item['button']})")
            logger.event("关闭弹窗", 标题=item["title"], 操作=item["button"], 方式=item.get("strategy", "button"))

    except TargetClosedError as e:
        logger.debug(f"浏览器关闭时停止页面优化: {logger.summarize_exception(e)}")
        return
    except Exception as e:
        logger.log_exception("页面优化失败.", e)
        return


async def get_video_attr(page, attr: str) -> any:
    try:
        await page.wait_for_selector(
            COURSE_VIDEO_SELECTOR, state="attached", timeout=1000
        )
    except TimeoutError:
        # 视频元素尚未挂载属于轮询常态, 不能每次打印完整堆栈
        logger.debug_throttled(
            f"video_attr:{attr}",
            f"读取视频属性超时,视频元素未就绪: {attr}",
        )
        return None
    except TargetClosedError as e:
        logger.debug(f"浏览器关闭时停止读取视频属性 {attr}: {logger.summarize_exception(e)}")
        return None
    return await run_on(
        page,
        COURSE_VIDEO_SELECTOR,
        f"(el) => el[{attr!r}]",
        f"读取视频属性 {attr}",
    )


# 元素不存在时由页面脚本返回的哨兵值
_MISSING = "__autovisor_missing__"


async def run_on(page, selector: str, action: str, purpose: str):
    """在匹配 selector 的元素上执行 action(el)。

    元素不存在时返回 None 并记一条带选择器的节流日志; 脚本报错同样返回 None,
    但会把选择器与用途写进异常日志, 便于定位是哪个元素/类名出的问题。
    """
    script = (
        "() => {"
        f" const el = document.querySelector({selector!r});"
        f" if (!el) return {_MISSING!r};"
        f" return ({action})(el);"
        " }"
    )
    try:
        result = await page.evaluate(script)
    except TargetClosedError:
        raise
    except Exception as exc:
        logger.log_exception(f"{purpose}失败(选择器: {selector}).", exc)
        return None
    if result == _MISSING:
        logger.debug_throttled(
            f"run_on:{selector}",
            f"{purpose}跳过: 页面未找到元素 {selector}",
        )
        return None
    return result


async def get_lesson_name(
    page: Page, lesson: Locator, catalog: CatalogSelectors
) -> str:
    return await get_lesson_title(page, lesson, catalog)


async def get_filtered_class(
    page: Page, catalog: CatalogSelectors, include_all=False
) -> list[Locator]:
    try:
        await page.wait_for_selector(catalog.item, timeout=2000)
    except TimeoutError:
        pass

    all_class = await page.locator(catalog.item).all()
    if include_all:
        logger.debug(f"Get to-review class: {len(all_class)}")
        return all_class

    to_learn_class = []
    for each in all_class:
        if not await lesson_is_complete(each, catalog):
            to_learn_class.append(each)
    logger.debug(f"Get to-learn class: {len(to_learn_class)} / {len(all_class)}")
    return to_learn_class
