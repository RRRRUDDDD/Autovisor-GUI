import asyncio
import random
import unittest
from unittest.mock import AsyncMock, patch

from modules.slider import (
    FAILURE_WORDS,
    GAP_BIAS_PX,
    _match_yidun_gap,
    download_image,
    gap_to_slider_distance,
    gen_track,
    image_to_display_point,
    is_failure_tip,
    process_block_image,
    unsupported_mode_label,
)

try:
    import cv2
    import numpy as np
except ImportError:
    cv2 = None
    np = None


class ImageToDisplayPointTests(unittest.TestCase):
    def test_scales_match_template_result_to_css_pixels(self):
        # 480x240 原图显示为 400x200 时, 匹配点 (120, 60) 应换算为 (100, 50)。
        self.assertEqual(image_to_display_point((120, 60), 480, 240, 400, 200), (100.0, 50.0))


class GenTrackTests(unittest.TestCase):
    def assert_valid_track(self, distance, track):
        self.assertGreaterEqual(len(track), 10)
        self.assertAlmostEqual(sum(step[0] for step in track), distance, places=6)
        for _dx, dy, delay in track:
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


class YidunDomGeometryTests(unittest.TestCase):
    def test_uses_dom_ratio_when_boxes_complete(self):
        bg_box = {"x": 50.0, "y": 100.0, "width": 320.0, "height": 160.0}
        block_box = {"x": 50.0, "y": 100.0, "width": 60.0, "height": 160.0}
        track_box = {"x": 50.0, "y": 270.0, "width": 320.0, "height": 40.0}
        slider_box = {"x": 50.0, "y": 270.0, "width": 40.0, "height": 40.0}

        distance, used_dom = gap_to_slider_distance(
            180.0, bg_box, block_box, track_box, slider_box
        )
        self.assertTrue(used_dom)
        self.assertAlmostEqual(distance, 180.0 * (280.0 / 260.0), places=2)

    def test_subtracts_initial_block_offset(self):
        bg_box = {"x": 50.0, "y": 100.0, "width": 320.0, "height": 160.0}
        block_box = {"x": 56.0, "y": 100.0, "width": 60.0, "height": 160.0}
        track_box = {"x": 50.0, "y": 270.0, "width": 320.0, "height": 40.0}
        slider_box = {"x": 50.0, "y": 270.0, "width": 40.0, "height": 40.0}

        distance, used_dom = gap_to_slider_distance(
            180.0, bg_box, block_box, track_box, slider_box
        )
        self.assertTrue(used_dom)
        self.assertAlmostEqual(distance, 174.0 * (280.0 / 260.0), places=2)

    def test_falls_back_to_gap_bias_when_track_or_slider_missing(self):
        bg_box = {"x": 50.0, "y": 100.0, "width": 320.0, "height": 160.0}
        distance, used_dom = gap_to_slider_distance(150.0, bg_box)
        self.assertFalse(used_dom)
        self.assertAlmostEqual(distance, 150.0 + GAP_BIAS_PX, places=2)

    def test_clamps_distance_within_track_range(self):
        bg_box = {"x": 0.0, "y": 0.0, "width": 320.0, "height": 160.0}
        block_box = {"x": 0.0, "y": 0.0, "width": 60.0, "height": 160.0}
        track_box = {"x": 0.0, "y": 170.0, "width": 320.0, "height": 40.0}
        slider_box = {"x": 0.0, "y": 170.0, "width": 40.0, "height": 40.0}

        distance, used_dom = gap_to_slider_distance(
            310.0, bg_box, block_box, track_box, slider_box
        )
        self.assertTrue(used_dom)
        self.assertLessEqual(distance, 280.0)


