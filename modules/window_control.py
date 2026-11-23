"""浏览器窗口隐藏与恢复控制（Windows 移出虚拟桌面方案）。

核心设计原则：
1. 不使用 Win32 SW_HIDE：SW_HIDE 会清除 WS_VISIBLE，不仅导致 pygetwindow
   等基于 IsWindowVisible 的窗口枚举永久失联，还会触发 Chromium 合成器停止
   出帧、document.visibilityState 变为 hidden 以及截屏/视频播放降频。
2. 改用「移出虚拟桌面边界外」的方式隐藏：先记录原始位置与最大化状态，必要时
   以 SW_SHOWNOACTIVATE 解除最大化（不抢焦点），再保持原尺寸移出所有显示器
   组成的虚拟桌面范围之外；恢复时精确还原位置/最大化状态并前置聚焦。
3. 按 Page 缓存窗口句柄（HWND）并用独立异步锁串行化隐藏/显示操作：避免每次
   改写 document.title 盲等 1 秒，也避免页面跳转、SPA 改标题或弹出新标签页
   时找不到原窗口。首次定位后立即恢复原 document.title。
4. 连接现有 Chrome（attach_existing_chrome=True）时默认跳过隐藏，避免误藏
   用户日常正在使用的浏览器窗口。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import sys
import uuid
import weakref

from playwright._impl._errors import TargetClosedError
from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from modules.logger import Logger

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes
else:
    ctypes = None
    wintypes = None

logger = Logger()

# Win32 常量
SM_CXSCREEN = 0
SM_CYSCREEN = 1
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

SW_MAXIMIZE = 3
SW_SHOWNOACTIVATE = 4
SW_SHOW = 5
SW_RESTORE = 9

HWND_TOP = 0
GW_OWNER = 4

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040

OFFSCREEN_MARGIN = 2000
TITLE_POLL_INTERVAL_S = 0.05
TITLE_POLL_ATTEMPTS = 30
DEFAULT_WINDOW_WIDTH = 1280
DEFAULT_WINDOW_HEIGHT = 720
MIN_VALID_WINDOW_SIZE = 200


@dataclass(frozen=True)
class WindowRect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)


@dataclass
class WindowState:
    hwnd: int
    hidden: bool = False
    saved_rect: WindowRect | None = None
    was_maximized: bool = False


class _Win32Backend:
    """封装所有 Win32 API 调用，便于在非 Windows 或单元测试中注入替身。"""

    def __init__(self) -> None:
        if ctypes is None or wintypes is None:
            raise RuntimeError("Win32 backend is only available on Windows")
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.IsWindow.argtypes = [wintypes.HWND]
        user32.IsWindow.restype = wintypes.BOOL
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.IsZoomed.argtypes = [wintypes.HWND]
        user32.IsZoomed.restype = wintypes.BOOL
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsIconic.restype = wintypes.BOOL
        user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetWindow.restype = wintypes.HWND
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetClassNameW.restype = ctypes.c_int
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetWindowRect.restype = wintypes.BOOL
        user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        user32.GetSystemMetrics.restype = ctypes.c_int
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL
        user32.SetWindowPos.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        user32.SetWindowPos.restype = wintypes.BOOL
        user32.BringWindowToTop.argtypes = [wintypes.HWND]
        user32.BringWindowToTop.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL
        self._user32 = user32

    def is_window(self, hwnd: int) -> bool:
        return bool(hwnd and self._user32.IsWindow(hwnd))

    def is_visible(self, hwnd: int) -> bool:
        return bool(self._user32.IsWindowVisible(hwnd))

    def is_maximized(self, hwnd: int) -> bool:
        return bool(self._user32.IsZoomed(hwnd))

    def is_minimized(self, hwnd: int) -> bool:
        return bool(self._user32.IsIconic(hwnd))

    def get_window_rect(self, hwnd: int) -> WindowRect | None:
        rect = wintypes.RECT()
        if not self._user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        return WindowRect(rect.left, rect.top, rect.right, rect.bottom)

    def get_virtual_screen(self) -> WindowRect:
        vx = int(self._user32.GetSystemMetrics(SM_XVIRTUALSCREEN))
        vy = int(self._user32.GetSystemMetrics(SM_YVIRTUALSCREEN))
        vw = int(self._user32.GetSystemMetrics(SM_CXVIRTUALSCREEN))
        vh = int(self._user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))
        if vw <= 0 or vh <= 0:
            vw = int(self._user32.GetSystemMetrics(SM_CXSCREEN)) or 1920
            vh = int(self._user32.GetSystemMetrics(SM_CYSCREEN)) or 1080
            vx, vy = 0, 0
        return WindowRect(vx, vy, vx + vw, vy + vh)

    def get_primary_screen(self) -> WindowRect:
        w = int(self._user32.GetSystemMetrics(SM_CXSCREEN)) or 1920
        h = int(self._user32.GetSystemMetrics(SM_CYSCREEN)) or 1080
        return WindowRect(0, 0, w, h)

    def find_windows_by_title(self, token: str) -> list[tuple[int, str, str, bool]]:
        """枚举所有顶层窗口（包含不可见窗口），按标题匹配 token。"""
        matches: list[tuple[int, str, str, bool]] = []
        enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def callback(hwnd, _lparam):
            # 仅检查无 owner 的顶层主窗口，避免命中工具提示或隐藏子窗口
            if self._user32.GetWindow(hwnd, GW_OWNER):
                return True
            length = self._user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            title_buf = ctypes.create_unicode_buffer(length + 1)
            self._user32.GetWindowTextW(hwnd, title_buf, length + 1)
            title = title_buf.value
            if token in title:
                cls_buf = ctypes.create_unicode_buffer(256)
                self._user32.GetClassNameW(hwnd, cls_buf, 256)
                matches.append(
                    (
                        int(hwnd),
                        title,
                        cls_buf.value,
                        bool(self._user32.IsWindowVisible(hwnd)),
                    )
                )
            return True

        self._user32.EnumWindows(enum_proc(callback), 0)
        return matches

    def show_window(self, hwnd: int, cmd: int) -> None:
        self._user32.ShowWindow(hwnd, cmd)

    def set_window_pos(
        self, hwnd: int, x: int, y: int, width: int, height: int, flags: int
    ) -> bool:
        return bool(
            self._user32.SetWindowPos(hwnd, HWND_TOP, x, y, width, height, flags)
        )

    def bring_to_foreground(self, hwnd: int) -> None:
        self._user32.BringWindowToTop(hwnd)
        self._user32.SetForegroundWindow(hwnd)


_default_backend: _Win32Backend | None = None
_page_states: weakref.WeakKeyDictionary[object, WindowState] = weakref.WeakKeyDictionary()
_page_locks: weakref.WeakKeyDictionary[object, asyncio.Lock] = weakref.WeakKeyDictionary()
_fallback_states: dict[int, WindowState] = {}
_fallback_locks: dict[int, asyncio.Lock] = {}


def _get_backend():
    global _default_backend
    if sys.platform != "win32":
        return None
    if _default_backend is None:
        _default_backend = _Win32Backend()
    return _default_backend


def _get_state(page: object) -> WindowState | None:
    try:
        return _page_states.get(page)
    except TypeError:
        return _fallback_states.get(id(page))


def _set_state(page: object, state: WindowState) -> None:
    try:
        _page_states[page] = state
    except TypeError:
        _fallback_states[id(page)] = state


def _clear_state(page: object) -> None:
    try:
        _page_states.pop(page, None)
    except TypeError:
        _fallback_states.pop(id(page), None)


def _get_lock(page: object) -> asyncio.Lock:
    try:
        lock = _page_locks.get(page)
        if lock is None:
            lock = asyncio.Lock()
            _page_locks[page] = lock
        return lock
    except TypeError:
        key = id(page)
        lock = _fallback_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _fallback_locks[key] = lock
        return lock


def reset_window_cache() -> None:
    """清空缓存的窗口状态（供测试使用）。"""
    _page_states.clear()
    _page_locks.clear()
    _fallback_states.clear()
    _fallback_locks.clear()


def should_hide_window(config) -> bool:
    """判断当前配置是否应该执行浏览器窗口隐藏。

    当连接用户现有 Chrome（attach_existing_chrome=True）时，禁止隐藏窗口，
    以免把用户日常正在使用的浏览器移出屏幕。
    """
    return bool(
        getattr(config, "enableHideWindow", False)
        and not getattr(config, "attach_existing_chrome", False)
    )


def _rects_intersect(a: WindowRect, b: WindowRect) -> bool:
    return a.left < b.right and a.right > b.left and a.top < b.bottom and a.bottom > b.top


def _is_valid_onscreen_rect(rect: WindowRect | None, virtual_screen: WindowRect) -> bool:
    if rect is None:
        return False
    if rect.width < MIN_VALID_WINDOW_SIZE or rect.height < MIN_VALID_WINDOW_SIZE:
        return False
    return _rects_intersect(rect, virtual_screen)


def _compute_offscreen_position(virtual_screen: WindowRect) -> tuple[int, int]:
    """计算绝对位于多显示器虚拟桌面之外的坐标（右侧保留安全余量）。"""
    return virtual_screen.right + OFFSCREEN_MARGIN, virtual_screen.top


def _clamp_restore_rect(
    rect: WindowRect | None, primary_screen: WindowRect, virtual_screen: WindowRect
) -> WindowRect:
    """确保恢复后的窗口坐标与尺寸处于可见桌面范围内。"""
    if rect is not None and _is_valid_onscreen_rect(rect, virtual_screen):
        return rect
    width = (
        rect.width
        if rect is not None and rect.width >= MIN_VALID_WINDOW_SIZE
        else min(DEFAULT_WINDOW_WIDTH, max(MIN_VALID_WINDOW_SIZE, primary_screen.width - 160))
    )
    height = (
        rect.height
        if rect is not None and rect.height >= MIN_VALID_WINDOW_SIZE
        else min(DEFAULT_WINDOW_HEIGHT, max(MIN_VALID_WINDOW_SIZE, primary_screen.height - 160))
    )
    width = min(width, primary_screen.width)
    height = min(height, primary_screen.height)
    left = max(primary_screen.left, min(primary_screen.left + 80, primary_screen.right - width))
    top = max(primary_screen.top, min(primary_screen.top + 60, primary_screen.bottom - height))
    return WindowRect(left, top, left + width, top + height)


def _pick_best_hwnd(matches: list[tuple[int, str, str, bool]]) -> int | None:
    """优先选择 Chromium 主窗口类（Chrome_WidgetWin_*）且可见的窗口。"""
    if not matches:
        return None
    ranked = sorted(
        matches,
        key=lambda item: (
            0 if item[2].startswith("Chrome_WidgetWin_") else 1,
            0 if item[3] else 1,
        ),
    )
    return ranked[0][0]


async def _locate_hwnd(page: Page, backend) -> int | None:
    state = _get_state(page)
    if state is not None and backend.is_window(state.hwnd):
        return state.hwnd
    if state is not None:
        _clear_state(page)

    try:
        await page.wait_for_load_state("domcontentloaded", timeout=5000)
    except PlaywrightTimeoutError:
        pass
    except TargetClosedError:
        raise
    except Exception:
        pass

    token = f"AutovisorWin-{uuid.uuid4().hex[:8]}"
    original_title = None
    title_injected = False
    try:
        original_title = await page.evaluate(
            """(marker) => {
                const prev = document.title;
                document.title = prev ? `${marker} - ${prev}` : marker;
                return prev;
            }""",
            token,
        )
        title_injected = True
    except TargetClosedError:
        raise
    except Exception as exc:
        logger.debug(f"写入临时窗口标题失败: {logger.summarize_exception(exc)}")
        return None

    try:
        for attempt in range(TITLE_POLL_ATTEMPTS):
            hwnd = _pick_best_hwnd(backend.find_windows_by_title(token))
            if hwnd is not None:
                _set_state(page, WindowState(hwnd=hwnd))
                return hwnd
            # 若当前页面不是激活标签页（例如测验开了新标签页），中途尝试切回当前页再找一次
            if attempt == 6 and hasattr(page, "bring_to_front"):
                try:
                    await page.bring_to_front()
                except Exception:
                    pass
            await asyncio.sleep(TITLE_POLL_INTERVAL_S)
        return None
    finally:
        if title_injected:
            try:
                await page.evaluate(
                    """([marker, prev]) => {
                        if (typeof document.title === "string" && document.title.includes(marker)) {
                            document.title = prev || "";
                        }
                    }""",
                    [token, original_title or ""],
                )
            except Exception:
                pass


async def get_browser_window(page: Page, backend=None) -> int | None:
    """获取当前 Playwright 页面对应的顶层浏览器窗口句柄（HWND）。"""
    resolved_backend = backend if backend is not None else _get_backend()
    if resolved_backend is None:
        return None
    return await _locate_hwnd(page, resolved_backend)


async def hide_window(page: Page, backend=None) -> bool:
    """将浏览器窗口移出虚拟桌面范围外，保持渲染与脚本正常运行。"""
    resolved_backend = backend if backend is not None else _get_backend()
    if resolved_backend is None:
        platform_label = "macOS" if sys.platform == "darwin" else "当前系统"
        logger.warn(f"{platform_label} 不支持隐藏单个浏览器窗口,将保持可见以便处理验证.")
        return False

    async with _get_lock(page):
        hwnd = await _locate_hwnd(page, resolved_backend)
        if not hwnd:
            logger.warn("未找到播放窗口!")
            return False

        state = _get_state(page)
        if state is None or state.hwnd != hwnd:
            state = WindowState(hwnd=hwnd)
            _set_state(page, state)

        virtual_screen = resolved_backend.get_virtual_screen()
        primary_screen = resolved_backend.get_primary_screen()
        is_min = resolved_backend.is_minimized(hwnd)
        is_max = resolved_backend.is_maximized(hwnd)
        current_rect = resolved_backend.get_window_rect(hwnd)

        # 仅当窗口当前处于屏幕内正常/最大化状态时刷新保存的原始位置，避免重复隐藏覆盖为屏幕外坐标
        if not is_min and _is_valid_onscreen_rect(current_rect, virtual_screen):
            state.saved_rect = current_rect
            state.was_maximized = is_max
        elif state.saved_rect is None:
            state.saved_rect = WindowRect(
                primary_screen.left,
                primary_screen.top,
                primary_screen.right,
                primary_screen.bottom,
            )
            state.was_maximized = is_max

        # 最大化或最小化窗口直接 SetWindowPos 移出屏幕会被系统限制，先静默恢复为普通态（不抢焦点）
        if is_max or is_min or not resolved_backend.is_visible(hwnd):
            resolved_backend.show_window(hwnd, SW_SHOWNOACTIVATE)

        target_w = max(state.saved_rect.width, MIN_VALID_WINDOW_SIZE)
        target_h = max(state.saved_rect.height, MIN_VALID_WINDOW_SIZE)
        off_x, off_y = _compute_offscreen_position(virtual_screen)
        ok = resolved_backend.set_window_pos(
            hwnd,
            off_x,
            off_y,
            target_w,
            target_h,
            SWP_NOZORDER | SWP_NOACTIVATE | SWP_SHOWWINDOW,
        )
        if not ok:
            logger.warn("移出播放窗口失败，窗口将保持当前状态。")
            return False

        state.hidden = True
        logger.info("播放窗口已移至屏幕外后台运行,将在需要安全验证或重新登录时自动前置.")
        return True


async def display_window(page: Page, backend=None) -> bool:
    """将浏览器窗口恢复回原屏幕位置/最大化状态并前置聚焦。"""
    resolved_backend = backend if backend is not None else _get_backend()
    if resolved_backend is None:
        if hasattr(page, "bring_to_front"):
            await page.bring_to_front()
        logger.info("播放标签页已前置.", shift=True)
        return True

    async with _get_lock(page):
        hwnd = await _locate_hwnd(page, resolved_backend)
        if not hwnd:
            logger.warn("未找到播放窗口!")
            return False

        state = _get_state(page)
        if state is None or state.hwnd != hwnd:
            state = WindowState(hwnd=hwnd)
            _set_state(page, state)

        virtual_screen = resolved_backend.get_virtual_screen()
        primary_screen = resolved_backend.get_primary_screen()

        if not resolved_backend.is_visible(hwnd):
            resolved_backend.show_window(hwnd, SW_SHOW)
        if resolved_backend.is_minimized(hwnd):
            resolved_backend.show_window(hwnd, SW_RESTORE)

        restore_rect = _clamp_restore_rect(
            state.saved_rect or resolved_backend.get_window_rect(hwnd),
            primary_screen,
            virtual_screen,
        )
        resolved_backend.set_window_pos(
            hwnd,
            restore_rect.left,
            restore_rect.top,
            restore_rect.width,
            restore_rect.height,
            SWP_SHOWWINDOW,
        )
        if state.was_maximized:
            resolved_backend.show_window(hwnd, SW_MAXIMIZE)

        resolved_backend.bring_to_foreground(hwnd)
        if hasattr(page, "bring_to_front"):
            try:
                await page.bring_to_front()
            except Exception:
                pass

        state.hidden = False
        logger.info("播放窗口已自动前置.", shift=True)
        return True
