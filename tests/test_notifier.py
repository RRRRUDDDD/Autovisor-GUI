import asyncio
import configparser
import os
import sys
import unittest
from unittest.mock import Mock, patch

import requests

from modules.notifier import (
    NOTIFICATION_TIMEOUT,
    PROVIDER_REGISTRY,
    Bark,
    Notification,
    Notifier,
    Qmsg,
    ServerChan,
    Telegram,
    Windows,
    mask_secret,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"ok": True, "code": 200}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, post_responses=None):
        self.post_calls = []
        self._post_responses = list(post_responses or [_FakeResp()])

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return (
            self._post_responses.pop(0)
            if len(self._post_responses) > 1
            else self._post_responses[0]
        )


class NotificationServiceTests(unittest.TestCase):
    """通知服务渠道与生命周期契约测试。"""

    def test_all_http_notifications_are_bounded_and_retry_exactly_once(self):
        for provider in (Bark, Qmsg, ServerChan, Telegram):
            with self.subTest(provider=provider.__name__):
                service = provider()
                service.url = "https://example.invalid/notification"
                with patch("modules.notifier.requests.post", side_effect=requests.Timeout) as post:
                    self.assertFalse(service.send("offline test"))
                self.assertEqual(post.call_count, 2)
                for call in post.call_args_list:
                    self.assertEqual(call.kwargs["timeout"], NOTIFICATION_TIMEOUT)

    def test_send_recovers_when_the_retry_succeeds(self):
        service = Bark()
        service.url = "https://example.invalid/notification"
        ok = Mock(status_code=200)
        ok.raise_for_status.return_value = None
        ok.json.return_value = {"code": 200}
        with patch(
            "modules.notifier.requests.post",
            side_effect=[requests.Timeout("first attempt"), ok],
        ) as post:
            self.assertTrue(service.send("retry works"))
        self.assertEqual(post.call_count, 2)

    def test_disabled_service_reports_success_without_sending(self):
        service = ServerChan()
        service.disabled = True
        with patch("modules.notifier.requests.post") as post:
            self.assertTrue(service.send("not configured"))
        post.assert_not_called()

    def test_factory_rejects_unknown_provider(self):
        factory = Notification()
        factory.config_set({"provider": "UnknownProvider", "url": "https://example.com"})
        service = factory.get_notification_from_config()
        self.assertTrue(service.disabled)

    def test_config_example_notification_keys_match_the_code(self):
        expected_names = {"ServerChan", "Qmsg", "Bark", "Telegram", "Windows"}
        self.assertEqual(set(PROVIDER_REGISTRY), expected_names)
        parser = configparser.ConfigParser()
        with open(os.path.join(ROOT, "config.ini.example"), encoding="utf8") as fh:
            parser.read_file(fh)
        self.assertIn("notification", parser)
        section = parser["notification"]
        self.assertIn("provider", section)
        self.assertIn("url", section)
        self.assertIn("tg_chat_id", section)
        provider = section.get("provider", "").strip()
        self.assertTrue(provider == "" or provider in PROVIDER_REGISTRY)


class WindowsToastHereStringTests(unittest.TestCase):
    def test_static_script_never_interpolates_payload(self):
        payloads = [
            "line one\n'@evil\nline two",
            "prefix\r'@; calc.exe; @'\nsuffix",
            "xml chars <tag> & \"quote\" > 'single'",
            "Traceback (most recent call last):\n  File \"a.py\", line 1\r'@; Start-Process calc\nValueError: boom",
        ]
        baseline = Windows._build_script()
        self.assertIn("$env:AUTOVISOR_TOAST_TEXT", baseline)
        self.assertIn("CreateTextNode", baseline)
        for payload in payloads:
            with self.subTest(payload=payload):
                script = Windows._build_script(payload)
                self.assertEqual(script, baseline)
                self.assertNotIn("calc.exe", script)
                self.assertNotIn("Start-Process", script)
                self.assertNotIn("evil", script)
                self.assertNotIn("<tag>", script)

    def test_run_powershell_passes_message_via_env_without_script_interpolation(self):
        payload = "err\r'@; calc.exe; @'\n<tag>&more"
        fake_res = Mock(returncode=0, stderr=b"")
        with patch("modules.notifier.subprocess.run", return_value=fake_res) as run_mock:
            ok, err = Windows._run_powershell(Windows._build_script(), payload)
        self.assertTrue(ok)
        self.assertEqual(err, "")
        cmd = run_mock.call_args.args[0]
        script_arg = cmd[-1]
        env_arg = run_mock.call_args.kwargs["env"]
        self.assertNotIn("calc.exe", script_arg)
        self.assertNotIn("<tag>", script_arg)
        self.assertEqual(env_arg["AUTOVISOR_TOAST_TEXT"], payload)


class TelegramContractTests(unittest.TestCase):
    def test_failure_returns_false_and_retries(self):
        service = Telegram()
        service.url = "https://example.invalid/notification"
        service.tg_chat_id = "cid"
        bad = Mock(status_code=500)
        bad.raise_for_status.side_effect = requests.HTTPError("500")
        ok = Mock(status_code=200)
        ok.raise_for_status.return_value = None
        ok.json.return_value = {"ok": False}
        with patch("modules.notifier.requests.post", side_effect=[bad, ok]) as post:
            self.assertFalse(service.send("retry fails"))
        self.assertEqual(post.call_count, 2)
        for call in post.call_args_list:
            self.assertEqual(call.kwargs["timeout"], NOTIFICATION_TIMEOUT)

    def test_special_characters_are_escaped(self):
        service = Telegram()
        service.url = "https://example.invalid/notification"
        service.tg_chat_id = "cid"
        ok = Mock(status_code=200)
        ok.raise_for_status.return_value = None
        ok.json.return_value = {"ok": True}
        with patch("modules.notifier.requests.post", return_value=ok) as post:
            service.send('<tag>&"weird"')
        body = post.call_args.kwargs["data"]
        self.assertNotIn("<tag>", body["text"])
        self.assertIn("&lt;tag&gt;", body["text"])
        self.assertIn("&amp;", body["text"])


