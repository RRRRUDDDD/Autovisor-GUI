import os
import tempfile
import unittest
from pathlib import Path

from modules.browser import (
    BrowserSession,
    create_browser_session,
    get_effective_driver,
    is_loopback_cdp_endpoint,
    resolve_browser_channel,
    resolve_cdp_endpoint,
    resolve_executable_path,
)


class BrowserResolutionTests(unittest.TestCase):
    def test_driver_is_normalized_from_config_alone(self):
        self.assertEqual(get_effective_driver("Edge"), "edge")
        self.assertEqual(get_effective_driver(" chrome "), "chrome")
        self.assertEqual(get_effective_driver("edge"), "edge")

    def test_driver_does_not_read_environment_variables(self):
        with unittest.mock.patch.dict(
            os.environ, {"AUTOVISOR_DRIVER": "chromium"}, clear=False
        ):
            self.assertEqual(get_effective_driver("edge"), "edge")

    def test_channels(self):
        self.assertEqual(resolve_browser_channel("edge"), "msedge")
        self.assertEqual(resolve_browser_channel("chrome"), "chrome")
        self.assertIsNone(resolve_browser_channel("chromium"))

    @unittest.skipIf(os.name == "nt", "POSIX path test")
    def test_macos_app_path(self):
        self.assertEqual(
            resolve_executable_path("chrome", "/Applications/Google Chrome.app"),
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        )

    def test_devtools_active_port_endpoint(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "DevToolsActivePort"
            path.write_text("9222\n/devtools/browser/test-id\n", encoding="utf-8")
            self.assertEqual(
                resolve_cdp_endpoint("http://127.0.0.1:9222", path),
                "ws://127.0.0.1:9222/devtools/browser/test-id",
            )

    def test_cdp_endpoint_must_be_local(self):
        self.assertTrue(is_loopback_cdp_endpoint("ws://127.0.0.1:9222/devtools/browser/x"))
        self.assertTrue(is_loopback_cdp_endpoint("http://localhost:9222"))
        self.assertFalse(is_loopback_cdp_endpoint("ws://example.com:9222/devtools/browser/x"))


class _Page:
    def __init__(self):
        self.closed = False
        self.timeout = None
        self.init_scripts = []

    async def close(self):
        self.closed = True

    def is_closed(self):
        return self.closed

    async def add_init_script(self, **kwargs):
        self.init_scripts.append(kwargs)

    def set_default_timeout(self, timeout):
        self.timeout = timeout


class _Context:
    def __init__(self):
        self.page = _Page()
        self.added_cookies = None

    async def add_cookies(self, cookies):
        self.added_cookies = cookies

    async def new_page(self):
        return self.page


class _Browser:
    def __init__(self):
        self.contexts = [_Context()]
        self.closed = False
        self.new_context_kwargs = None

    async def new_context(self, **kwargs):
        self.new_context_kwargs = kwargs
        return self.contexts[0]

    async def close(self):
        self.closed = True


class _Chromium:
    def __init__(self):
        self.browser = _Browser()
        self.cdp_url = None
        self.launch_kwargs = None

    async def connect_over_cdp(self, url):
        self.cdp_url = url
        return self.browser

    async def launch(self, **kwargs):
        self.launch_kwargs = kwargs
        return self.browser


class _Playwright:
    def __init__(self):
        self.chromium = _Chromium()


class _Config:
    attach_existing_chrome = True
    cdp_url = "http://127.0.0.1:9222"
    driver = "chrome"
    exe_path = ""


class _Logger:
    def __init__(self, root):
        self.runtime_root = root

    def info(self, *_args, **_kwargs):
        pass

    def debug(self, *_args, **_kwargs):
        pass


class BrowserSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_attached_session_closes_only_its_page(self):
        with tempfile.TemporaryDirectory() as root:
            playwright = _Playwright()
            session = await create_browser_session(
                playwright, _Config(), None, _Logger(root)
            )
            self.assertTrue(session.attached)
            await session.close()
            self.assertTrue(session.page.closed)
            self.assertFalse(session.browser.closed)

    async def test_owned_session_closes_browser(self):
        browser = _Browser()
        session = BrowserSession(browser, browser.contexts[0], _Page(), False)
        await session.close()
        self.assertTrue(browser.closed)

    async def test_launch_session_uses_viewport_none_and_injects_stealth(self):
        class _LaunchConfig:
            attach_existing_chrome = False
            cdp_url = "http://127.0.0.1:9222"
            driver = "edge"
            exe_path = ""
            proxy = ""

        with tempfile.TemporaryDirectory() as root:
            playwright = _Playwright()
            session = await create_browser_session(
                playwright, _LaunchConfig(), [{"name": "a", "value": "b"}], _Logger(root)
            )
            self.assertFalse(session.attached)
            self.assertEqual(session.browser.new_context_kwargs, {"viewport": None})
            self.assertEqual(session.context.added_cookies, [{"name": "a", "value": "b"}])
            self.assertIn("--no-first-run", playwright.chromium.launch_kwargs["args"])
            self.assertIn("--no-default-browser-check", playwright.chromium.launch_kwargs["args"])
            self.assertIn(
                "--disable-backgrounding-occluded-windows",
                playwright.chromium.launch_kwargs["args"],
            )
            self.assertIn(
                "--disable-renderer-backgrounding",
                playwright.chromium.launch_kwargs["args"],
            )
            self.assertIn(
                "--disable-background-timer-throttling",
                playwright.chromium.launch_kwargs["args"],
            )
            self.assertTrue(
                any(
                    "stealth.min.js" in str(call.get("path", ""))
                    for call in session.page.init_scripts
                )
            )
