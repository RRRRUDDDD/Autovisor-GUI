import random
import unittest

from modules.slider import (
    FAILURE_WORDS,
    GAP_BIAS_PX,
    gen_track,
    image_to_display_point,
    is_failure_tip,
    unsupported_mode_label,
)


class ImageToDisplayPointTests(unittest.TestCase):
    def test_scales_match_template_result_to_css_pixels(self):
        # 480x240 原图显示为 400x200 时, 匹配点 (120, 60) 应换算为 (100, 50)。
        self.assertEqual(image_to_display_point((120, 60), 480, 240, 400, 200), (100.0, 50.0))


class GenTrackTests(unittest.TestCase):
    def assert_valid_track(self, distance, track):
        self.assertGreaterEqual(len(track), 10)
        self.assertAlmostEqual(sum(step[0] for step in track), distance, places=6)
        for dx, dy, delay in track:
            self.assertTrue(delay >= 6.0)
            self.assertLessEqual(abs(dy), 1.2)
        # 末端过冲后必然出现回拉, 否则不像真人拖动。
        self.assertTrue(any(step[0] < 0 for step in track))

    def test_track_ends_exactly_at_target_distance(self):
        random.seed(20261123)
        for distance in (80, 160, 300):
            self.assert_valid_track(distance, gen_track(distance))

    def test_track_duration_scales_with_distance(self):
        short = sum(step[2] for step in gen_track(60, rng=random.Random(1)))
        far = sum(step[2] for step in gen_track(320, rng=random.Random(1)))
        self.assertGreater(far, short)

    def test_custom_duration_is_respected(self):
        track = gen_track(200, duration_ms=500, rng=random.Random(2))
        total = sum(step[2] for step in track)
        # 每步延时在 0.7~1.35 倍之间抖动, 总和允许上下浮动。
        self.assertGreater(total, 500 * 0.6)
        self.assertLess(total, 500 * 1.5)


class FailureTipTests(unittest.TestCase):
    def test_initial_instruction_is_not_failure(self):
        self.assertFalse(is_failure_tip("向右拖动滑块填充拼图"))
        self.assertFalse(is_failure_tip(""))

    def test_failure_words_are_detected(self):
        for word in FAILURE_WORDS:
            self.assertTrue(is_failure_tip(f"验证{word},请重试"))


class UnsupportedModeTests(unittest.TestCase):
    def test_jigsaw_is_supported(self):
        self.assertIsNone(
            unsupported_mode_label("yidun yidun-custom yidun--light yidun--popup yidun--jigsaw")
        )

    def test_other_yidun_modes_are_reported(self):
        self.assertEqual(unsupported_mode_label("yidun yidun--sms"), "短信验证")
        self.assertEqual(unsupported_mode_label("yidun yidun--icon"), "图标点选")
        self.assertEqual(unsupported_mode_label("yidun yidun--word"), "文字点选")


class CalibrationTests(unittest.TestCase):
    def test_gap_bias_is_within_track_tolerance(self):
        # 易盾判定窗口很窄(0 与 +32 都失败), 不允许把这个校准值随意改大。
        self.assertGreater(GAP_BIAS_PX, 0)
        self.assertLessEqual(GAP_BIAS_PX, 24)


class CaptchaCommonAndRouterTests(unittest.TestCase):
    def test_no_global_cv2_np_and_no_circular_imports(self):
        import ast
        from pathlib import Path
        from modules import captcha_common, captcha_router, click_captcha, slider, tencent_slider

        self.assertFalse(hasattr(slider, "cv2"))
        self.assertFalse(hasattr(slider, "np"))
        self.assertIs(slider.gen_track, captcha_common.gen_track)
        self.assertIs(tencent_slider.gen_track, captcha_common.gen_track)
        self.assertTrue(callable(captcha_router.solve_slider_captcha))
        self.assertTrue(callable(captcha_router.solve_any_captcha))

        root = Path(__file__).resolve().parents[1] / "modules"
        ts_src = (root / "tencent_slider.py").read_text(encoding="utf-8")
        cc_src = (root / "click_captcha.py").read_text(encoding="utf-8")
        sl_src = (root / "slider.py").read_text(encoding="utf-8")
        self.assertNotIn("modules.slider", ts_src)
        self.assertNotIn("modules.tencent_slider", cc_src)
        self.assertNotIn("modules.tencent_slider", sl_src)
        self.assertNotIn("global cv2", sl_src)
        # Verify Linux font fallbacks exist in click_captcha
        fonts = click_captcha._font_candidates()
        self.assertTrue(any("DejaVuSans-Bold" in f for f in fonts))
        self.assertTrue(any("NotoSans-Bold" in f for f in fonts))
        ast.parse(sl_src)

    def test_fetch_image_bytes_uses_configured_proxy_on_requests_fallback(self):
        import asyncio
        from unittest.mock import patch
        from modules import captcha_common

        captured = {}

        class _FakeResp:
            content = b"PNG-BYTES"

        def _fake_get(url, **kwargs):
            captured["url"] = url
            captured["kwargs"] = kwargs
            return _FakeResp()

        captcha_common.set_captcha_proxy("http://127.0.0.1:7890")
        try:
            with patch("modules.captcha_common.requests.get", side_effect=_fake_get):
                data = asyncio.run(
                    captcha_common.fetch_image_bytes(None, "https://cdn.example.com/bg.png")
                )
        finally:
            captcha_common.set_captcha_proxy("")

        self.assertEqual(data, b"PNG-BYTES")
        self.assertEqual(captured["url"], "https://cdn.example.com/bg.png")
        self.assertEqual(
            captured["kwargs"].get("proxies"),
            {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"},
        )


if __name__ == "__main__":
    unittest.main()