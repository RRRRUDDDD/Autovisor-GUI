from base64 import b64encode
import io
import os
import tempfile
import unittest

from PIL import Image
import requests
from requests.cookies import RequestsCookieJar, create_cookie

from modules import zhs_api
from modules.qr_login import (
    QR_IMG_URL,
    QR_INFO_URL,
    QR_LOGIN_SERVICE_URL,
    QrLoginManager,
    _DEFAULT_HEADERS,
    qr_login_for_cookies,
    render_qr_tty,
    render_qr_unicode,
    requests_cookies_to_playwright,
    save_qr_image,
)


def _sample_qr_png() -> bytes:
    img = Image.new("L", (16, 16), color=255)
    for x in range(4, 12):
        for y in range(4, 12):
            img.putpixel((x, y), 0)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class _FakeResp:
    def __init__(self, payload=None):
        self._payload = payload or {}

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeQrSession:
    def __init__(self, steps):
        self.steps = list(steps)
        self.cookies = RequestsCookieJar()
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        action = self.steps.pop(0)
        if callable(action):
            return action(self, url, kwargs)
        return _FakeResp(action)

    def close(self):
        pass


class QrLoginTests(unittest.TestCase):
    def test_render_qr_unicode_and_tty(self):
        png = _sample_qr_png()
        uni = render_qr_unicode(png, size=10)
        self.assertTrue(any(ch in uni for ch in ("▀", "▄", "█")))
        tty = render_qr_tty(png, size=10)
        self.assertIn("\033[0;37;40m", tty)
        self.assertIn("\033[0;37;47m", tty)

    def test_save_qr_image(self):
        png = _sample_qr_png()
        with tempfile.TemporaryDirectory() as tmp:
            saved = save_qr_image(png, tmp)
            self.assertTrue(os.path.isfile(saved))
            with open(saved, "rb") as f:
                self.assertEqual(f.read(), png)

    def test_requests_cookies_to_playwright_filters_domain(self):
        jar = RequestsCookieJar()
        jar.set_cookie(create_cookie("CASLOGC", "abc", domain=".zhihuishu.com", rest={}))
        jar.set_cookie(
            create_cookie(
                "CASTGC",
                "tgt-1",
                domain=".zhihuishu.com",
                secure=True,
                expires=1893456000,
                rest={"HttpOnly": None},
            )
        )
        jar.set_cookie(create_cookie("OTHER", "xyz", domain=".example.com"))
        converted = {c["name"]: c for c in requests_cookies_to_playwright(jar)}
        self.assertEqual(set(converted.keys()), {"CASLOGC", "CASTGC"})
        self.assertFalse(converted["CASLOGC"]["httpOnly"])
        self.assertTrue(converted["CASTGC"]["httpOnly"])
        self.assertTrue(converted["CASTGC"]["secure"])
        self.assertEqual(converted["CASTGC"]["expires"], 1893456000.0)

    def test_qr_urls_and_unified_user_agent(self):
        self.assertEqual(
            QR_IMG_URL,
            "https://user.zhihuishu.com/zhsuser/qrCodeLogin/getLoginQrImg",
        )
        self.assertEqual(
            QR_INFO_URL,
            "https://user.zhihuishu.com/zhsuser/qrCodeLogin/getLoginQrInfo",
        )
        self.assertIn(
            "service=https://onlineservice-api.zhihuishu.com/login/gologin",
            QR_LOGIN_SERVICE_URL,
        )
        self.assertEqual(
            _DEFAULT_HEADERS["User-Agent"],
            zhs_api.DEFAULT_HEADERS["User-Agent"],
        )
        self.assertNotIn("Chrome/120", _DEFAULT_HEADERS["User-Agent"])
        self.assertNotIn("Chrome/131", _DEFAULT_HEADERS["User-Agent"])

    def test_qr_login_full_flow_with_refresh(self):
        png_b64 = b64encode(_sample_qr_png()).decode("ascii")
        received_imgs = []

        def finish_login(sess, _url, _kwargs):
            sess.cookies.set_cookie(
                create_cookie("CASLOGC", '{"uuid":"u1"}', domain=".zhihuishu.com")
            )
            return _FakeResp({})

        session = _FakeQrSession(
            [
                {},  # pre-warm passport/login
                {"qrToken": "t1", "img": png_b64},
                {"status": 2, "msg": "expired"},
                {"qrToken": "t2", "img": png_b64},
                {"status": -1, "msg": "waiting"},
                {"status": 0, "msg": "scanned"},
                {"status": 1, "msg": "ok", "oncePassword": "otp-1"},
                finish_login,
            ]
        )

        cookies = qr_login_for_cookies(
            qr_callback=lambda b: received_imgs.append(b),
            session=session,
            sleep_fn=lambda _: None,
        )
        self.assertEqual(len(received_imgs), 2)
        self.assertEqual(len(cookies), 1)
        self.assertEqual(cookies[0]["name"], "CASLOGC")
        self.assertEqual(session.calls[0][0], QR_LOGIN_SERVICE_URL)
        self.assertEqual(session.calls[1][0], QR_IMG_URL)
        self.assertEqual(session.calls[2][0], QR_INFO_URL)
        self.assertEqual(session.calls[-1][0], QR_LOGIN_SERVICE_URL)
        self.assertEqual(session.calls[-1][1]["params"], {"pwd": "otp-1"})

    def test_qr_login_cancelled_raises(self):
        png_b64 = b64encode(_sample_qr_png()).decode("ascii")
        session = _FakeQrSession(
            [
                {},  # pre-warm
                {"qrToken": "t1", "img": png_b64},
                {"status": 3, "msg": "canceled"},
            ]
        )
        with self.assertRaisesRegex(RuntimeError, "取消"):
            qr_login_for_cookies(
                qr_callback=lambda _: None,
                session=session,
                sleep_fn=lambda _: None,
            )

    def test_qr_login_retries_transient_poll_errors_and_succeeds(self):
        png_b64 = b64encode(_sample_qr_png()).decode("ascii")

        def transient_err(_sess, _url, _kwargs):
            raise requests.ConnectionError("temporary reset")

        def finish_login(sess, _url, _kwargs):
            sess.cookies.set_cookie(
                create_cookie("CASLOGC", '{"uuid":"u-retry"}', domain=".zhihuishu.com")
            )
            return _FakeResp({})

        session = _FakeQrSession(
            [
                {},  # pre-warm
                {"qrToken": "t-retry", "img": png_b64},
                transient_err,
                transient_err,
                {"status": 1, "oncePassword": "otp-retry"},
                finish_login,
            ]
        )
        cookies = qr_login_for_cookies(
            qr_callback=lambda _: None,
            session=session,
            sleep_fn=lambda _: None,
        )
        self.assertEqual(len(cookies), 1)
        self.assertEqual(cookies[0]["name"], "CASLOGC")

    def test_qr_login_poll_respects_total_timeout(self):
        png_b64 = b64encode(_sample_qr_png()).decode("ascii")
        clock = {"t": 0.0}

        def fake_monotonic():
            return clock["t"]

        def fake_sleep(sec):
            clock["t"] += sec

        def always_waiting(_sess, _url, _kwargs):
            return _FakeResp({"status": -1})

        session = _FakeQrSession(
            [
                {},  # pre-warm
                {"qrToken": "t-timeout", "img": png_b64},
                always_waiting,
                always_waiting,
                always_waiting,
            ]
        )
        with self.assertRaisesRegex(RuntimeError, "超时"):
            qr_login_for_cookies(
                qr_callback=lambda _: None,
                session=session,
                poll_interval=1.0,
                timeout_secs=2.0,
                sleep_fn=fake_sleep,
                monotonic_fn=fake_monotonic,
            )

    def test_qr_login_manager_lifecycle_and_save_image(self):
        import json

        png = _sample_qr_png()
        png_b64 = b64encode(png).decode("ascii")

        def finish_login(sess, _url, _kwargs):
            sess.cookies.set_cookie(
                create_cookie("CASLOGC", '{"uuid":"u2"}', domain=".zhihuishu.com")
            )
            return _FakeResp({})

        fake_sess = _FakeQrSession(
            [
                {},  # pre-warm
                {"qrToken": "tok-mgr", "img": png_b64},
                {"status": -1},
                {"status": 0},
                {"status": 1, "oncePassword": "otp-mgr"},
                finish_login,
            ]
        )
        mgr = QrLoginManager(session_factory=lambda _proxy: fake_sess)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                started = mgr.start(proxy="")
                sid = started["sessionId"]
                self.assertEqual(started["imageBase64"], png_b64)
                self.assertEqual(started["savedPath"], "")
                self.assertEqual(fake_sess.calls[0][0], QR_LOGIN_SERVICE_URL)
                self.assertEqual(fake_sess.calls[1][0], QR_IMG_URL)

                saved_file = mgr.save_current_image(sid, tmp)
                self.assertTrue(os.path.isfile(saved_file))
                with open(saved_file, "rb") as f:
                    self.assertEqual(f.read(), png)

                cookie_file = os.path.join(tmp, "data", "cookies.json")
                self.assertEqual(mgr.poll(sid, cookie_path=cookie_file), {"status": "waiting"})
                self.assertEqual(mgr.poll(sid, cookie_path=cookie_file), {"status": "scanned"})
                confirmed = mgr.poll(sid, cookie_path=cookie_file)
                self.assertEqual(confirmed, {"status": "confirmed", "cookieCount": 1})
                self.assertTrue(os.path.isfile(cookie_file))
                with open(cookie_file, "r", encoding="utf-8") as f:
                    saved_cookies = json.load(f)
                self.assertEqual(len(saved_cookies), 1)
                self.assertEqual(saved_cookies[0]["name"], "CASLOGC")

                # Session cleaned up after confirmation
                with self.assertRaises(KeyError):
                    mgr.poll(sid, cookie_path=cookie_file)
        finally:
            mgr.close_all()

    def test_qr_login_manager_expired_and_cancel(self):
        png_b64 = b64encode(_sample_qr_png()).decode("ascii")
        sessions = [
            _FakeQrSession([{}, {"qrToken": "t-exp", "img": png_b64}, {"status": 2}]),
            _FakeQrSession([{}, {"qrToken": "t-can", "img": png_b64}]),
        ]
        mgr = QrLoginManager(session_factory=lambda _proxy: sessions.pop(0))
        try:
            s1 = mgr.start()["sessionId"]
            self.assertEqual(mgr.poll(s1), {"status": "expired"})
            with self.assertRaises(KeyError):
                mgr.poll(s1)

            s2 = mgr.start()["sessionId"]
            self.assertTrue(mgr.cancel(s2))
            self.assertFalse(mgr.cancel(s2))
        finally:
            mgr.close_all()


if __name__ == "__main__":
    unittest.main()
