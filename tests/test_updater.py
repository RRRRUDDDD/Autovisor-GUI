import os
import re
import unittest

import requests

from modules import updater
from modules.version import __gui_version__, __version__

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

RELEASE_PAGE = "https://github.com/RRRRUDDDD/Autovisor-GUI/releases/tag/x"
ASSET_URL = (
    "https://github.com/RRRRUDDDD/Autovisor-GUI/releases/download/3.19.0/"
    "Autovisor-3.19.0-windows-amd64.zip"
)


class FakeLogger:
    def __init__(self):
        self.records = []

    def _record(self, level, msg, shift=False):
        self.records.append((level, msg))

    def debug(self, msg):
        self._record("DEBUG", msg)

    def info(self, msg, shift=False):
        self._record("INFO", msg)

    def warn(self, msg, shift=False):
        self._record("WARN", msg)

    def event(self, name, interval=None, **fields):
        details = " ".join(f"{key}={value}" for key, value in fields.items())
        self._record("EVENT", f"{name} | {details}" if details else name)

    def text(self, level=None):
        return "\n".join(msg for lvl, msg in self.records if level is None or lvl == level)

    def summarize_exception(self, exc):
        return f"{type(exc).__name__}: {exc}"


class FakeResponse:
    def __init__(self, payload=None, error=None, *, status_code=200, url="", text="", headers=None):
        self._payload = payload
        self._error = error
        self.status_code = status_code
        self.url = url
        self.text = text
        self.headers = headers or {}

    def raise_for_status(self):
        if self._error:
            raise self._error

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []
        self.kwargs = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        self.kwargs.append(kwargs)
        if self.error:
            raise self.error
        return self.response or FakeResponse({})


class RetrySession(FakeSession):
    """首次抛错, 之后正常返回, 用于验证降级重试。"""

    def get(self, url, **kwargs):
        self.calls.append(url)
        self.kwargs.append(kwargs)
        if len(self.calls) == 1:
            raise self.error
        return self.response


def release_payload(name, html_url, assets=()):
    return {
        "name": name,
        "tag_name": "2026/9/15",
        "html_url": html_url,
        "assets": list(assets),
    }


class ParseVersionTests(unittest.TestCase):
    def test_extracts_version_from_release_name(self):
        self.assertEqual(updater.parse_version("Autovisor-3.18.0"), (3, 18, 0))

    def test_ignores_date_style_tag(self):
        self.assertIsNone(updater.parse_version("2026/9/15"))

    def test_ignores_empty_and_none(self):
        self.assertIsNone(updater.parse_version(""))
        self.assertIsNone(updater.parse_version(None))


class IsNewerTests(unittest.TestCase):
    def test_newer_patch_and_minor(self):
        self.assertTrue(updater.is_newer((3, 18, 1), (3, 18, 0)))
        self.assertTrue(updater.is_newer((3, 19, 0), (3, 18, 9)))

    def test_same_or_older(self):
        self.assertFalse(updater.is_newer((3, 18, 0), (3, 18, 0)))
        self.assertFalse(updater.is_newer((3, 17, 9), (3, 18, 0)))

    def test_unparsable_is_not_newer(self):
        self.assertFalse(updater.is_newer(None, (3, 18, 0)))
        self.assertFalse(updater.is_newer((3, 19, 0), None))


