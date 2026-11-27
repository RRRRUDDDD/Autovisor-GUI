import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ROOT / "packages"
if PACKAGES.exists():
    sys.path.insert(0, str(PACKAGES))

from modules.click_captcha import _load_font, find_click_point, parse_click_instruction  # noqa: E402
from modules.tasks import VERIFICATION_SELECTORS  # noqa: E402

try:
    import cv2
    import numpy as np
except ImportError:
    cv2 = None
    np = None

COLOR_FIXTURE = ROOT / "tests" / "fixtures" / "color_click_captcha.png"
SIDEWAYS_FIXTURE = ROOT / "tests" / "fixtures" / "sideways_b_captcha.png"
GRAY_FIXTURE = ROOT / "tests" / "fixtures" / "gray_v_captcha.png"


class ClickInstructionTests(unittest.TestCase):
    def test_parses_same_color_digit_prompt(self):
        parsed = parse_click_instruction("请点击大写W颜色一样的数字4")
        self.assertEqual(
            parsed,
            {"mode": "same_color", "ref": "W", "target": "4", "color": None},
        )

    def test_parses_named_color_prompt(self):
        parsed = parse_click_instruction("请点击红色的数字7")
        self.assertEqual(
            parsed,
            {"mode": "named_color", "ref": None, "target": "7", "color": "红色"},
        )

    def test_parses_gray_letter_prompt(self):
        parsed = parse_click_instruction("请点击灰色小写v")
        self.assertEqual(
            parsed,
            {"mode": "named_color", "ref": None, "target": "v", "color": "灰色"},
        )

    def test_parses_sideways_letter_prompt(self):
        parsed = parse_click_instruction("请点击侧向的小写b")
        self.assertEqual(
            parsed,
            {
                "mode": "orientation",
                "orientation": "sideways",
                "target": "b",
                "ref": None,
                "color": None,
            },
        )

    def test_ignores_unrelated_text(self):
        self.assertIsNone(parse_click_instruction("请完成安全验证"))

    def test_course_selectors_include_tencent_iframe(self):
        self.assertTrue(
            any("tcaptcha" in selector for selector in VERIFICATION_SELECTORS)
        )


