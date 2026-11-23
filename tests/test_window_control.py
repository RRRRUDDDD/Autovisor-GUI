import unittest
from types import SimpleNamespace
from unittest import mock

from modules import utils, window_control
from modules.window_control import (
    OFFSCREEN_MARGIN,
    SW_MAXIMIZE,
    SW_RESTORE,
    SW_SHOW,
    SW_SHOWNOACTIVATE,
    WindowRect,
    display_window,
    get_browser_window,
    hide_window,
    reset_window_cache,
    should_hide_window,
)


class _DummyLogger:
    def __init__(self):
        self.infos = []
        self.warns = []
        self.debugs = []

    def info(self, msg, shift=False):
        self.infos.append(msg)

    def warn(self, msg, shift=False):
        self.warns.append(msg)

    def debug(self, msg):
        self.debugs.append(msg)

    @staticmethod
    def summarize_exception(exc):
        return f"{type(exc).__name__}: {exc}"


class _FakePage:
    def __init__(self, title="智慧树课程播放页", *, navigate_title=None):
        self.title = title
        self.navigate_title = navigate_title
        self.evaluate_calls = 0
        self.bring_to_front_calls = 0
        self.on_title_set = None

    async def wait_for_load_state(self, state, timeout=None):
        return None

    async def bring_to_front(self):
        self.bring_to_front_calls += 1

    async def evaluate(self, script, arg=None):
        self.evaluate_calls += 1
        if "const prev = document.title" in script:
            prev = self.title
            marker = arg
            self.title = f"{marker} - {prev}" if prev else marker
            if self.on_title_set is not None:
                self.on_title_set(self.title)
            if self.navigate_title is not None:
                # 模拟页面在恢复原标题前发生了跳转改标题
                self.title = self.navigate_title
            return prev
        if "document.title.includes(marker)" in script:
            marker, prev = arg
            if marker in self.title:
                self.title = prev
            return None
        raise AssertionError(f"Unexpected script: {script}")


class _FakeWin32Backend:
    def __init__(
        self,
        *,
        hwnd=1001,
        rect=WindowRect(100, 80, 1380, 800),
        maximized=False,
        minimized=False,
        visible=True,
        virtual_screen=WindowRect(-1920, 0, 1920, 1080),
        primary_screen=WindowRect(0, 0, 1920, 1080),
        extra_windows=None,
    ):
        self.hwnd = hwnd
        self.valid_hwnds = {hwnd}
        self.rect = rect
        self.maximized = maximized
        self.minimized = minimized
        self.visible = visible
        self.virtual_screen = virtual_screen
        self.primary_screen = primary_screen
        self.window_title = ""
        self.extra_windows = list(extra_windows or [])
        self.show_calls = []
        self.pos_calls = []
        self.foreground_calls = []
        self.find_calls = 0

    def is_window(self, hwnd: int) -> bool:
        return hwnd in self.valid_hwnds

    def is_visible(self, hwnd: int) -> bool:
        return self.visible

    def is_maximized(self, hwnd: int) -> bool:
        return self.maximized

    def is_minimized(self, hwnd: int) -> bool:
        return self.minimized

    def get_window_rect(self, hwnd: int) -> WindowRect | None:
        return self.rect

    def get_virtual_screen(self) -> WindowRect:
        return self.virtual_screen

    def get_primary_screen(self) -> WindowRect:
        return self.primary_screen

    def find_windows_by_title(self, token: str):
        self.find_calls += 1
        hits = []
        for item in self.extra_windows:
            if token in item[1]:
                hits.append(item)
        if self.hwnd in self.valid_hwnds and token in self.window_title:
            hits.append((self.hwnd, self.window_title, "Chrome_WidgetWin_1", self.visible))
        return hits

    def show_window(self, hwnd: int, cmd: int) -> None:
        self.show_calls.append((hwnd, cmd))
        if cmd == SW_SHOWNOACTIVATE:
            self.maximized = False
            self.minimized = False
            self.visible = True
        elif cmd == SW_MAXIMIZE:
            self.maximized = True
            self.minimized = False
            self.visible = True
        elif cmd == SW_SHOW:
            self.visible = True
        elif cmd == SW_RESTORE:
            self.minimized = False
            self.maximized = False
            self.visible = True

    def set_window_pos(self, hwnd, x, y, width, height, flags) -> bool:
        self.pos_calls.append((hwnd, x, y, width, height, flags))
        self.rect = WindowRect(x, y, x + width, y + height)
        return True

    def bring_to_foreground(self, hwnd: int) -> None:
        self.foreground_calls.append(hwnd)


class WindowControlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        reset_window_cache()
        self.logger = _DummyLogger()
        patcher = mock.patch.object(window_control, "logger", self.logger)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(reset_window_cache)

    def test_should_hide_window_respects_attach_existing_chrome(self):
        self.assertTrue(
            should_hide_window(
                SimpleNamespace(enableHideWindow=True, attach_existing_chrome=False)
            )
        )
        self.assertFalse(
            should_hide_window(
                SimpleNamespace(enableHideWindow=False, attach_existing_chrome=False)
            )
        )
        self.assertFalse(
            should_hide_window(
                SimpleNamespace(enableHideWindow=True, attach_existing_chrome=True)
            )
        )

    def test_utils_reexports_window_control_helpers(self):
        self.assertIs(utils.hide_window, hide_window)
        self.assertIs(utils.display_window, display_window)
        self.assertIs(utils.get_browser_window, get_browser_window)
        self.assertIs(utils.should_hide_window, should_hide_window)

    async def test_hide_and_display_maximized_window_roundtrip_and_caches_hwnd(self):
        backend = _FakeWin32Backend(
            rect=WindowRect(-7, -7, 1927, 1087),
            maximized=True,
            virtual_screen=WindowRect(-1920, 0, 1920, 1080),
        )
        page = _FakePage("高等数学 - 智慧树")
        page.on_title_set = lambda title: setattr(backend, "window_title", f"{title} - Google Chrome")

        hidden = await hide_window(page, backend=backend)
        self.assertTrue(hidden)
        # 标题必须在定位后恢复为原标题
        self.assertEqual(page.title, "高等数学 - 智慧树")
        # 最大化窗口先静默解除最大化再移到多屏虚拟桌面最右侧之外
        self.assertIn((backend.hwnd, SW_SHOWNOACTIVATE), backend.show_calls)
        self.assertEqual(backend.rect.left, 1920 + OFFSCREEN_MARGIN)
        self.assertEqual(backend.rect.width, 1934)
        self.assertEqual(backend.rect.height, 1094)
        self.assertTrue(backend.visible)

        # 页面后续改了标题后，display_window 直接复用缓存 HWND，不再修改 document.title
        page.title = "安全验证页面"
        evals_before_display = page.evaluate_calls
        shown = await display_window(page, backend=backend)
        self.assertTrue(shown)
        self.assertEqual(page.evaluate_calls, evals_before_display)
        self.assertEqual(page.title, "安全验证页面")
        self.assertTrue(backend.maximized)
        self.assertIn((backend.hwnd, SW_MAXIMIZE), backend.show_calls)
        self.assertEqual(backend.foreground_calls, [backend.hwnd])
        self.assertEqual(page.bring_to_front_calls, 1)

    async def test_double_hide_does_not_overwrite_saved_onscreen_rect(self):
        original = WindowRect(120, 90, 1400, 810)
        backend = _FakeWin32Backend(rect=original, maximized=False)
        page = _FakePage("课程页")
        page.on_title_set = lambda title: setattr(backend, "window_title", title)

        self.assertTrue(await hide_window(page, backend=backend))
        self.assertEqual(backend.rect.left, 1920 + OFFSCREEN_MARGIN)

        # 重复隐藏时，窗口已在屏幕外，不得把屏幕外坐标当成原位置保存
        self.assertTrue(await hide_window(page, backend=backend))
        self.assertTrue(await display_window(page, backend=backend))
        self.assertEqual(backend.rect, original)

    async def test_display_finds_and_restores_even_when_window_was_sw_hidden(self):
        # 回归测试：即便窗口当前 IsWindowVisible == False，仍应能定位并恢复显示
        backend = _FakeWin32Backend(
            rect=WindowRect(100, 100, 1300, 820),
            visible=False,
        )
        page = _FakePage("验证页")
        page.on_title_set = lambda title: setattr(backend, "window_title", title)

        self.assertTrue(await display_window(page, backend=backend))
        self.assertIn((backend.hwnd, SW_SHOW), backend.show_calls)
        self.assertTrue(backend.visible)
        self.assertEqual(backend.foreground_calls, [backend.hwnd])

    async def test_minimized_window_does_not_save_bogus_minimized_rect(self):
        backend = _FakeWin32Backend(
            rect=WindowRect(-32000, -32000, -31840, -31972),
            minimized=True,
            primary_screen=WindowRect(0, 0, 1920, 1080),
        )
        page = _FakePage("课程页")
        page.on_title_set = lambda title: setattr(backend, "window_title", title)

        self.assertTrue(await hide_window(page, backend=backend))
        self.assertIn((backend.hwnd, SW_SHOWNOACTIVATE), backend.show_calls)
        self.assertEqual(backend.rect.width, 1920)
        self.assertEqual(backend.rect.height, 1080)

        self.assertTrue(await display_window(page, backend=backend))
        self.assertEqual(backend.rect, WindowRect(0, 0, 1920, 1080))

    async def test_prefers_chromium_window_class_when_multiple_titles_match(self):
        backend = _FakeWin32Backend(hwnd=2002)
        page = _FakePage("课程")

        def on_set(title):
            backend.window_title = f"{title} - Microsoft Edge"
            backend.extra_windows = [
                (9999, f"Editor - {title}", "Notepad", True),
            ]

        page.on_title_set = on_set
        found = await get_browser_window(page, backend=backend)
        self.assertEqual(found, 2002)

    async def test_does_not_clobber_title_changed_by_navigation(self):
        backend = _FakeWin32Backend(hwnd=3003)
        page = _FakePage("旧页面", navigate_title="新跳转页面")
        page.on_title_set = lambda title: setattr(backend, "window_title", title)

        found = await get_browser_window(page, backend=backend)
        self.assertEqual(found, 3003)
        self.assertEqual(page.title, "新跳转页面")

    async def test_relocates_when_cached_hwnd_becomes_invalid(self):
        backend = _FakeWin32Backend(hwnd=4001)
        page = _FakePage("课程")
        page.on_title_set = lambda title: setattr(backend, "window_title", title)

        self.assertEqual(await get_browser_window(page, backend=backend), 4001)
        # 模拟原窗口句柄失效，换成新句柄
        backend.valid_hwnds = {4002}
        backend.hwnd = 4002
        self.assertEqual(await get_browser_window(page, backend=backend), 4002)

    async def test_non_windows_reports_actual_platform_and_brings_tab_to_front(self):
        page = _FakePage("课程")
        with (
            mock.patch.object(window_control, "_get_backend", return_value=None),
            mock.patch.object(window_control.sys, "platform", "linux"),
        ):
            self.assertFalse(await hide_window(page))
            self.assertTrue(any("当前系统 不支持" in msg for msg in self.logger.warns))
            self.assertTrue(await display_window(page))
            self.assertEqual(page.bring_to_front_calls, 1)


if __name__ == "__main__":
    unittest.main()
