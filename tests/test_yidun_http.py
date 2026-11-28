# encoding=utf-8
from __future__ import annotations

from io import BytesIO
import json
import os
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import quote

import cv2
import numpy as np
from PIL import Image

from modules import redaction
from modules.browser import BrowserSession, create_browser_session
from modules.logger import Logger
from modules.yidun_fp import (
    YidunFpCollector,
    YidunFpStore,
    clear_session_fp,
    extract_fp_from_url,
    get_session_fp,
    is_valid_fp,
    normalize_fp,
    probe_fp_from_page,
    validate_fp_format,
)
from modules.yidun_http import (
    CAPTCHA_CLICK,
    CAPTCHA_SLIDE,
    CLICK_TYPE,
    SLIDE_TYPE,
    CaptchaTrigger,
    YidunHttpSolver,
    aes,
    aes_click,
    base64_decode,
    base64_decode_private,
    base64_encode,
    base64_encode_private,
    build_click_data,
    build_slide_data,
    build_slide_track,
    compose_validate,
    find_slide_gap_x,
    gen_crc32,
    generate_cb,
    parse_jsonp,
    to_click_css,
    with_crc,
    xor_encode,
)


def _sample_fp(seed: str = "A") -> str:
    body = (seed + "x9K+/=_-7qLmN2pR") * 11
    return f"{body[:176]}:1764200000123"


def _make_slide_images(gap_x: int = 128) -> tuple[bytes, bytes]:
    bg = np.full((160, 320, 3), 235, dtype=np.uint8)
    cv2.rectangle(bg, (gap_x, 45), (gap_x + 44, 95), (30, 30, 30), 2)
    cv2.line(bg, (gap_x + 10, 45), (gap_x + 34, 95), (45, 45, 45), 2)
    ok_bg, buf_bg = cv2.imencode(".png", bg)
    assert ok_bg

    front_rgb = np.zeros((160, 56, 3), dtype=np.uint8)
    front_rgb[45:96, 6:51] = 235
    cv2.rectangle(front_rgb, (6, 45), (50, 95), (30, 30, 30), 2)
    cv2.line(front_rgb, (16, 45), (40, 95), (45, 45, 45), 2)
    front = np.zeros((160, 56, 4), dtype=np.uint8)
    front[:, :, :3] = front_rgb
    front[45:96, 6:51, 3] = 255
    ok_fg, buf_fg = cv2.imencode(".png", front)
    assert ok_fg
    return buf_bg.tobytes(), buf_fg.tobytes()


def _make_click_image(width: int = 480, height: int = 240) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (width, height), color=(240, 240, 240)).save(buf, format="PNG")
    return buf.getvalue()


class _FakeHttpResponse:
    def __init__(self, *, text: str = "", content: bytes = b"", status_code: int = 200):
        self.text = text
        self.content = content
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _ScriptedYidunClient:
    def __init__(self, routes):
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, url: str, params: dict | None = None):
        self.calls.append((url, dict(params or {})))
        handler = self.routes[url]
        return handler(params or {}) if callable(handler) else handler


