import json
import threading
import unittest

import requests

from modules import zhidao_crypto, zhs_api
from modules.exam_ctx import ExamApi, ExamApiError
from modules.zhs_api import (
    CaptchaRequiredError,
    SessionExpiredError,
    SessionStatus,
    ZhsApiClient,
    ZhsApiError,
)

SUCCESS_PAYLOADS = {
    "home": {"code": 0},
    "home_share": {"code": 200},
    "home_ai": {"status": "200", "rt": []},
    "zhidao": {"code": 0},
    "ai_exam": {"code": 0},
    "ai_exam_submit": {},
    "ai_task": {"code": 200},
    "homework": {"status": "200"},
    "zhidao_exam": {"status": "200"},
}


class FakeResponse:
    def __init__(self, payload=None, status_code=200, headers=None, text="", raise_json=False):
        self.payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text or (json.dumps(payload) if payload is not None else "")
        self._raise_json = raise_json

    def json(self):
        if self._raise_json:
            raise ValueError("not json")
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def close(self):
        pass


class FakeSession:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def _next(self):
        if not self.responses:
            raise AssertionError("没有剩余的 mock 响应")
        return self.responses.pop(0)

    def get(self, url, params=None, timeout=None, allow_redirects=True):
        self.calls.append(("GET", url, params, allow_redirects))
        return self._next()

    def post(self, url, data=None, json=None, headers=None, timeout=None, stream=False):
        self.calls.append(("POST", url, data, json, headers))
        return self._next()


class StrategyQueryTests(unittest.TestCase):
    def _client(self, response):
        client = ZhsApiClient([])
        client.session = FakeSession([response])
        return client

    def test_all_named_strategies_encrypt_with_their_key_and_validate(self):
        self.assertEqual(len(zhs_api.STRATEGIES), 9)
        for name, strategy in zhs_api.STRATEGIES.items():
            with self.subTest(strategy=name):
                client = self._client(FakeResponse(SUCCESS_PAYLOADS[name]))
                ret = client.query(name, "https://example.com/api", {"courseId": 42})
                self.assertEqual(ret, SUCCESS_PAYLOADS[name])
                call = client.session.calls[0]
                self.assertEqual(call[0], "POST")
                if strategy.content_type == "json":
                    form = json.loads(call[2])
                else:
                    form = call[2]
                payload = json.loads(
                    zhidao_crypto.decrypt(form["secretStr"], key=strategy.key)
                )
                self.assertEqual(payload["courseId"], 42)
                if strategy.set_timestamp:
                    self.assertIn("dateFormate", form)
                    self.assertEqual(payload["dateFormate"], form["dateFormate"])
                else:
                    self.assertNotIn("dateFormate", form)
                    self.assertNotIn("dateFormate", payload)

    def test_legacy_exam_api_query_uses_dynamic_strategy(self):
        api = ExamApi([])
        api.session = FakeSession([FakeResponse({"code": 200, "data": {"ok": True}})])
        ret = api.query(
            "https://example.com/paper",
            {"courseId": 1},
            key=zhidao_crypto.AI_KEY,
            ok_code=200,
            content_type="json",
        )
        self.assertTrue(ret["data"]["ok"])
        form = json.loads(api.session.calls[0][2])
        payload = json.loads(
            zhidao_crypto.decrypt(form["secretStr"], key=zhidao_crypto.AI_KEY)
        )
        self.assertEqual(payload["courseId"], 1)

    def test_extra_fields_are_plaintext_and_not_encrypted(self):
        client = self._client(FakeResponse({"status": "200"}))
        client.query(
            "homework",
            "https://example.com/save",
            {"questionId": 7},
            extra_fields={"source": "1"},
        )
        form = client.session.calls[0][2]
        self.assertEqual(form["source"], "1")
        payload = json.loads(
            zhidao_crypto.decrypt(form["secretStr"], key=zhidao_crypto.EXAM_KEY)
        )
        self.assertEqual(payload["questionId"], 7)
        self.assertNotIn("source", payload)

    def test_get_method_passes_form_as_params(self):
        client = self._client(FakeResponse({"code": 0}))
        client.query("home", "https://example.com/list", {"page": 1}, method="GET")
        method, _url, params, _redirect = client.session.calls[0]
        self.assertEqual(method, "GET")
        payload = json.loads(
            zhidao_crypto.decrypt(params["secretStr"], key=zhidao_crypto.HOME_KEY)
        )
        self.assertEqual(payload["page"], 1)

    def test_minus_12_raises_captcha_error_on_zhidao_homework_and_exam_strategies(self):
        for strategy_name, payload in (
            ("zhidao", {"code": -12, "message": "验证码"}),
            ("homework", {"status": "200", "code": "-12", "message": "需要滑块"}),
            ("zhidao_exam", {"status": "200", "code": "-12", "message": "需要滑块"}),
            ("zhidao_exam", {"status": "500", "code": -12, "message": "需要滑块"}),
        ):
            with self.subTest(strategy=strategy_name, payload=payload):
                client = self._client(FakeResponse(payload))
                with self.assertRaises(CaptchaRequiredError):
                    client.query(strategy_name, "https://example.com", {})

        client = self._client(FakeResponse({"code": -12, "message": "unknown"}))
        with self.assertRaises(ZhsApiError) as ctx:
            client.query("home", "https://example.com", {})
        self.assertNotIsInstance(ctx.exception, CaptchaRequiredError)

    def test_date_formate_uses_real_milliseconds(self):
        from unittest.mock import patch

        client = self._client(FakeResponse({"code": 0}))
        with patch("modules.zhs_api.time.time", return_value=1700000000.456):
            client.query("home", "https://example.com/api", {"x": 1})
        form = client.session.calls[0][2]
        self.assertEqual(form["dateFormate"], 1700000000456)
        payload = json.loads(
            zhidao_crypto.decrypt(form["secretStr"], key=zhidao_crypto.HOME_KEY)
        )
        self.assertEqual(payload["dateFormate"], 1700000000456)

    def test_bad_status_and_unknown_strategy_fail_fast(self):
        client = self._client(FakeResponse({"status": "500"}))
        with self.assertRaises(ZhsApiError):
            client.query("homework", "https://example.com", {})

        client = self._client(FakeResponse({"code": 0}))
        with self.assertRaises(ValueError):
            client.query("not-a-strategy", "https://example.com", {})

    def test_exam_api_error_alias_still_matches(self):
        self.assertIs(ExamApiError, ZhsApiError)


