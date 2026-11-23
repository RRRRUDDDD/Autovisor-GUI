import unittest
from unittest import mock

from playwright.async_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError

from modules.login import (
    LOGIN_ERROR_SELECTORS,
    LOGIN_TAB_SELECTOR,
    is_login_page,
    perform_password_login,
    wait_for_login_page_settled,
)
from modules.slider import image_to_display_point
from modules.browser import _launch_args, create_browser_session


class _FakeLocator:
    def __init__(self, count_val=0, visible=False, checked=True):
        self._count = count_val
        self._visible = visible
        self._checked = checked
        self.clicked = False
        self.evaluated = False

    @property
    def first(self):
        return self

    async def count(self):
        return self._count

    async def is_visible(self):
        return self._visible

    async def click(self, timeout=None):
        self.clicked = True

    async def is_checked(self):
        return self._checked

    async def evaluate(self, script):
        self.evaluated = True
        self._checked = True


class _FakeElement:
    def __init__(self, fail_once=None):
        self.value = None
        self.clicked = False
        self._fail_once = fail_once

    async def fill(self, value):
        if self._fail_once:
            err = self._fail_once
            self._fail_once = None
            raise err
        self.value = value

    async def click(self):
        self.clicked = True


class _FakePage:
    def __init__(self, url, elements, redirect_to=None):
        self.url = url
        self._elements = elements
        self._redirect_to = redirect_to
        self.checkbox = _FakeLocator(count_val=1, visible=False, checked=False)
        self.tab = _FakeLocator(count_val=1, visible=True)

    async def wait_for_url(self, predicate, wait_until=None, timeout=None):
        if self._redirect_to:
            self.url = self._redirect_to
            self._redirect_to = None
            return
        raise PlaywrightTimeoutError("timeout")

    def locator(self, selector):
        if "el-checkbox__original" in selector:
            return self.checkbox
        return self.tab

    async def wait_for_selector(self, selector, state=None, timeout=None):
        return self._elements[selector]

    async def wait_for_timeout(self, ms):
        return None


class LoginUrlTests(unittest.TestCase):
    def test_recognizes_current_and_legacy_login_hosts(self):
        self.assertTrue(is_login_page("https://login.zhihuishu.com/?origin=zhs"))
        self.assertTrue(is_login_page("https://passport.zhihuishu.com/login"))

    def test_rejects_authenticated_destination_hosts(self):
        self.assertFalse(is_login_page("https://www.zhihuishu.com/"))
        self.assertFalse(is_login_page("https://onlineweb.zhihuishu.com/"))

    def test_does_not_match_login_text_outside_the_hostname(self):
        self.assertFalse(is_login_page("https://example.com/login.zhihuishu.com"))

    def test_selectors_support_new_element_plus_login_page(self):
        self.assertIn("tab-1.1", LOGIN_TAB_SELECTOR)
        self.assertIn(".el-message__content", LOGIN_ERROR_SELECTORS)
        self.assertIn(".el-form-item__error", LOGIN_ERROR_SELECTORS)

    def test_converts_opencv_point_to_display_coordinates(self):
        self.assertEqual(
            image_to_display_point((240, 120), 480, 240, 400, 200),
            (200, 100),
        )

    def test_browser_starts_maximized(self):
        from types import SimpleNamespace
        import os

        launch = _launch_args(SimpleNamespace(driver="chrome", exe_path="", proxy=""))
        self.assertIn("--window-position=0,0", launch["args"])
        self.assertIn("--no-default-browser-check", launch["args"])
        self.assertIn("--no-first-run", launch["args"])
        if os.name == "nt":
            self.assertIn("--start-maximized", launch["args"])
        self.assertIn(("viewport",), create_browser_session.__code__.co_consts)


class PasswordLoginFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_waits_for_passport_redirect_before_filling(self):
        user_el = _FakeElement()
        pwd_el = _FakeElement()
        submit_el = _FakeElement()
        from modules.login import (
            LOGIN_PASSWORD_SELECTOR,
            LOGIN_SUBMIT_SELECTOR,
            LOGIN_USERNAME_SELECTOR,
        )

        page = _FakePage(
            "https://passport.zhihuishu.com/login",
            {
                LOGIN_USERNAME_SELECTOR: user_el,
                LOGIN_PASSWORD_SELECTOR: pwd_el,
                LOGIN_SUBMIT_SELECTOR: submit_el,
            },
            redirect_to="https://login.zhihuishu.com/?origin=zhs",
        )
        await perform_password_login(page, "13800138000", "secret")
        self.assertEqual(page.url, "https://login.zhihuishu.com/?origin=zhs")
        self.assertEqual(user_el.value, "13800138000")
        self.assertEqual(pwd_el.value, "secret")
        self.assertTrue(page.checkbox.evaluated)
        self.assertTrue(submit_el.clicked)

    async def test_retries_once_on_navigation_context_destroyed(self):
        user_el = _FakeElement(
            fail_once=PlaywrightError(
                "Execution context was destroyed, most likely because of a navigation"
            )
        )
        pwd_el = _FakeElement()
        submit_el = _FakeElement()
        from modules.login import (
            LOGIN_PASSWORD_SELECTOR,
            LOGIN_SUBMIT_SELECTOR,
            LOGIN_USERNAME_SELECTOR,
        )

        page = _FakePage(
            "https://login.zhihuishu.com/?origin=zhs",
            {
                LOGIN_USERNAME_SELECTOR: user_el,
                LOGIN_PASSWORD_SELECTOR: pwd_el,
                LOGIN_SUBMIT_SELECTOR: submit_el,
            },
        )
        await perform_password_login(page, "13800138000", "secret")
        self.assertEqual(user_el.value, "13800138000")
        self.assertEqual(pwd_el.value, "secret")
        self.assertTrue(submit_el.clicked)

    async def test_settle_helper_skips_when_already_on_new_login(self):
        page = _FakePage("https://login.zhihuishu.com/?origin=zhs", {})
        with mock.patch.object(page, "wait_for_url", side_effect=AssertionError("should not wait")):
            await wait_for_login_page_settled(page)


if __name__ == "__main__":
    unittest.main()