class YidunCodecTests(unittest.TestCase):
    def test_base64_roundtrip_and_crc(self):
        sample = [1, 2, 3, -12, 127, -128, 0, 42]
        self.assertEqual(base64_decode(base64_encode(sample)), sample)
        self.assertEqual(base64_decode_private(base64_encode_private(sample)), sample)
        crc = gen_crc32([104, 101, 108, 108, 111])
        self.assertEqual(len(crc), 8)
        self.assertTrue(with_crc("abc").startswith("abc"))
        self.assertEqual(len(with_crc("abc")), 11)

    def test_aes_xor_cb_compose_and_jsonp(self):
        cipher_slide = aes("hello")
        cipher_click = aes_click("hello")
        self.assertTrue(cipher_slide)
        self.assertTrue(cipher_click)
        self.assertTrue(xor_encode("tok123", "10,20,0"))
        self.assertTrue(generate_cb())

        fp = _sample_fp("V")
        composed = compose_validate("raw-val-123", fp)
        self.assertTrue(composed.startswith("CN31_"))
        self.assertTrue(composed.endswith("_v_i_1"))
        self.assertNotIn(fp, composed)
        self.assertNotIn("raw-val-123", composed)

        parsed = parse_jsonp('__JSONP_abc1234_1({"error":0,"msg":"ok","data":{"dt":"d1"}});')
        self.assertEqual(parsed["error"], 0)
        self.assertEqual(parsed["data"]["dt"], "d1")
        with self.assertRaises(ValueError):
            parse_jsonp("not-jsonp")

    def test_slide_and_click_payload_builders(self):
        trace = build_slide_track(120.0)
        self.assertGreater(len(trace), 5)
        self.assertEqual(trace[-1][0], 120.0)
        slide_data = build_slide_data("tok1", trace, 120.0, 400)
        self.assertEqual(set(slide_data), {"d", "m", "p", "f", "ext"})
        self.assertEqual(slide_data["m"], "")
        self.assertTrue(all(slide_data[k] for k in ("d", "p", "f", "ext")))

        self.assertEqual(to_click_css(240.0, 120.0, 480, 320), (160, 80))
        click_data = build_click_data("tok2", 160, 80)
        self.assertEqual(set(click_data), {"d", "m", "p", "ext"})
        self.assertEqual(click_data["d"], "")
        self.assertTrue(all(click_data[k] for k in ("m", "p", "ext")))


class YidunFpAndRedactionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        clear_session_fp()
        with redaction._secrets_lock:
            self._saved_secrets = dict(redaction._secrets)

    def tearDown(self):
        clear_session_fp()
        with redaction._secrets_lock:
            redaction._secrets.clear()
            redaction._secrets.update(self._saved_secrets)

    def test_fp_validation_and_url_extraction(self):
        fp = _sample_fp("F")
        self.assertEqual(validate_fp_format(fp), [])
        self.assertTrue(is_valid_fp(f' "{fp}\n" '))
        self.assertEqual(normalize_fp(f"'{fp}'"), fp)
        self.assertTrue(validate_fp_format("short:1764200000123"))
        self.assertTrue(validate_fp_format("a" * 180))
        self.assertTrue(validate_fp_format(f"{'a' * 170}%2B:1764200000123"))

        encoded_fp = quote(fp, safe="")
        url = f"https://c.dun.163.com/api/v3/get?id={CAPTCHA_CLICK}&fp={encoded_fp}&width=320"
        extracted, raw = extract_fp_from_url(url)
        self.assertEqual(extracted, fp)
        self.assertEqual(raw, encoded_fp)
        self.assertEqual(
            extract_fp_from_url(f"https://example.com/api/v3/get?fp={encoded_fp}"),
            (None, None),
        )

    def test_collector_captures_fp_in_memory_and_redacts_logs_and_notifications(self):
        fp = _sample_fp("S")
        encoded_fp = quote(fp, safe="")
        store = YidunFpStore()

        class _FakeContext:
            def __init__(self):
                self.handlers = {}

            def on(self, event, cb):
                self.handlers.setdefault(event, []).append(cb)

            def remove_listener(self, event, cb):
                self.handlers.get(event, []).remove(cb)

            def emit(self, event, payload):
                for cb in list(self.handlers.get(event, [])):
                    cb(payload)

        class _FakeRequest:
            def __init__(self, url):
                self.url = url

        ctx = _FakeContext()
        collector = YidunFpCollector(context=ctx, store=store).attach()
        self.assertTrue(collector.attached)

        ctx.emit(
            "request",
            _FakeRequest(
                f"https://c.dun.163.com/api/v3/get?id={CAPTCHA_CLICK}&fp={encoded_fp}&width=320"
            ),
        )
        self.assertEqual(store.get(context=ctx), fp)

        # 验证 redaction.redact_sensitive 与 redact_notification 均彻底脱敏
        self.assertNotIn(fp, redaction.redact_sensitive(f"leak bare {fp} end"))
        self.assertNotIn(
            encoded_fp,
            redaction.redact_sensitive(
                f"https://c.dun.163.com/api/v3/get?fp={encoded_fp}&id=1"
            ),
        )
        self.assertNotIn(fp, redaction.redact_sensitive(f"fp={fp} ok"))
        self.assertNotIn(fp, redaction.redact_sensitive(json.dumps({"fp": fp, "irToken": "ir_secret_123"})))
        self.assertNotIn("ir_secret_123", redaction.redact_sensitive(json.dumps({"irToken": "ir_secret_123"})))
        self.assertNotIn(fp, redaction.redact_notification(f"通知正文含 {fp}"))

        # 验证真实 Logger 写入日志文件时严防泄露明文与 URL 编码形态
        logger = Logger()
        orig_filename = logger.filename
        handle = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
        handle.close()
        try:
            logger.filename = handle.name
            logger.warn(f"调试异常 url=https://c.dun.163.com/api/v3/get?fp={encoded_fp}")
            logger.info(f"上下文包含 {fp}")
            logger.event("风控测试", 指纹=fp)
            logger.flush()
            with open(handle.name, encoding="utf-8") as fh:
                content = fh.read()
            self.assertNotIn(fp, content)
            self.assertNotIn(encoded_fp, content)
            self.assertIn("***REDACTED***", content)
        finally:
            logger.filename = orig_filename
            os.unlink(handle.name)

        collector.detach(clear_store=True)
        self.assertFalse(collector.attached)
        self.assertIsNone(store.get(context=ctx))

    async def test_probe_fp_from_page_extracts_and_registers_secret(self):
        from modules.yidun_fp import _PROBE_STORAGE_JS

        self.assertIn("window.gdxidpyhxde", _PROBE_STORAGE_JS)
        self.assertIn("gdxidpyhxdE", _PROBE_STORAGE_JS)

        fp = _sample_fp("P")
        encoded_fp = quote(fp, safe="")
        store = YidunFpStore()

        class _Page:
            context = object()

            async def evaluate(self, _script):
                return ["invalid", encoded_fp]

        found = await probe_fp_from_page(_Page(), store=store)
        self.assertEqual(found, fp)
        self.assertEqual(store.get(context=_Page.context), fp)
        self.assertNotIn(fp, redaction.redact_sensitive(f"raw {fp}"))
        self.assertNotIn(encoded_fp, redaction.redact_sensitive(f"cookie gdxidpyhxdE={encoded_fp}"))

    def test_fp_store_isolates_contexts_and_clears_session(self):
        fp_a = _sample_fp("A")
        fp_b = _sample_fp("B")
        ctx_a = object()
        ctx_b = object()
        store = YidunFpStore()

        self.assertTrue(store.set(fp_a, context=ctx_a))
        self.assertEqual(store.get(context=ctx_a), fp_a)
        # 明确传入 ctx_b 时默认不得跨上下文串读 ctx_a 的 _latest
        self.assertIsNone(store.get(context=ctx_b))
        self.assertEqual(store.get(context=ctx_b, fallback_latest=True), fp_a)
        self.assertEqual(store.get(), fp_a)

        self.assertTrue(store.set(fp_b, context=ctx_b))
        self.assertEqual(store.get(context=ctx_a), fp_a)
        self.assertEqual(store.get(context=ctx_b), fp_b)

        store.clear(context=ctx_a)
        self.assertIsNone(store.get(context=ctx_a))
        self.assertEqual(store.get(context=ctx_b), fp_b)

        store.clear()
        self.assertIsNone(store.get(context=ctx_b))
        self.assertIsNone(store.get())

    async def test_browser_session_attaches_and_cleans_up_fp_collector(self):
        fp = _sample_fp("B")
        encoded_fp = quote(fp, safe="")

        class _Page:
            def __init__(self):
                self.closed = False

            async def close(self):
                self.closed = True

            def is_closed(self):
                return self.closed

            async def add_init_script(self, **_kwargs):
                pass

            def set_default_timeout(self, _timeout):
                pass

        class _Context:
            def __init__(self):
                self.page = _Page()
                self.listeners = {}

            def on(self, event, cb):
                self.listeners.setdefault(event, []).append(cb)

            def remove_listener(self, event, cb):
                self.listeners.get(event, []).remove(cb)

            async def new_page(self):
                return self.page

        class _Browser:
            def __init__(self):
                self.contexts = [_Context()]

            async def close(self):
                pass

        class _Chromium:
            def __init__(self):
                self.browser = _Browser()

            async def connect_over_cdp(self, _url):
                return self.browser

        class _Playwright:
            def __init__(self):
                self.chromium = _Chromium()

        class _Cfg:
            attach_existing_chrome = True
            cdp_url = "http://127.0.0.1:9222"
            driver = "chrome"
            exe_path = ""

        class _NullLogger:
            def info(self, *_a, **_kw):
                pass

            def debug(self, *_a, **_kw):
                pass

        session = await create_browser_session(_Playwright(), _Cfg(), None, _NullLogger())
        self.assertIsInstance(session, BrowserSession)
        self.assertIsNotNone(session.fp_collector)
        self.assertTrue(session.fp_collector.attached)

        class _Req:
            url = f"https://c.dun.163.com/api/v3/get?id={CAPTCHA_SLIDE}&fp={encoded_fp}"

        for cb in list(session.context.listeners.get("request", [])):
            cb(_Req())
        self.assertEqual(get_session_fp(session.context), fp)

        await session.close()
        self.assertIsNone(get_session_fp(session.context))
        self.assertEqual(session.context.listeners.get("request", []), [])