class CasSsoTests(unittest.TestCase):
    def _client(self, responses):
        client = ZhsApiClient([])
        client.session = FakeSession(responses)
        return client

    def test_ensure_exam_sso_follows_ticket_and_caches(self):
        client = self._client(
            [
                FakeResponse(
                    status_code=302,
                    headers={"Location": "/cas/login?ticket=ST-123"},
                ),
                FakeResponse(status_code=200),
            ]
        )
        client.ensure_exam_sso()
        self.assertTrue(client._exam_sso_ready)
        self.assertEqual(len(client.session.calls), 2)
        self.assertEqual(client.session.calls[1][0], "GET")
        self.assertIn("ticket=ST-123", client.session.calls[1][1])

        client.ensure_exam_sso()
        self.assertEqual(len(client.session.calls), 2)

    def test_ensure_exam_sso_raises_on_200_or_missing_ticket(self):
        client = self._client([FakeResponse(status_code=200)])
        with self.assertRaises(SessionExpiredError):
            client.ensure_exam_sso()

        client = self._client(
            [FakeResponse(status_code=302, headers={"Location": "/cas/login"})]
        )
        with self.assertRaises(SessionExpiredError):
            client.ensure_exam_sso()

    def test_concurrent_ensure_exam_sso_logs_in_once(self):
        client = self._client(
            [
                FakeResponse(
                    status_code=302,
                    headers={"Location": "/cas/login?ticket=ST-1"},
                ),
                FakeResponse(status_code=200),
            ]
        )
        errors = []

        def worker():
            try:
                client.ensure_exam_sso()
            except Exception as exc:  # pragma: no cover - 不应发生
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(client.session.calls), 2)
        self.assertTrue(client._exam_sso_ready)

    def test_lock_is_reentrant(self):
        client = ZhsApiClient([])
        with client.lock:
            with client.lock:
                pass

    def test_verify_session_ok(self):
        client = self._client(
            [
                FakeResponse(
                    status_code=302,
                    headers={"Location": "https://passport.zhihuishu.com/cas/x?ticket=ST-1"},
                ),
                FakeResponse(status_code=200),
                FakeResponse({"result": {"uuid": "u-1"}}),
            ]
        )
        self.assertEqual(client.verify_session_status(), SessionStatus.OK)

    def test_verify_session_expired(self):
        client = self._client([FakeResponse(status_code=200)])
        self.assertEqual(client.verify_session_status(), SessionStatus.EXPIRED)

        client = self._client(
            [
                FakeResponse(
                    status_code=302,
                    headers={"Location": "/cas/x?ticket=ST-1"},
                ),
                FakeResponse(status_code=200),
                FakeResponse({"result": {}}),
            ]
        )
        self.assertEqual(client.verify_session_status(), SessionStatus.EXPIRED)

        client = self._client(
            [
                FakeResponse(
                    status_code=302,
                    headers={"Location": "/cas/x?ticket=ST-1"},
                ),
                FakeResponse(status_code=200),
                FakeResponse({}, status_code=401),
            ]
        )
        self.assertEqual(client.verify_session_status(), SessionStatus.EXPIRED)

    def test_verify_session_unavailable_keeps_cookie(self):
        client = self._client(
            [
                FakeResponse(
                    status_code=302,
                    headers={"Location": "/cas/x?ticket=ST-1"},
                ),
                FakeResponse(status_code=200),
                FakeResponse({}, status_code=503),
            ]
        )
        self.assertEqual(client.verify_session_status(), SessionStatus.UNAVAILABLE)

        client = self._client([FakeResponse({}, status_code=502)])
        self.assertEqual(client.verify_session_status(), SessionStatus.UNAVAILABLE)

    def test_cas_login_supports_3xx_and_checks_ticket_target_status(self):
        # 303 重定向正常换票
        client = self._client(
            [
                FakeResponse(
                    status_code=303,
                    headers={"Location": "/cas/login?ticket=ST-303"},
                ),
                FakeResponse(status_code=200),
            ]
        )
        self.assertTrue(client.ensure_service_sso("https://example.com/svc"))

        # 票据重定向目标返回 403 -> 视为失败 / EXPIRED
        client = self._client(
            [
                FakeResponse(
                    status_code=302,
                    headers={"Location": "/cas/login?ticket=ST-403"},
                ),
                FakeResponse(status_code=403),
            ]
        )
        self.assertFalse(client.ensure_service_sso("https://example.com/svc"))

        client = self._client(
            [
                FakeResponse(
                    status_code=302,
                    headers={"Location": "/cas/login?ticket=ST-403"},
                ),
                FakeResponse(status_code=403),
            ]
        )
        self.assertEqual(client.verify_session_status(), SessionStatus.EXPIRED)

        # 票据重定向目标返回 502 -> UNAVAILABLE（不误判掉线）
        client = self._client(
            [
                FakeResponse(
                    status_code=302,
                    headers={"Location": "/cas/login?ticket=ST-502"},
                ),
                FakeResponse(status_code=502),
            ]
        )
        self.assertEqual(client.verify_session_status(), SessionStatus.UNAVAILABLE)

    def test_build_session_preserves_multi_domain_cookies(self):
        cookies = [
            {
                "name": "SESSION",
                "value": "online-sess",
                "domain": "onlineservice-api.zhihuishu.com",
                "path": "/",
            },
            {
                "name": "SESSION",
                "value": "hike-sess",
                "domain": "hikeservice.zhihuishu.com",
                "path": "/",
            },
            {
                "name": "CASLOGC",
                "value": "%7B%22uuid%22%3A%22u-xyz%22%2C%22realName%22%3A%22%E5%BC%A0%E4%B8%89%22%7D",
                "domain": ".zhihuishu.com",
                "path": "/",
            },
        ]
        session = zhs_api.build_session(cookies)
        self.assertEqual(
            session.cookies.get("SESSION", domain="onlineservice-api.zhihuishu.com"),
            "online-sess",
        )
        self.assertEqual(
            session.cookies.get("SESSION", domain="hikeservice.zhihuishu.com"),
            "hike-sess",
        )
        self.assertEqual(
            session.cookies.get("exitRecod_u-xyz", domain=".zhihuishu.com"),
            "2",
        )
        info = zhs_api.extract_caslogc_info(session.cookies)
        self.assertEqual(info.get("uuid"), "u-xyz")
        self.assertEqual(info.get("realName"), "张三")

    def test_build_session_and_export_preserve_cookie_security_and_expiry(self):
        from modules.qr_login import requests_cookies_to_playwright

        future_exp = 1893456000
        cookies = [
            {
                "name": "CASTGC",
                "value": "TGT-secure-1",
                "domain": ".zhihuishu.com",
                "path": "/",
                "expires": future_exp,
                "secure": True,
                "httpOnly": True,
            },
            {
                "name": "CASLOGC",
                "value": "%7B%22uuid%22%3A%22u-keep%22%7D",
                "domain": ".zhihuishu.com",
                "path": "/",
                "expires": future_exp + 100,
                "secure": False,
                "httpOnly": False,
            },
        ]
        session = zhs_api.build_session(cookies)
        exported = {c["name"]: c for c in requests_cookies_to_playwright(session.cookies)}
        self.assertIn("CASTGC", exported)
        self.assertEqual(exported["CASTGC"]["expires"], float(future_exp))
        self.assertTrue(exported["CASTGC"]["secure"])
        self.assertTrue(exported["CASTGC"]["httpOnly"])

        self.assertIn("CASLOGC", exported)
        self.assertEqual(exported["CASLOGC"]["expires"], float(future_exp + 100))
        self.assertFalse(exported["CASLOGC"]["secure"])
        self.assertFalse(exported["CASLOGC"]["httpOnly"])

    def test_context_manager_clone_session_and_no_retry_strategy(self):
        cookies = [
            {
                "name": "CASTGC",
                "value": "TGT-clone-1",
                "domain": ".zhihuishu.com",
                "path": "/",
            }
        ]
        with ZhsApiClient(cookies) as client:
            cloned = client.clone_session()
            try:
                self.assertIsNot(cloned, client.session)
                self.assertEqual(cloned.cookies.get("CASTGC"), "TGT-clone-1")
                self.assertEqual(
                    cloned.headers.get("User-Agent"),
                    client.session.headers.get("User-Agent"),
                )
            finally:
                cloned.close()

            # ai_exam_submit 禁用 urllib3 POST 自动重试
            self.assertFalse(zhs_api.get_strategy("ai_exam_submit").allow_retry)
            no_retry = client._get_no_retry_session()
            self.assertIsNot(no_retry, client.session)
            adapter = no_retry.get_adapter("https://studentexamtest.zhihuishu.com")
            self.assertEqual(adapter.max_retries.total, 0)

    def test_execute_does_not_hold_lock_during_network_io(self):
        client = ZhsApiClient([])
        in_post = threading.Event()
        release_post = threading.Event()
        lock_acquired_concurrently = []

        class _BlockingSession:
            def post(self, url, **kwargs):
                in_post.set()
                release_post.wait(timeout=3.0)
                return FakeResponse({"code": 0})

        client.session = _BlockingSession()

        t = threading.Thread(
            target=lambda: client.query("home", "https://example.com/slow", {"x": 1})
        )
        t.start()
        self.assertTrue(in_post.wait(timeout=2.0))
        acquired = client.lock.acquire(blocking=False)
        if acquired:
            lock_acquired_concurrently.append(True)
            client.lock.release()
        release_post.set()
        t.join(timeout=3.0)
        self.assertEqual(lock_acquired_concurrently, [True])


if __name__ == "__main__":
    unittest.main()