class CheckForUpdateTests(unittest.TestCase):
    def test_prompts_when_release_is_newer(self):
        logger = FakeLogger()
        session = FakeSession(
            FakeResponse(release_payload("Autovisor-3.19.0", RELEASE_PAGE))
        )
        url = updater.check_for_update(logger, current_version="3.18.1", session=session)
        self.assertEqual(url, RELEASE_PAGE)
        self.assertIn("3.19.0", logger.text("INFO"))
        self.assertIn(RELEASE_PAGE, logger.text("INFO"))
        self.assertEqual(logger.text("WARN"), "")

    def test_uses_tag_when_tag_is_a_version(self):
        logger = FakeLogger()
        payload = release_payload("Autovisor", RELEASE_PAGE)
        payload["tag_name"] = "3.19.0"
        session = FakeSession(FakeResponse(payload))
        url = updater.check_for_update(logger, current_version="3.18.1", session=session)
        self.assertEqual(url, RELEASE_PAGE)
        self.assertIn("3.19.0", logger.text("INFO"))

    def test_reports_up_to_date(self):
        logger = FakeLogger()
        payload = release_payload("Autovisor-3.18.1", RELEASE_PAGE)
        payload["tag_name"] = "3.18.1"
        session = FakeSession(FakeResponse(payload))
        self.assertIsNone(
            updater.check_for_update(logger, current_version="3.18.1", session=session)
        )
        self.assertIn("当前版本3.18.1,已是最新版本.", logger.text("INFO"))
        self.assertEqual(logger.text("WARN"), "")

    def test_silent_when_release_version_unparsable(self):
        logger = FakeLogger()
        session = FakeSession(FakeResponse(release_payload("latest", RELEASE_PAGE)))
        self.assertIsNone(
            updater.check_for_update(logger, current_version="3.18.1", session=session)
        )
        self.assertEqual(logger.text("INFO"), "")

    def test_network_error_does_not_raise(self):
        logger = FakeLogger()
        session = FakeSession(error=OSError("network is unreachable"))
        self.assertIsNone(
            updater.check_for_update(logger, current_version="3.18.1", session=session)
        )
        self.assertIn("检查更新失败", logger.text("DEBUG"))
        self.assertEqual(logger.text("INFO"), "")
        self.assertEqual(len(session.calls), 1)

    def test_windows_asset_link_is_included_on_windows(self):
        if os.name != "nt":
            self.skipTest("Windows-only asset matching")
        logger = FakeLogger()
        session = FakeSession(
            FakeResponse(
                release_payload(
                    "Autovisor-3.19.0",
                    RELEASE_PAGE,
                    assets=[
                        {
                            "name": "Autovisor-3.19.0-windows-amd64.zip",
                            "browser_download_url": ASSET_URL,
                        }
                    ],
                )
            )
        )
        updater.check_for_update(logger, current_version="3.18.1", session=session)
        self.assertIn(ASSET_URL, logger.text("INFO"))


class TrustedUrlTests(unittest.TestCase):
    def test_accepts_only_repo_release_urls(self):
        self.assertTrue(
            updater.is_trusted_url(
                f"https://github.com/{updater.REPO}/releases/latest"
            )
        )
        self.assertTrue(
            updater.is_trusted_url(
                f"https://github.com/{updater.REPO}/releases/tag/v3.20.0"
            )
        )
        self.assertTrue(
            updater.is_trusted_url(
                f"https://github.com/{updater.REPO}/releases/download/3.20.0/app.exe"
            )
        )
        self.assertTrue(
            updater.is_trusted_url(f"https://github.com/{updater.REPO}/releases")
        )
        # 大小写无关的仓库路径前缀
        self.assertTrue(
            updater.is_trusted_url(
                f"https://github.com/{updater.REPO.lower()}/releases/latest"
            )
        )

    def test_rejects_other_repos_hosts_traversal_and_schemes(self):
        self.assertFalse(updater.is_trusted_url("https://github.com/a/b"))
        self.assertFalse(
            updater.is_trusted_url(
                "https://github.com/attacker/malware/releases/download/v1/bad.exe"
            )
        )
        self.assertFalse(
            updater.is_trusted_url(
                f"https://github.com/{updater.REPO}-evil/releases/latest"
            )
        )
        self.assertFalse(
            updater.is_trusted_url(
                f"https://github.com/{updater.REPO}/releases/../../attacker/evil"
            )
        )
        self.assertFalse(
            updater.is_trusted_url(
                f"https://github.com/{updater.REPO}/releases%2f..%2f..%2fattacker"
            )
        )
        self.assertFalse(updater.is_trusted_url("https://objects.githubusercontent.com/x"))
        self.assertFalse(updater.is_trusted_url("https://api.github.com/repos/x"))
        self.assertFalse(updater.is_trusted_url("https://example.com/dl.zip"))
        self.assertFalse(updater.is_trusted_url("https://evilgithub.com/dl.zip"))
        self.assertFalse(
            updater.is_trusted_url(f"http://github.com/{updater.REPO}/releases/latest")
        )
        self.assertFalse(updater.is_trusted_url(None))
        self.assertFalse(updater.is_trusted_url(""))