@unittest.skipIf(cv2 is None or np is None, "opencv/numpy 未安装")
class ClickPointTests(unittest.TestCase):
    def _draw_puzzle(self):
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (320, 160), (220, 220, 220))
        draw = ImageDraw.Draw(image)
        font = _load_font(48)
        draw.text((28, 28), "4", fill=(255, 210, 40), font=font)
        draw.text((118, 32), "W", fill=(255, 205, 35), font=font)
        draw.text((40, 92), "7", fill=(220, 50, 45), font=font)
        draw.text((128, 90), "4", fill=(235, 110, 145), font=font)
        draw.text((190, 88), "K", fill=(230, 120, 130), font=font)
        return cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)

    def test_picks_same_color_digit_on_synthetic_puzzle(self):
        image = self._draw_puzzle()
        point = find_click_point(
            image, "请点击大写W颜色一样的数字4", np, cv2
        )
        self.assertIsNotNone(point)
        x, y = point
        self.assertLess(x, 100, f"should click yellow 4 on the left, got {point}")
        self.assertLess(y, 85, f"should click the upper yellow 4, got {point}")

    def test_picks_yellow_four_on_live_tencent_screenshot(self):
        if not COLOR_FIXTURE.exists():
            self.skipTest("missing captcha fixture")
        image = cv2.imread(str(COLOR_FIXTURE))
        self.assertIsNotNone(image)
        point = find_click_point(
            image, "请点击大写W颜色一样的数字4", np, cv2
        )
        self.assertIsNotNone(point)
        x, y = point
        # Live sample: yellow 4 around (55, 58); pink 4 around (133, 118).
        self.assertLess(x, 90, f"clicked the wrong 4 at {point}")
        self.assertLess(y, 90, f"clicked the lower pink 4 at {point}")
        self.assertGreater(x, 35)
        self.assertGreater(y, 35)

    def test_picks_sideways_b_not_upright_b(self):
        if not SIDEWAYS_FIXTURE.exists():
            self.skipTest("missing captcha fixture")
        image = cv2.imread(str(SIDEWAYS_FIXTURE))
        self.assertIsNotNone(image)
        point = find_click_point(image, "请点击侧向的小写b", np, cv2)
        self.assertIsNotNone(point)
        x, y = point
        # Live sample: sideways blue b around (206, 86); upright green b around (252, 116).
        self.assertGreater(x, 170, f"should click the sideways b, got {point}")
        self.assertLess(x, 230, f"should not click the upright green b, got {point}")
        self.assertGreater(y, 60)
        self.assertLess(y, 110)

    def test_picks_gray_v_not_colored_v(self):
        if not GRAY_FIXTURE.exists():
            self.skipTest("missing captcha fixture")
        image = cv2.imread(str(GRAY_FIXTURE))
        self.assertIsNotNone(image)
        point = find_click_point(image, "请点击灰色小写v", np, cv2)
        self.assertIsNotNone(point)
        x, y = point
        # Live sample: gray v in the MV pair around (90, 109); colored V is on the right.
        self.assertLess(x, 140, f"should click the gray v, got {point}")
        self.assertGreater(y, 80, f"should click the lower gray v, got {point}")

    def test_solve_click_captcha_async_uses_asyncio_to_thread(self):
        import asyncio
        from unittest.mock import AsyncMock, patch
        from modules import click_captcha

        image = self._draw_puzzle()
        ok, buf = cv2.imencode(".png", image)
        self.assertTrue(ok)
        png_bytes = buf.tobytes()

        class _FakeLocator:
            async def screenshot(self, type="png"):
                return png_bytes

            async def bounding_box(self):
                return {"x": 10.0, "y": 20.0, "width": 260.0, "height": 140.0}

        clicked_points = []

        async def fake_click(_target, _loc, x, y, _shape):
            clicked_points.append((x, y))

        class _FakePage:
            async def wait_for_timeout(self, _ms):
                return None

        page = _FakePage()
        with (
            patch.object(
                click_captcha,
                "resolve_click_captcha_target",
                AsyncMock(side_effect=[page, None]),
            ),
            patch(
                "modules.tencent_slider.has_slider_thumb",
                AsyncMock(return_value=False),
            ),
            patch.object(
                click_captcha,
                "read_instruction",
                AsyncMock(return_value="请点击大写W颜色一样的数字4"),
            ),
            patch.object(
                click_captcha,
                "find_puzzle_image",
                AsyncMock(return_value=_FakeLocator()),
            ),
            patch.object(
                click_captcha,
                "click_image_point",
                side_effect=fake_click,
            ),
        ):
            solved = asyncio.run(
                click_captcha.solve_click_captcha(page, [np, cv2], attempts=1)
            )
        self.assertTrue(solved)
        self.assertEqual(len(clicked_points), 1)