class YidunHttpSolverTests(unittest.TestCase):
    def setUp(self):
        clear_session_fp()
        with redaction._secrets_lock:
            self._saved_secrets = dict(redaction._secrets)

    def tearDown(self):
        clear_session_fp()
        with redaction._secrets_lock:
            redaction._secrets.clear()
            redaction._secrets.update(self._saved_secrets)

    def test_slide_solver_reuses_phase2_opencv_matcher(self):
        fp = _sample_fp("L")
        bg_bytes, front_bytes = _make_slide_images(gap_x=128)
        bg_url = "https://necaptcha.nosdn.127.net/bg.png"
        front_url = "https://necaptcha.nosdn.127.net/front.png"

        client = _ScriptedYidunClient(
            {
                "https://c.dun.163.com/api/v2/getconf": _FakeHttpResponse(
                    text='__JSONP_a_0({"error":0,"msg":"ok","data":{"dt":"dt1","irToken":"ir1"}});'
                ),
                "https://c.dun.163.com/api/v3/get": _FakeHttpResponse(
                    text='__JSONP_a_1({"error":0,"msg":"ok","data":{"token":"tok_slide","type":2,"bg":["'
                    + bg_url
                    + '"],"front":["'
                    + front_url
                    + '"]}});'
                ),
                bg_url: _FakeHttpResponse(content=bg_bytes),
                front_url: _FakeHttpResponse(content=front_bytes),
                "https://c.dun.163.com/api/v3/check": _FakeHttpResponse(
                    text='__JSONP_a_2({"error":0,"msg":"ok","data":{"result":true,"validate":"raw_slide_val"}});'
                ),
            }
        )

        # 验证确实复用了 Phase 2 的 slider._match_yidun_gap
        from modules import slider as slider_mod

        with patch.object(
            slider_mod, "_match_yidun_gap", wraps=slider_mod._match_yidun_gap
        ) as spy_match:
            gap_res = find_slide_gap_x(bg_bytes, front_bytes)
            self.assertIsNotNone(gap_res)
            self.assertAlmostEqual(gap_res[0], 122.0, delta=12.0)
            self.assertEqual(gap_res[1], 320)

            solver = YidunHttpSolver(
                fp_provider=lambda: fp,
                client_factory=lambda: client,
            )
            solution = solver.solve(CaptchaTrigger(kind="slide"))

        self.assertGreaterEqual(spy_match.call_count, 2)
        self.assertIsNotNone(solution)
        self.assertEqual(solution.raw_validate, "raw_slide_val")
        self.assertTrue(solution.validate.startswith("CN31_"))
        self.assertTrue(solution.validate.endswith("_v_i_1"))
        check_call = [c for c in client.calls if c[0].endswith("/api/v3/check")][0]
        self.assertEqual(check_call[1]["type"], str(SLIDE_TYPE))
        sent_data = json.loads(check_call[1]["data"])
        self.assertEqual(sent_data["m"], "")
        self.assertTrue(sent_data["d"])

    def test_click_solver_reuses_phase2_vlm_parser(self):
        fp = _sample_fp("C")
        bg_bytes = _make_click_image(480, 240)
        bg_url = "https://necaptcha.nosdn.127.net/click_bg.png"

        client = _ScriptedYidunClient(
            {
                "https://c.dun.163.com/api/v2/getconf": _FakeHttpResponse(
                    text='__JSONP_b_0({"error":0,"msg":"ok","data":{"dt":"dt2"}});'
                ),
                "https://c.dun.163.com/api/v3/get": _FakeHttpResponse(
                    text='__JSONP_b_1({"error":0,"msg":"ok","data":{"token":"tok_click","type":11,"bg":["'
                    + bg_url
                    + '"],"front":"请点击红色字母A"}});'
                ),
                bg_url: _FakeHttpResponse(content=bg_bytes),
                "https://c.dun.163.com/api/v3/check": _FakeHttpResponse(
                    text='__JSONP_b_2({"error":0,"msg":"ok","data":{"result":true,"validate":"raw_click_val"}});'
                ),
            }
        )

        with (
            patch("modules.yidun_click.is_vision_available", return_value=True),
            patch(
                "modules.yidun_click.request_click_point",
                return_value=(240.0, 120.0, 480, 240),
            ) as mock_vlm,
        ):
            solver = YidunHttpSolver(
                fp_provider=lambda: fp,
                client_factory=lambda: client,
            )
            solution = solver.solve(CaptchaTrigger(kind="click"))

        mock_vlm.assert_called_once()
        self.assertIsNotNone(solution)
        self.assertEqual(solution.raw_validate, "raw_click_val")
        self.assertTrue(solution.validate.startswith("CN31_"))
        check_call = [c for c in client.calls if c[0].endswith("/api/v3/check")][0]
        self.assertEqual(check_call[1]["type"], str(CLICK_TYPE))
        sent_data = json.loads(check_call[1]["data"])
        self.assertEqual(sent_data["d"], "")
        self.assertTrue(all(sent_data[k] for k in ("m", "p", "ext")))

    def test_solver_returns_none_without_fp_or_on_protocol_error(self):
        solver = YidunHttpSolver(fp_provider=lambda: None)
        self.assertIsNone(solver.solve(CaptchaTrigger(kind="click")))

        fp = _sample_fp("E")
        broken_client = _ScriptedYidunClient(
            {
                "https://c.dun.163.com/api/v2/getconf": _FakeHttpResponse(
                    text="unexpected html protocol change"
                ),
            }
        )
        with patch("modules.yidun_click.is_vision_available", return_value=True):
            solver_broken = YidunHttpSolver(
                fp_provider=lambda: fp,
                client_factory=lambda: broken_client,
                max_retry=1,
            )
            self.assertIsNone(solver_broken.solve(CaptchaTrigger(kind="click")))

    def test_find_slide_gap_x_returns_none_for_transparent_or_oversized_front(self):
        bg_bytes, _ = _make_slide_images(128)
        # 全透明前景图应返回 None 而不是 0.0
        transparent_front = np.zeros((160, 56, 4), dtype=np.uint8)
        ok_t, buf_t = cv2.imencode(".png", transparent_front)
        self.assertTrue(ok_t)
        self.assertIsNone(find_slide_gap_x(bg_bytes, buf_t.tobytes()))

        # front 尺寸大于 background 应返回 None 而不是抛 ValueError
        oversized_front = np.full((200, 400, 4), 255, dtype=np.uint8)
        ok_o, buf_o = cv2.imencode(".png", oversized_front)
        self.assertTrue(ok_o)
        self.assertIsNone(find_slide_gap_x(bg_bytes, buf_o.tobytes()))