class SslFallbackTests(unittest.TestCase):
    def test_ssl_error_does_not_downgrade_tls_verification(self):
        logger = FakeLogger()
        session = RetrySession(
            FakeResponse(
                release_payload(
                    "Autovisor-3.19.0",
                    RELEASE_PAGE,
                    assets=[
                        {
                            "name": "Autovisor-3.19.0-windows-amd64.zip",
                            "browser_download_url": ASSET_URL,
                        }
                    ],
                )
            ),
            error=requests.exceptions.SSLError("certificate verify failed"),
        )
        url = updater.check_for_update(logger, current_version="3.18.1", session=session)
        self.assertIsNone(url)
        # 绝不再以 verify=False 重试，仅发起一次校验请求
        self.assertEqual(len(session.calls), 1)
        self.assertNotIn("verify", session.kwargs[0])
        self.assertIn("证书校验失败", logger.text("DEBUG"))
        self.assertEqual(logger.text("INFO"), "")

    def test_ssl_error_is_silent_and_never_retries_insecurely(self):
        logger = FakeLogger()
        session = FakeSession(error=requests.exceptions.SSLError("certificate verify failed"))
        self.assertIsNone(
            updater.check_for_update(logger, current_version="3.18.1", session=session)
        )
        self.assertEqual(len(session.calls), 1)
        self.assertIn("证书校验失败", logger.text("DEBUG"))
        self.assertEqual(logger.text("INFO"), "")

    def test_untrusted_release_page_falls_back_to_official_page(self):
        logger = FakeLogger()
        session = FakeSession(
            FakeResponse(release_payload("Autovisor-3.19.0", "https://evil.com/update"))
        )
        url = updater.check_for_update(logger, current_version="3.18.1", session=session)
        self.assertEqual(url, updater.RELEASE_PAGE_URL)
        self.assertNotIn("evil.com", logger.text("INFO"))

    def test_untrusted_asset_url_is_ignored(self):
        if os.name != "nt":
            self.skipTest("Windows-only asset matching")
        logger = FakeLogger()
        session = FakeSession(
            FakeResponse(
                release_payload(
                    "Autovisor-3.19.0",
                    RELEASE_PAGE,
                    assets=[
                        {
                            "name": "Autovisor-3.19.0-windows-amd64.zip",
                            "browser_download_url": "https://evil.com/payload.zip",
                        }
                    ],
                )
            )
        )
        updater.check_for_update(logger, current_version="3.18.1", session=session)
        self.assertNotIn("evil.com", logger.text("INFO"))
        self.assertIn(RELEASE_PAGE, logger.text("INFO"))


class VersionSourceTests(unittest.TestCase):
    def test_pyproject_takes_version_from_code(self):
        with open(os.path.join(PROJECT_ROOT, "pyproject.toml"), "r", encoding="utf-8") as file:
            content = file.read()
        self.assertIn('dynamic = ["version"]', content)
        self.assertIn('attr = "modules.version.__version__"', content)
        self.assertIsNone(
            re.search(r'^version\s*=\s*"', content, re.MULTILINE),
            "pyproject.toml 不应再写死 version",
        )
        self.assertEqual(updater.__version__, __version__)
        self.assertEqual(__version__, "3.20.0(Autovisor GUI Edition)")
        self.assertEqual(__gui_version__, "1.1.0")

    def test_desktop_and_python_versions_are_in_sync(self):
        import json

        tauri_conf_path = os.path.join(PROJECT_ROOT, "gui", "src-tauri", "tauri.conf.json")
        with open(tauri_conf_path, "r", encoding="utf-8") as f:
            tauri_conf = json.load(f)
        self.assertEqual(tauri_conf.get("version"), __gui_version__)

        cargo_toml_path = os.path.join(PROJECT_ROOT, "gui", "src-tauri", "Cargo.toml")
        with open(cargo_toml_path, "r", encoding="utf-8") as f:
            cargo_toml = f.read()
        cargo_match = re.search(r'^version\s*=\s*"([^"]+)"', cargo_toml, re.MULTILINE)
        self.assertIsNotNone(cargo_match, "Cargo.toml missing package version")
        self.assertEqual(cargo_match.group(1), __gui_version__)

        package_json_path = os.path.join(PROJECT_ROOT, "gui", "package.json")
        with open(package_json_path, "r", encoding="utf-8") as f:
            package_json = json.load(f)
        self.assertEqual(package_json.get("version"), __gui_version__)