class YidunVlmClickTests(unittest.TestCase):
    def _sample_png(self, width: int = 320, height: int = 160) -> bytes:
        from io import BytesIO
        from PIL import Image

        buf = BytesIO()
        Image.new("RGB", (width, height), (210, 220, 230)).save(buf, format="PNG")
        return buf.getvalue()

    def test_detect_yidun_click_mode(self):
        from modules.yidun_click import detect_yidun_click_mode

        self.assertEqual(detect_yidun_click_mode("yidun yidun--word"), "文字点选")
        self.assertEqual(detect_yidun_click_mode("yidun yidun--icon"), "图标点选")
        self.assertEqual(detect_yidun_click_mode("yidun yidun_word"), "文字点选")
        self.assertIsNone(detect_yidun_click_mode("yidun yidun--jigsaw"))
        self.assertIsNone(detect_yidun_click_mode("yidun yidun--sms"))

    def test_parse_click_point_supports_json_think_and_xy_regex(self):
        from modules.yidun_click import parse_click_point

        self.assertEqual(parse_click_point('{"x": 128, "y": 64}'), (128.0, 64.0))
        self.assertEqual(
            parse_click_point('```json\n{"x": 95, "y": 42}\n```'),
            (95.0, 42.0),
        )
        # 思考模型先输出 <think> 推理再输出最终 JSON 时，应忽略思考过程里的干扰坐标。
        think_reply = (
            '<think>先看左侧干扰字符 {"x": 20, "y": 30}，真正目标在右侧。</think>\n'
            '{"x": 210, "y": 88}'
        )
        self.assertEqual(
            parse_click_point(think_reply, width=320, height=160),
            (210.0, 88.0),
        )
        self.assertEqual(
            parse_click_point("坐标大约在 x=150, y=75 附近", width=320, height=160),
            (150.0, 75.0),
        )
        self.assertEqual(
            parse_click_point("中心位置 X：120.5，Y：64.2", width=320, height=160),
            (120.5, 64.2),
        )
        self.assertIsNone(parse_click_point('{"x": 400, "y": 50}', width=320, height=160))
        self.assertIsNone(parse_click_point('{"x": -5, "y": 50}', width=320, height=160))
        self.assertIsNone(parse_click_point('{"x": true, "y": 50}', width=320, height=160))
        self.assertIsNone(parse_click_point("无法识别"))

    def test_request_click_point_uses_sized_prompt_and_existing_chat_vision_sync(self):
        from unittest.mock import patch
        from modules import llm, yidun_click

        png = self._sample_png(320, 160)
        captured = {}

        # 与 llm_client.chat_vision_sync 完全一致的参数签名，验证不传递非法关键字参数。
        def _fake_chat_vision_sync(
            image_bytes,
            prompt,
            mime="image/png",
            conf_override=None,
            proxy=None,
        ):
            captured["image_len"] = len(image_bytes)
            captured["prompt"] = prompt
            captured["mime"] = mime
            return '{"x": 140, "y": 82}'

        with patch.object(llm, "chat_vision_sync", side_effect=_fake_chat_vision_sync):
            result = yidun_click.request_click_point(png, "请点击红色字母A")

        self.assertEqual(result, (140.0, 82.0, 320, 160))
        self.assertEqual(captured["image_len"], len(png))
        self.assertEqual(captured["mime"], "image/png")
        self.assertIn("320x160", captured["prompt"])
        self.assertIn("请点击红色字母A", captured["prompt"])

        # 若未来视觉接口显式支持 temperature / max_tokens，也应自动透传 0 / 8000。
        extended = {}

        def _extended_vision(
            image_bytes,
            prompt,
            mime="image/png",
            conf_override=None,
            proxy=None,
            *,
            temperature=None,
            max_tokens=None,
        ):
            extended["temperature"] = temperature
            extended["max_tokens"] = max_tokens
            return '{"x": 88, "y": 44}'

        with patch.object(llm, "chat_vision_sync", new=_extended_vision):
            ext_result = yidun_click.request_click_point(png, "请点击图标")

        self.assertEqual(ext_result, (88.0, 44.0, 320, 160))
        self.assertEqual(extended["temperature"], 0)
        self.assertEqual(extended["max_tokens"], 8000)

    def test_click_yidun_point_scales_coordinates_to_live_bounding_box(self):
        import asyncio
        from modules import yidun_click

        events = []

        class _FakeMouse:
            async def move(self, x, y):
                events.append(("move", x, y))

            async def down(self):
                events.append(("down",))

            async def up(self):
                events.append(("up",))

        class _FakeTarget:
            mouse = _FakeMouse()

        class _FakeBg:
            async def bounding_box(self):
                return {"x": 100.0, "y": 200.0, "width": 240.0, "height": 120.0}

        clicked = asyncio.run(
            yidun_click.click_yidun_point(_FakeTarget(), _FakeBg(), 160.0, 80.0, 320, 160)
        )
        # 160/320 * 240 + 100 = 220.0; 80/160 * 120 + 200 = 260.0
        self.assertEqual(clicked, (220.0, 260.0))
        moves = [ev for ev in events if ev[0] == "move"]
        self.assertGreaterEqual(len(moves), 2)
        self.assertEqual(moves[-1], ("move", 220.0, 260.0))
        self.assertIn(("down",), events)
        self.assertIn(("up",), events)

    def test_solve_yidun_click_falls_back_to_manual_when_vision_not_configured(self):
        import asyncio
        from unittest.mock import AsyncMock, patch
        from modules import captcha_router, llm, slider, yidun_click

        fake_page = object()
        with (
            patch.object(
                yidun_click,
                "resolve_yidun_click_target",
                AsyncMock(return_value=(fake_page, "文字点选")),
            ),
            patch.object(llm, "vision_enabled", return_value=False),
            patch.object(llm, "chat_vision_sync") as vision_mock,
            patch.object(slider, "solve_yidun", AsyncMock()) as slider_mock,
        ):
            solved = asyncio.run(
                captcha_router.solve_slider_captcha(fake_page, [None, None])
            )

        self.assertFalse(solved)
        vision_mock.assert_not_called()
        slider_mock.assert_not_called()

    def test_solve_yidun_click_retries_and_falls_back_after_3_failures(self):
        import asyncio
        from unittest.mock import AsyncMock, patch
        from modules import llm, yidun_click

        png = self._sample_png(320, 160)
        fake_page = object()
        fake_bg = object()
        refresh_mock = AsyncMock()
        click_mock = AsyncMock(return_value=(150.0, 240.0))

        with (
            patch.object(
                yidun_click,
                "resolve_yidun_click_target",
                AsyncMock(return_value=(fake_page, "图标点选")),
            ),
            patch.object(llm, "vision_enabled", return_value=True),
            patch.object(yidun_click, "_wait_click_ready", AsyncMock()),
            patch.object(yidun_click, "_read_tips", AsyncMock(return_value="请点击星星")),
            patch.object(yidun_click, "visible_locator", AsyncMock(return_value=fake_bg)),
            patch.object(yidun_click, "_capture_bg_bytes", AsyncMock(return_value=png)),
            patch.object(
                yidun_click,
                "request_click_point",
                return_value=(120.0, 60.0, 320, 160),
            ) as req_mock,
            patch.object(yidun_click, "click_yidun_point", click_mock),
            patch.object(yidun_click, "_wait_verify_result", AsyncMock(return_value=False)),
            patch.object(yidun_click, "_bg_visible", AsyncMock(return_value=True)),
            patch.object(yidun_click, "_refresh_click_captcha", refresh_mock),
        ):
            solved = asyncio.run(yidun_click.solve_yidun_click(fake_page))

        self.assertFalse(solved)
        self.assertEqual(req_mock.call_count, 3)
        self.assertEqual(click_mock.call_count, 3)
        self.assertEqual(refresh_mock.call_count, 2)

    def test_captcha_router_routes_yidun_click_to_vlm_and_succeeds(self):
        import asyncio
        from unittest.mock import AsyncMock, patch
        from modules import captcha_router, click_captcha, llm, slider, yidun_click

        png = self._sample_png(320, 160)
        fake_page = object()
        fake_bg = object()

        with (
            patch.object(
                click_captcha, "resolve_click_captcha_target", AsyncMock(return_value=None)
            ),
            patch.object(
                yidun_click,
                "resolve_yidun_click_target",
                AsyncMock(return_value=(fake_page, "文字点选")),
            ),
            patch.object(llm, "vision_enabled", return_value=True),
            patch.object(yidun_click, "_wait_click_ready", AsyncMock()),
            patch.object(yidun_click, "_read_tips", AsyncMock(return_value="请点击汉字春")),
            patch.object(yidun_click, "visible_locator", AsyncMock(return_value=fake_bg)),
            patch.object(yidun_click, "_capture_bg_bytes", AsyncMock(return_value=png)),
            patch.object(
                llm, "chat_vision_sync", return_value='{"x": 160, "y": 80}'
            ) as vision_mock,
            patch.object(
                yidun_click, "click_yidun_point", AsyncMock(return_value=(220.0, 260.0))
            ) as click_mock,
            patch.object(yidun_click, "_wait_verify_result", AsyncMock(return_value=True)),
            patch.object(slider, "solve_yidun", AsyncMock()) as slider_mock,
        ):
            solved = asyncio.run(
                captcha_router.solve_any_captcha(fake_page, [None, None], max_attempts=4)
            )

        self.assertTrue(solved)
        vision_mock.assert_called_once()
        click_mock.assert_awaited_once()
        slider_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
