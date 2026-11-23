from base64 import b64encode
import io
import os
import tempfile
import unittest

from PIL import Image
from requests.cookies import RequestsCookieJar, create_cookie

from modules.qr_login import (
    QrLoginManager,
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
        jar.set_cookie(create_cookie("CASLOGC", "abc", domain=".zhihuishu.com"))
        jar.set_cookie(create_cookie("OTHER", "xyz", domain=".example.com"))
        converted = requests_cookies_to_playwright(jar)
        self.assertEqual(len(converted), 1)
        self.assertEqual(converted[0]["name"], "CASLOGC")

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

    def test_qr_login_cancelled_raises(self):
        png_b64 = b64encode(_sample_qr_png()).decode("ascii")
        session = _FakeQrSession(
            [
                {"qrToken": "t1", "img": png_b64},
                {"status": 3, "msg": "canceled"},
            ]
        )
        with self.assertRaises(RuntimeError):
            qr_login_for_cookies(
                qr_callback=lambda _: None,
                session=session,
                sleep_fn=lambda _: None,
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
            _FakeQrSession([{"qrToken": "t-exp", "img": png_b64}, {"status": 2}]),
            _FakeQrSession([{"qrToken": "t-can", "img": png_b64}]),
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
