import argparse
import os
import tempfile
import unittest

from modules.configs import Config, ConfigError
from modules.http_utils import (
    build_playwright_proxy,
    build_requests_proxies,
    create_retry_session,
    normalize_proxy_url,
)


class HttpUtilsTests(unittest.TestCase):
    def test_empty_proxy(self):
        self.assertEqual(normalize_proxy_url(""), "")
        self.assertEqual(normalize_proxy_url(None), "")
        self.assertEqual(build_requests_proxies(""), {})
        self.assertIsNone(build_playwright_proxy(""))

    def test_valid_proxies(self):
        self.assertEqual(
            normalize_proxy_url("http://127.0.0.1:7890"),
            "http://127.0.0.1:7890",
        )
        self.assertEqual(
            build_requests_proxies("socks5://127.0.0.1:1080"),
            {
                "http": "socks5://127.0.0.1:1080",
                "https": "socks5://127.0.0.1:1080",
            },
        )

    def test_playwright_proxy_with_auth(self):
        cfg = build_playwright_proxy("http://alice:secret@127.0.0.1:8080")
        self.assertEqual(
            cfg,
            {
                "server": "http://127.0.0.1:8080",
                "username": "alice",
                "password": "secret",
            },
        )

    def test_invalid_proxy_raises(self):
        with self.assertRaises(ValueError):
            normalize_proxy_url("ftp://127.0.0.1:21")
        with self.assertRaises(ValueError):
            normalize_proxy_url("127.0.0.1:7890")

    def test_create_retry_session_mounts_adapters_and_proxies(self):
        session = create_retry_session(proxy="http://127.0.0.1:7890", total_retries=2)
        try:
            self.assertEqual(session.proxies["http"], "http://127.0.0.1:7890")
            self.assertEqual(session.proxies["https"], "http://127.0.0.1:7890")
            self.assertEqual(session.get_adapter("https://example.com").max_retries.total, 2)
        finally:
            session.close()


class ConfigExtensionsAndCliOverrideTests(unittest.TestCase):
    def _make_config(self, extra_ini: str = "") -> tuple[Config, str]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        data_dir = os.path.join(tmp.name, "data")
        os.makedirs(data_dir, exist_ok=True)
        mirrors_path = os.path.join(data_dir, "mirrors.json")
        with open(mirrors_path, "w", encoding="utf-8") as f:
            f.write('{"Official": "https://pypi.org"}\n')
        config_path = os.path.join(tmp.name, "config.ini")
        base_ini = (
            "[user-account]\n"
            "username =\n"
            "password =\n"
            "[browser-option]\n"
            "driver = Edge\n"
            "EXE_PATH =\n"
            "[script-option]\n"
            "enableAutoCaptcha = True\n"
            "enableHideWindow = False\n"
            "[course-option]\n"
            "limitMaxTime = 30\n"
            "limitSpeed = 1.0\n"
            "soundOff = True\n"
            "[course-url]\n"
            "URL1 = https://studyvideoh5.zhihuishu.com/stuStudy?recruitAndCourseId=abc\n"
        )
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(base_ini + "\n" + extra_ini)
        return Config(config_path, mirrors_path), config_path

    def test_optional_notification_and_proxy_sections(self):
        cfg, _ = self._make_config(
            "[notification]\n"
            "provider = Telegram\n"
            "url = https://api.telegram.org/bot123:abc/sendMessage\n"
            "tg_chat_id = 123456789\n"
            "notifyOnVerify = False\n"
            "notifyOnFinish = True\n"
            "notifyOnError = False\n"
        )
        self.assertEqual(cfg.notification_provider, "Telegram")
        self.assertEqual(cfg.notification_url, "https://api.telegram.org/bot123:abc/sendMessage")
        self.assertEqual(cfg.notification_tg_chat_id, "123456789")
        self.assertFalse(cfg.notify_on_verify)
        self.assertTrue(cfg.notify_on_finish)
        self.assertFalse(cfg.notify_on_error)

    def test_legacy_bark_url_falls_back_to_bark_provider(self):
        cfg, _ = self._make_config(
            "[notification]\n"
            "barkUrl = https://api.day.app/mykey\n"
        )
        self.assertEqual(cfg.notification_provider, "Bark")
        self.assertEqual(cfg.notification_url, "https://api.day.app/mykey")

    def test_cli_overrides_take_precedence_over_ini(self):
        cfg, _ = self._make_config()
        args = argparse.Namespace(
            course=["https://wisdom-mooc.zhihuishu.com/study/1"],
            speed=2.5,
            limit=15,
            proxy="socks5://127.0.0.1:1080",
            hide_window=True,
            qr_login=False,
            qr_terminal=True,
            qr_unicode=True,
            qr_save_dir="qr_out",
        )
        cfg.apply_cli_overrides(args)
        self.assertEqual(cfg.course_urls, ["https://wisdom-mooc.zhihuishu.com/study/1"])
        self.assertEqual(cfg.limitSpeed, 1.8)
        self.assertEqual(cfg.limitMaxTime, 15.0)
        self.assertEqual(cfg.proxy, "socks5://127.0.0.1:1080")
        self.assertTrue(cfg.enableHideWindow)
        self.assertTrue(cfg.qr_login)
        self.assertTrue(cfg.qr_terminal)
        self.assertTrue(cfg.qr_unicode)
        self.assertEqual(cfg.qr_save_dir, "qr_out")

    def test_cli_invalid_proxy_raises_config_error(self):
        cfg, _ = self._make_config()
        args = argparse.Namespace(proxy="bad-proxy")
        with self.assertRaises(ConfigError):
            cfg.apply_cli_overrides(args)


if __name__ == "__main__":
    unittest.main()