class NotifierWrapperTests(unittest.TestCase):
    def test_mask_secret(self):
        self.assertEqual(mask_secret(""), "")
        self.assertEqual(mask_secret("short"), "***")
        self.assertEqual(mask_secret("1234567890abcdef"), "1234***cdef")
        self.assertEqual(
            mask_secret("https://api.day.app/secretkey123"), "https://api.day.app/***"
        )

    def test_send_serverchan_qmsg_bark_telegram(self):
        cases = [
            ("ServerChan", "https://sctapi.ftqq.com/key.send", ""),
            ("Qmsg", "https://qmsg.zendee.cn/send/key", ""),
            ("Bark", "https://api.day.app/devicekey/", ""),
            ("Telegram", "https://api.telegram.org/bot123/sendMessage", "987654"),
        ]
        for provider, url, chat_id in cases:
            with self.subTest(provider=provider):
                session = _FakeSession()
                notifier = Notifier(
                    provider=provider,
                    url=url,
                    tg_chat_id=chat_id,
                    session=session,
                )
                self.assertTrue(notifier.enabled)
                self.assertEqual(notifier.configured_channels(), [provider])
                results = notifier.send("测试标题", "测试内容")
                self.assertTrue(results[provider][0])
                self.assertEqual(len(session.post_calls), 1)
                self.assertEqual(session.post_calls[0][0], url)

    def test_telegram_requires_chat_id(self):
        notifier = Notifier(
            provider="Telegram",
            url="https://api.telegram.org/bot123/sendMessage",
            tg_chat_id="",
        )
        self.assertFalse(notifier.enabled)
        self.assertEqual(notifier.configured_channels(), [])

    def test_windows_provider_sends_single_toast_without_extra_probe(self):
        notifier = Notifier(provider="Windows")
        with patch.object(Windows, "_run_powershell", return_value=(True, "")) as run_ps:
            with patch("modules.notifier.sys.platform", "win32"):
                self.assertTrue(notifier.enabled)
                results = notifier.send("Autovisor", "任务已完成")
        self.assertTrue(results["Windows"][0])
        self.assertEqual(run_ps.call_count, 1)
        script_arg, msg_arg = run_ps.call_args[0]
        self.assertNotIn("任务已完成", script_arg)
        self.assertIn("任务已完成", msg_arg)

    def test_event_filters_respect_flags(self):
        session = _FakeSession()
        notifier = Notifier(
            provider="Bark",
            url="https://api.day.app/mykey/",
            notify_on_verify=False,
            notify_on_finish=True,
            notify_on_error=False,
            session=session,
        )
        self.assertEqual(asyncio.run(notifier.notify_verify("captcha")), {})
        self.assertEqual(asyncio.run(notifier.notify_error("boom")), {})
        res = asyncio.run(notifier.notify_finish("done"))
        self.assertTrue(res["Bark"][0])
        self.assertEqual(len(session.post_calls), 1)

    def test_proxy_forwarded_when_using_requests(self):
        ok = _FakeResp()
        notifier = Notifier(
            provider="ServerChan",
            url="https://sctapi.ftqq.com/key.send",
            proxy="http://127.0.0.1:7890",
        )
        with patch("modules.notifier.requests.post", return_value=ok) as post:
            res = notifier.send("标题", "内容")
        self.assertTrue(res["ServerChan"][0])
        self.assertEqual(
            post.call_args.kwargs["proxies"],
            {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"},
        )

    def test_proxy_persists_across_retries_and_multiple_sends_with_session(self):
        fake_session = _FakeSession()
        notifier = Notifier(
            provider="ServerChan",
            url="https://sctapi.ftqq.com/key.send",
            proxy="http://127.0.0.1:7890",
            session=fake_session,
        )
        r1 = notifier.send("标题1", "内容1")
        r2 = notifier.send("标题2", "内容2")
        self.assertTrue(r1["ServerChan"][0])
        self.assertTrue(r2["ServerChan"][0])
        self.assertEqual(len(fake_session.post_calls), 2)
        expected_proxies = {
            "http": "http://127.0.0.1:7890",
            "https": "http://127.0.0.1:7890",
        }
        self.assertEqual(fake_session.post_calls[0][1]["proxies"], expected_proxies)
        self.assertEqual(fake_session.post_calls[1][1]["proxies"], expected_proxies)

    def test_test_notification_config_validates_and_sends(self):
        from modules.notifier import test_notification_config

        with self.assertRaises(ValueError):
            test_notification_config("")
        with self.assertRaises(ValueError):
            test_notification_config("Bark", url="not-a-url")
        with self.assertRaises(ValueError):
            test_notification_config("Telegram", url="https://api.telegram.org/botx/sendMessage")

        with patch("modules.notifier.requests.post", return_value=_FakeResp()):
            msg = test_notification_config("Bark", url="https://api.day.app/key/")
            self.assertEqual(msg, "Bark通知发送成功")


if __name__ == "__main__":
    unittest.main()