@unittest.skipUnless(cv2 is not None and np is not None, "需要 opencv 与 numpy")
class YidunAlphaSliderTests(unittest.TestCase):
    def _build_synthetic_yidun_pair(self, gap_x: int = 176):
        height, width = 160, 320
        strip_w = 60
        rng = np.random.default_rng(2026)

        # 生成带多尺度纹理的背景图，模拟真实摄影背景。
        bg = np.zeros((height, width, 3), dtype=np.uint8)
        for y in range(height):
            for c in range(3):
                bg[y, :, c] = np.clip(
                    40 + (y * (c + 1)) % 140 + np.linspace(0, 70, width),
                    0,
                    255,
                ).astype(np.uint8)
        cv2.circle(bg, (75, 55), 32, (210, 140, 60), 3)
        cv2.rectangle(bg, (115, 22), (285, 138), (60, 175, 130), 2)
        cv2.line(bg, (20, 140), (300, 20), (230, 80, 90), 2)

        # 构造拼图轮廓掩码（位于 60x160 前景条带中部）。
        silhouette = np.zeros((height, strip_w), dtype=np.uint8)
        cv2.rectangle(silhouette, (8, 52), (44, 108), 255, -1)
        cv2.circle(silhouette, (44, 80), 10, 255, -1)
        cv2.circle(silhouette, (26, 52), 9, 255, -1)

        # 构造带 PNG 透明通道的前景条带：透明区填入高对比噪点验证 Alpha 掩码过滤。
        block = np.zeros((height, strip_w, 4), dtype=np.uint8)
        noise_rgb = rng.integers(0, 255, size=(height, strip_w, 3), dtype=np.uint8)
        block[:, :, :3] = noise_rgb
        # 透明区加一部分 alpha=30 (<=50) 的半透明噪点，确保阈值 >50 会将其滤除。
        block[:30, :, 3] = 30

        # 内部区域写入带亮边框与纹理的拼图块，并在背景图对应位置刻出一致缺口。
        patch = bg[:, gap_x : gap_x + strip_w].copy()
        inner_border = cv2.subtract(
            silhouette, cv2.erode(silhouette, np.ones((5, 5), np.uint8))
        )
        patch[inner_border > 0] = (245, 245, 245)
        block[silhouette > 0, :3] = patch[silhouette > 0]
        block[silhouette > 0, 3] = 255

        bg_roi = bg[:, gap_x : gap_x + strip_w]
        bg_roi[silhouette > 0] = (bg_roi[silhouette > 0] * 0.45).astype(np.uint8)
        bg_roi[inner_border > 0] = (245, 245, 245)
        return bg, block, silhouette

    def test_alpha_mask_erosion_filters_transparent_noise_and_matches_gap(self):
        expected_x = 176
        bg, block, silhouette = self._build_synthetic_yidun_pair(gap_x=expected_x)

        block_edges = process_block_image(block, cv2=cv2, np=np)
        # 透明区域（alpha <= 50）不应残留任何边缘像素。
        self.assertEqual(int(np.count_nonzero(block_edges[silhouette == 0])), 0)
        self.assertGreater(int(np.count_nonzero(block_edges)), 20)

        score, max_loc = _match_yidun_gap(bg, block, cv2, np)
        self.assertGreater(score, 0.25)
        self.assertAlmostEqual(max_loc[0], expected_x, delta=2)

    def test_download_image_preserves_alpha_when_unchanged_true(self):
        rgba = np.zeros((20, 24, 4), dtype=np.uint8)
        rgba[4:16, 4:20, :3] = (90, 140, 200)
        rgba[4:16, 4:20, 3] = 255
        ok, buf = cv2.imencode(".png", rgba)
        self.assertTrue(ok)
        png_bytes = buf.tobytes()

        with patch(
            "modules.slider.fetch_image_bytes",
            AsyncMock(return_value=png_bytes),
        ):
            img_alpha = asyncio.run(
                download_image(
                    "https://cdn.example.com/jigsaw.png",
                    np=np,
                    cv2=cv2,
                    unchanged=True,
                )
            )
            img_bgr = asyncio.run(
                download_image(
                    "https://cdn.example.com/bg.png",
                    np=np,
                    cv2=cv2,
                    unchanged=False,
                )
            )

        self.assertEqual(img_alpha.shape, (20, 24, 4))
        self.assertEqual(int(img_alpha[0, 0, 3]), 0)
        self.assertEqual(int(img_alpha[10, 10, 3]), 255)
        self.assertEqual(img_bgr.shape, (20, 24, 3))

    def test_progress_img_downloads_block_with_unchanged_true(self):
        from modules import slider

        expected_x = 144
        bg, block, _ = self._build_synthetic_yidun_pair(gap_x=expected_x)
        download_calls = []

        async def _fake_download(url, page=None, *, np=None, cv2=None, proxy=None, unchanged=False):
            download_calls.append((url, unchanged))
            return block if "jigsaw" in url else bg

        class _FakeLocator:
            def __init__(self, *, src="", box=None, nat_size=None, visible=True):
                self._src = src
                self._box = box
                self._nat = nat_size
                self._visible = visible
                self.first = self

            async def count(self):
                return 1 if self._visible else 0

            async def is_visible(self):
                return self._visible

            async def wait_for(self, **kwargs):
                return None

            async def get_attribute(self, name):
                return self._src if name == "src" else None

            async def bounding_box(self):
                return self._box

            async def evaluate(self, _expr):
                return self._nat

        bg_loc = _FakeLocator(
            src="https://cdn.example.com/bg.jpg",
            box={"x": 20.0, "y": 40.0, "width": 320.0, "height": 160.0},
            nat_size={"width": 320, "height": 160},
        )
        block_loc = _FakeLocator(
            src="https://cdn.example.com/jigsaw.png",
            box={"x": 20.0, "y": 40.0, "width": 60.0, "height": 160.0},
        )
        track_loc = _FakeLocator(
            box={"x": 20.0, "y": 210.0, "width": 320.0, "height": 40.0}
        )
        slider_loc = _FakeLocator(
            box={"x": 20.0, "y": 210.0, "width": 40.0, "height": 40.0}
        )
        hidden_loc = _FakeLocator(visible=False)

        class _FakePage:
            def locator(self, selector):
                if "loading" in selector:
                    return hidden_loc
                return hidden_loc

        async def _fake_visible(_target, selector):
            if selector == slider.YIDUN_BG_SELECTOR:
                return bg_loc
            if selector == slider.YIDUN_BLOCK_SELECTOR:
                return block_loc
            if selector == slider.YIDUN_TRACK_SELECTOR:
                return track_loc
            if selector == slider.YIDUN_SLIDER_SELECTOR:
                return slider_loc
            return None

        with (
            patch.object(slider, "_visible_locator", side_effect=_fake_visible),
            patch.object(slider, "download_image", side_effect=_fake_download),
        ):
            gap_x, _ = asyncio.run(slider.progress_img(_FakePage(), modules=[np, cv2]))

        self.assertAlmostEqual(gap_x, expected_x, delta=2)
        self.assertEqual(
            download_calls,
            [
                ("https://cdn.example.com/bg.jpg", False),
                ("https://cdn.example.com/jigsaw.png", True),
            ],
        )

    def test_solve_yidun_preserves_piece_offset_on_dom_fallback_and_clamps_negative(self):
        from modules import slider

        moved = []

        async def _fake_move(_target, distance, bias=slider.GAP_BIAS_PX):
            moved.append((float(distance), float(bias)))

        # 1) used_dom=False 且存在 piece_offset=10（gap_x=150 -> drag_distance=140+GAP_BIAS_PX）
        with (
            patch.object(slider, "_wait_puzzle_ready", AsyncMock()),
            patch.object(
                slider,
                "_measure_yidun_puzzle",
                AsyncMock(return_value=(150.0, 40.0, 140.0 + GAP_BIAS_PX, False, 0.8)),
            ),
            patch.object(slider, "move_slider", side_effect=_fake_move),
            patch.object(slider, "_wait_verify_result", AsyncMock(return_value=True)),
        ):
            ok = asyncio.run(slider.solve_yidun(object(), [np, cv2], max_attempts=1))
        self.assertTrue(ok)
        self.assertEqual(len(moved), 1)
        # 基线应保留 piece_offset 扣除后的 140.0，而非原始 gap_x=150.0
        self.assertAlmostEqual(moved[0][0], 140.0, places=3)
        self.assertAlmostEqual(moved[0][1], GAP_BIAS_PX, places=3)

        # 2) used_dom=True 且 drag_distance=1.0，第 3 次尝试 probe=-2.0 不得传入负位移
        moved.clear()
        with (
            patch.object(slider, "_wait_puzzle_ready", AsyncMock()),
            patch.object(
                slider,
                "_measure_yidun_puzzle",
                AsyncMock(return_value=(1.0, 40.0, 1.0, True, 0.8)),
            ),
            patch.object(slider, "move_slider", side_effect=_fake_move),
            patch.object(slider, "_wait_verify_result", AsyncMock(return_value=False)),
            patch.object(slider, "_puzzle_visible", AsyncMock(return_value=True)),
            patch.object(slider, "_refresh_puzzle", AsyncMock()),
        ):
            ok2 = asyncio.run(slider.solve_yidun(object(), [np, cv2], max_attempts=3))
        self.assertFalse(ok2)
        self.assertEqual(len(moved), 3)
        self.assertAlmostEqual(moved[0][0] + moved[0][1], 1.0, places=3)
        self.assertAlmostEqual(moved[1][0] + moved[1][1], 3.0, places=3)
        self.assertGreaterEqual(moved[2][0], 0.0)
        self.assertEqual(moved[2][0] + moved[2][1], 0.0)

    def test_opencv_edge_cases_gray_bgr_transparent_and_oversized_block(self):
        bg, block, _ = self._build_synthetic_yidun_pair(gap_x=120)

        # 2D 灰度块与 3 通道 BGR 块均应正常提取边缘并完成匹配
        gray_block = cv2.cvtColor(block[:, :, :3], cv2.COLOR_BGR2GRAY)
        bgr_block = block[:, :, :3]
        self.assertGreater(int(np.count_nonzero(process_block_image(gray_block, cv2=cv2, np=np))), 0)
        self.assertGreater(int(np.count_nonzero(process_block_image(bgr_block, cv2=cv2, np=np))), 0)

        # 全透明空 Alpha 块应返回 0.0 匹配分
        transparent = np.zeros((160, 60, 4), dtype=np.uint8)
        score, loc = _match_yidun_gap(bg, transparent, cv2, np)
        self.assertEqual(score, 0.0)
        self.assertEqual(loc, (0, 0))

        # front > background 应抛出 ValueError
        oversized = np.full((200, 360, 4), 255, dtype=np.uint8)
        with self.assertRaises(ValueError):
            _match_yidun_gap(bg, oversized, cv2, np)


if __name__ == "__main__":
    unittest.main()