class CheckUpdateInfoTests(unittest.TestCase):
    def test_returns_structured_info_with_windows_setup_exe_priority(self):
        setup_url = (
            "https://github.com/RRRRUDDDD/Autovisor-GUI/releases/download/3.19.0/"
            "Autovisor-GUI-Setup-3.19.0-windows-x64.exe"
        )
        payload = {
            "name": "Autovisor GUI v3.19.0",
            "tag_name": "v3.19.0",
            "html_url": RELEASE_PAGE,
            "body": "## 更新内容\n- 自动检查更新",
            "published_at": "2026-11-20T10:00:00Z",
            "assets": [
                {
                    "name": "Autovisor-3.19.0-windows-amd64.zip",
                    "browser_download_url": ASSET_URL,
                },
                {
                    "name": "Autovisor-GUI-Setup-3.19.0-windows-x64.exe",
                    "browser_download_url": setup_url,
                },
                {
                    "name": "Autovisor-GUI-3.19.0-macos.dmg",
                    "browser_download_url": "https://github.com/RRRRUDDDD/Autovisor-GUI/releases/download/3.19.0/Autovisor-GUI-3.19.0-macos.dmg",
                },
                {
                    "name": "Autovisor-GUI-3.19.0-linux.AppImage",
                    "browser_download_url": "https://github.com/RRRRUDDDD/Autovisor-GUI/releases/download/3.19.0/Autovisor-GUI-3.19.0-linux.AppImage",
                },
            ],
        }
        self.assertEqual(updater.pick_download_url(payload, platform="win32"), setup_url)
        self.assertTrue(
            updater.pick_download_url(payload, platform="darwin").endswith(".dmg")
        )
        self.assertTrue(
            updater.pick_download_url(payload, platform="linux").endswith(".AppImage")
        )

        session = FakeSession(FakeResponse(payload))
        info = updater.check_update_info(current_version="3.18.4", session=session)
        self.assertEqual(info["currentVersion"], "3.18.4")
        self.assertEqual(info["latestVersion"], "3.19.0")
        self.assertTrue(info["hasUpdate"])
        self.assertEqual(info["releaseName"], "Autovisor GUI v3.19.0")
        self.assertEqual(info["releaseUrl"], RELEASE_PAGE)
        self.assertIn("自动检查更新", info["notes"])
        self.assertEqual(info["publishedAt"], "2026-11-20T10:00:00Z")
        if os.name == "nt":
            self.assertEqual(info["downloadUrl"], setup_url)

    def test_check_update_info_rejects_ssl_error_without_insecure_retry(self):
        payload = {
            "name": "Autovisor-3.18.4",
            "tag_name": "3.18.4",
            "html_url": "https://evil.example/release",
            "body": "notes",
            "published_at": "2026-11-20T00:00:00Z",
            "assets": [],
        }
        session = RetrySession(
            FakeResponse(payload),
            error=requests.exceptions.SSLError("cert failed"),
        )
        with self.assertRaises(RuntimeError) as ctx:
            updater.check_update_info(current_version="3.18.4", session=session)
        self.assertIn("证书校验失败", str(ctx.exception))
        self.assertEqual(len(session.calls), 1)
        self.assertNotIn("verify", session.kwargs[0])

        fail_session = FakeSession(error=OSError("offline"))
        with self.assertRaises(RuntimeError):
            updater.check_update_info(current_version="3.18.4", session=fail_session)


class RoutedSession:
    """按 URL 路由返回不同响应，用于验证 API 限流降级到网页端。"""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []
        self.kwargs = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        self.kwargs.append(kwargs)
        result = self.handler(url, kwargs)
        if isinstance(result, Exception):
            raise result
        return result


def make_http_error_response(status_code, message="HTTP error", headers=None):
    resp = FakeResponse(status_code=status_code, headers=headers)
    resp._error = requests.exceptions.HTTPError(message, response=resp)
    return resp


class RateLimitWebFallbackTests(unittest.TestCase):
    def setUp(self):
        updater._API_RATE_LIMIT_UNTIL.clear()

    def tearDown(self):
        updater._API_RATE_LIMIT_UNTIL.clear()

    def test_falls_back_to_github_web_on_api_403_rate_limit(self):
        tag_url = "https://github.com/RRRRUDDDD/Autovisor-GUI/releases/tag/v3.19.0"
        setup_path = (
            "/RRRRUDDDD/Autovisor-GUI/releases/download/v3.19.0/"
            "Autovisor-GUI-Setup-3.19.0-windows-x64.exe"
        )
        assets_html = f'''
        <ul>
          <li><a href="{setup_path}">Autovisor-GUI-Setup-3.19.0-windows-x64.exe</a></li>
          <li><a href="https://evil.example/releases/download/v3.19.0/malware-windows-x64.exe">bad</a></li>
        </ul>
        '''
        atom_xml = f'''<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry>
            <id>tag:github.com,2008:Repository/1/v3.19.0</id>
            <updated>2026-11-23T08:00:00Z</updated>
            <link rel="alternate" type="text/html" href="{tag_url}"/>
            <title>Autovisor GUI v3.19.0</title>
            <content type="html">&lt;h2&gt;更新内容&lt;/h2&gt;&lt;ul&gt;&lt;li&gt;修复 API 限流&lt;/li&gt;&lt;/ul&gt;</content>
          </entry>
        </feed>
        '''

        def handler(url, _kwargs):
            if url == updater.LATEST_RELEASE_API:
                return make_http_error_response(403, "API rate limit exceeded")
            if url == updater.RELEASE_PAGE_URL:
                return FakeResponse(
                    payload={"id": 1, "tag_name": "v3.19.0"},
                    url=tag_url,
                )
            if "expanded_assets/v3.19.0" in url:
                return FakeResponse(text=assets_html)
            if url == updater.RELEASES_ATOM_URL:
                return FakeResponse(text=atom_xml)
            raise AssertionError(f"Unexpected URL: {url}")

        session = RoutedSession(handler)
        info = updater.check_update_info(current_version="3.18.4", session=session)
        self.assertTrue(info["hasUpdate"])
        self.assertEqual(info["latestVersion"], "3.19.0")
        self.assertEqual(info["releaseName"], "Autovisor GUI v3.19.0")
        self.assertEqual(info["releaseUrl"], tag_url)
        self.assertEqual(info["publishedAt"], "2026-11-23T08:00:00Z")
        self.assertIn("修复 API 限流", info["notes"])
        if os.name == "nt":
            self.assertEqual(info["downloadUrl"], f"https://github.com{setup_path}")

    def test_falls_back_using_redirect_url_when_web_response_is_html_and_enrich_fails(self):
        tag_url = "https://github.com/RRRRUDDDD/Autovisor-GUI/releases/tag/v3.20.0"

        def handler(url, _kwargs):
            if url == updater.LATEST_RELEASE_API:
                return make_http_error_response(429, "Too Many Requests")
            if url == updater.RELEASE_PAGE_URL:
                return FakeResponse(
                    payload=ValueError("not json"),
                    url=tag_url,
                    text="<html>release page</html>",
                )
            return OSError("enrichment unavailable")

        session = RoutedSession(handler)
        info = updater.check_update_info(current_version="3.18.4", session=session)
        self.assertTrue(info["hasUpdate"])
        self.assertEqual(info["latestVersion"], "3.20.0")
        self.assertEqual(info["releaseUrl"], tag_url)
        self.assertEqual(info["downloadUrl"], "")

    def test_reports_no_release_when_web_fallback_redirects_to_releases_index(self):
        def handler(url, _kwargs):
            if url == updater.LATEST_RELEASE_API:
                return make_http_error_response(403, "API rate limit exceeded")
            if url == updater.RELEASE_PAGE_URL:
                return FakeResponse(
                    payload={"payload": {"releasesIndexRoute": {}}},
                    url="https://github.com/RRRRUDDDD/Autovisor-GUI/releases",
                )
            raise AssertionError(f"Unexpected URL: {url}")

        session = RoutedSession(handler)
        with self.assertRaises(RuntimeError) as ctx:
            updater.check_update_info(current_version="3.18.4", session=session)
        self.assertIn("未找到可用的发行版本", str(ctx.exception))

    def test_reports_rate_limit_and_fallback_failure_when_both_fail(self):
        def handler(url, _kwargs):
            if url == updater.LATEST_RELEASE_API:
                return make_http_error_response(403, "API rate limit exceeded")
            if url == updater.RELEASE_PAGE_URL:
                return OSError("connection timed out")
            raise AssertionError(f"Unexpected URL: {url}")

        session = RoutedSession(handler)
        with self.assertRaises(RuntimeError) as ctx:
            updater.check_update_info(current_version="3.18.4", session=session)
        self.assertIn("GitHub API 请求受限", str(ctx.exception))
        self.assertIn("备用通道访问失败", str(ctx.exception))

    def test_rejects_untrusted_redirect_in_web_fallback(self):
        def handler(url, _kwargs):
            if url == updater.LATEST_RELEASE_API:
                return make_http_error_response(403, "API rate limit exceeded")
            if url == updater.RELEASE_PAGE_URL:
                return FakeResponse(
                    payload={"tag_name": "v9.9.9"},
                    url="https://evil.example/releases/tag/v9.9.9",
                )
            raise AssertionError(f"Unexpected URL: {url}")

        session = RoutedSession(handler)
        with self.assertRaises(RuntimeError) as ctx:
            updater.check_update_info(current_version="3.18.4", session=session)
        self.assertIn("非 GitHub 官方域名", str(ctx.exception))

    def test_rate_limit_cooldown_is_scoped_by_proxy(self):
        resp = make_http_error_response(
            403,
            "rate limit",
            headers={"X-RateLimit-Reset": str(int(updater.time.time()) + 600)},
        )
        wait_seconds = updater._record_api_rate_limit("http://127.0.0.1:7890", resp)
        self.assertIsNotNone(wait_seconds)
        self.assertTrue(updater._is_api_rate_limited("http://127.0.0.1:7890"))
        self.assertFalse(updater._is_api_rate_limited(""))


if __name__ == "__main__":
    unittest.main()
