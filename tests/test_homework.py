import types
import unittest
from unittest.mock import AsyncMock, patch

from modules import homework


class HomeworkUrlTests(unittest.TestCase):
    def test_detects_answer_pages_and_ignores_lists(self):
        homework_url = (
            "https://onlineexamh5new.zhihuishu.com/stuExamWeb.html"
            "#/webExamList/dohomework/458533/tok1/tok2/1000013343/1847/0"
        )
        exam_url = (
            "https://onlineexamh5new.zhihuishu.com/stuExamWeb.html"
            "#/webExamList/doexam/458533/tok1/tok2/1000013343/1847/0"
        )
        self.assertTrue(homework.is_homework_url(homework_url))
        self.assertTrue(homework.is_homework_url(exam_url))

        list_url = (
            "https://onlineexamh5new.zhihuishu.com/stuExamWeb.html"
            "#/webExamList?recruitId=%2BWogM%2BWCUyf3AymMKLqxwg%3D%3D"
        )
        report_url = "https://onlineexamh5new.zhihuishu.com/stuLearnReportNew.html#/report/1"
        course_url = "https://studyvideoh5.zhihuishu.com/stuStudy?recruitAndCourseId=abc"
        self.assertFalse(homework.is_homework_url(list_url))
        self.assertFalse(homework.is_homework_url(report_url))
        self.assertFalse(homework.is_homework_url(course_url))

        self.assertTrue(homework.is_non_quiz_exam_page(list_url))
        self.assertTrue(homework.is_non_quiz_exam_page(report_url))
        self.assertFalse(homework.is_non_quiz_exam_page(homework_url))
        self.assertFalse(homework.is_non_quiz_exam_page(course_url))


class VisionAnswerTests(unittest.TestCase):
    def test_parses_single_and_multiple_choice(self):
        self.assertEqual(homework.parse_vision_answer("B"), ([1], ""))
        self.assertEqual(homework.parse_vision_answer("ABD"), ([0, 1, 3], ""))
        self.assertEqual(homework.parse_vision_answer("A、C"), ([0, 2], ""))
        self.assertEqual(homework.parse_vision_answer("答案：B"), ([1], ""))
        self.assertEqual(homework.parse_vision_answer("正确答案是 C"), ([2], ""))
        self.assertEqual(homework.parse_vision_answer("选b"), ([1], ""))

    def test_parses_last_line_and_filters_out_of_range(self):
        reply = "先看题干...\n最终答案：D"
        self.assertEqual(homework.parse_vision_answer(reply), ([3], ""))
        self.assertEqual(homework.parse_vision_answer("AB", option_count=2), ([0, 1], ""))
        self.assertEqual(homework.parse_vision_answer("D", option_count=2), ([], ""))
        self.assertEqual(homework.parse_vision_answer(""), ([], ""))

    def test_parses_fill_answer_and_think_tags(self):
        self.assertEqual(
            homework.parse_vision_answer("FILL: 光的折射"),
            ([], "光的折射"),
        )
        think = "<thi" + "nk>"
        end = "</thi" + "nk>"
        self.assertEqual(
            homework.parse_vision_answer(f"{think}推理过程\nA{end}B"),
            ([1], ""),
        )
        self.assertEqual(
            homework.parse_vision_answer(f"{think}推理过程\nA"),
            ([0], ""),
        )


class _FakePage:
    url = (
        "https://onlineexamh5new.zhihuishu.com/stuExamWeb.html"
        "#/webExamList/dohomework/458533/tok1/tok2/1000013343/1847/0"
    )


class _QuietLogger:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


class HybridRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = patch.object(homework, "logger", _QuietLogger())
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_api_result_is_returned_without_visual_flow(self):
        expected = {"answered": 3, "submitted": True, "needs_vision": False}
        with patch.object(
            homework, "_try_zhidao_api", new=AsyncMock(return_value=expected)
        ) as api_call:
            stats = await homework.auto_answer_homework(_FakePage(), config=None)
        self.assertEqual(stats, expected)
        api_call.assert_awaited_once()

    async def test_image_questions_fall_back_to_visual_flow(self):
        api_stats = {"answered": 0, "needs_vision": True}
        with patch.object(
            homework, "_try_zhidao_api", new=AsyncMock(return_value=api_stats)
        ):
            with patch.object(homework.llm, "vision_enabled", return_value=False):
                stats = await homework.auto_answer_homework(_FakePage(), config=None)
        self.assertEqual(stats, {"ok": 0, "fill": 0, "fail": 0, "submitted": False})

    async def test_disabled_api_flag_skips_encrypted_route(self):
        config = types.SimpleNamespace(
            enableHomeworkApi=False, enableFormalExamApi=True, proxy=""
        )
        with patch.object(
            homework, "_try_zhidao_api", new=AsyncMock()
        ) as api_call:
            with patch.object(homework.llm, "vision_enabled", return_value=False):
                stats = await homework.auto_answer_homework(_FakePage(), config=config)
        api_call.assert_not_awaited()
        self.assertEqual(stats, {"ok": 0, "fill": 0, "fail": 0, "submitted": False})

    async def test_captcha_settles_then_retries_api(self):
        from modules.zhs_api import CaptchaRequiredError

        expected = {"answered": 2, "submitted": False, "needs_vision": False}
        attempts = {"count": 0}

        async def fake_api(_page, _config, _params):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise CaptchaRequiredError("需要滑块验证")
            return expected

        settle = AsyncMock(return_value=True)
        with patch.object(homework, "_try_zhidao_api", new=fake_api):
            with patch.object(homework, "_settle_captcha", new=settle):
                stats = await homework.auto_answer_homework(_FakePage(), config=None)

        self.assertEqual(stats, expected)
        self.assertEqual(attempts["count"], 2)
        settle.assert_awaited_once()

    async def test_visual_loop_counts_skipped_answered_questions_in_seen(self):
        class _DummyQuestion:
            def __init__(self, sig, answered):
                self.sig = sig
                self.answered = answered

            async def screenshot(self, **_kwargs):
                return b"fake-png"

        class _VisualPage:
            url = (
                "https://onlineexamh5new.zhihuishu.com/stuExamWeb.html"
                "#/webExamList/dohomework/458533/tok1/tok2/1000013343/1847/0"
            )

            async def wait_for_load_state(self, *_args, **_kwargs):
                return None

            async def reload(self, *_args, **_kwargs):
                return None

        questions = [_DummyQuestion("sig-1", True), _DummyQuestion("sig-2", True)]
        cursor = {"idx": 0}

        async def fake_visible(_page):
            return questions[cursor["idx"]]

        async def fake_sig(handle):
            return handle.sig

        async def fake_has_answer(handle):
            return handle.answered

        async def fake_goto_next(_page, _sig=None):
            if cursor["idx"] == 0:
                cursor["idx"] = 1
                return True, True
            # 最后一题：下一题按钮仍可见但点击不再翻页
            return False, True

        save_or_submit = AsyncMock()
        config = types.SimpleNamespace(
            enableHomeworkApi=False, enableFormalExamApi=False, proxy=""
        )
        with (
            patch.object(homework.llm, "vision_enabled", return_value=True),
            patch.object(homework.llm, "vision_label", return_value="test-vl"),
            patch.object(homework.llm, "conf", return_value={"auto_submit": True}),
            patch.object(homework, "_wait_question", new=AsyncMock(return_value=questions[0])),
            patch.object(homework, "_visible_question", new=fake_visible),
            patch.object(homework, "_signature", new=fake_sig),
            patch.object(homework, "_has_answer", new=fake_has_answer),
            patch.object(homework, "_question_total", new=AsyncMock(return_value=2)),
            patch.object(homework, "_goto_next", new=fake_goto_next),
            patch.object(homework, "_save_or_submit", new=save_or_submit),
        ):
            stats = await homework.auto_answer_homework(_VisualPage(), config=config)

        self.assertEqual(stats, {"ok": 0, "fill": 0, "fail": 0, "submitted": True})
        # seen 已计满 total=2，不应误判 stuck=True 而取消自动提交
        save_or_submit.assert_awaited_once()
        self.assertTrue(save_or_submit.await_args.args[1])

    async def test_visual_loop_sets_submitted_false_when_stuck(self):
        class _DummyQuestion:
            def __init__(self, sig, answered):
                self.sig = sig
                self.answered = answered

        class _VisualPage:
            url = (
                "https://onlineexamh5new.zhihuishu.com/stuExamWeb.html"
                "#/webExamList/dohomework/458533/tok1/tok2/1000013343/1847/0"
            )

            async def wait_for_load_state(self, *_args, **_kwargs):
                return None

        q1 = _DummyQuestion("sig-1", True)
        save_or_submit = AsyncMock()
        config = types.SimpleNamespace(
            enableHomeworkApi=False, enableFormalExamApi=False, proxy=""
        )
        with (
            patch.object(homework.llm, "vision_enabled", return_value=True),
            patch.object(homework.llm, "vision_label", return_value="test-vl"),
            patch.object(homework.llm, "conf", return_value={"auto_submit": True}),
            patch.object(homework, "_wait_question", new=AsyncMock(return_value=q1)),
            patch.object(homework, "_visible_question", new=AsyncMock(return_value=q1)),
            patch.object(homework, "_signature", new=AsyncMock(return_value="sig-1")),
            patch.object(homework, "_has_answer", new=AsyncMock(return_value=True)),
            patch.object(homework, "_question_total", new=AsyncMock(return_value=5)),
            patch.object(homework, "_goto_next", new=AsyncMock(return_value=(False, True))),
            patch.object(homework, "_save_or_submit", new=save_or_submit),
        ):
            stats = await homework.auto_answer_homework(_VisualPage(), config=config)

        self.assertEqual(stats, {"ok": 0, "fill": 0, "fail": 0, "submitted": False})
        save_or_submit.assert_awaited_once()
        self.assertFalse(save_or_submit.await_args.args[1])

    async def test_skip_api_goes_straight_to_vision_without_calling_api(self):
        api_call = AsyncMock()
        config = types.SimpleNamespace(
            enableHomeworkApi=True, enableFormalExamApi=True, proxy=""
        )
        with (
            patch.object(homework, "_try_api_with_captcha", api_call),
            patch.object(homework.llm, "vision_enabled", return_value=False),
        ):
            stats = await homework.auto_answer_homework(
                _FakePage(), config=config, skip_api=True
            )
        api_call.assert_not_awaited()
        self.assertEqual(stats, {"ok": 0, "fill": 0, "fail": 0, "submitted": False})

    async def test_settle_captcha_reloads_when_not_visible_and_exits_after_appear_timeout(self):
        class _Clock:
            def __init__(self):
                self.now = 100.0

            def monotonic(self):
                return self.now

        clock = _Clock()
        reloads = []
        waits = []

        class _CaptchaPage:
            async def reload(self, **kwargs):
                reloads.append(kwargs)

            async def wait_for_timeout(self, ms):
                waits.append(ms)
                clock.now += ms / 1000.0

        config = types.SimpleNamespace(enableAutoCaptcha=False, enableHideWindow=False)
        with (
            patch.object(homework.time, "monotonic", side_effect=clock.monotonic),
            patch("modules.slider.captcha_visible", new=AsyncMock(return_value=False)),
            patch("modules.window_control.should_hide_window", return_value=False),
        ):
            solved = await homework._settle_captcha(_CaptchaPage(), config)

        self.assertFalse(solved)
        self.assertEqual(len(reloads), 1)
        # 从未出现验证码时应在 _CAPTCHA_APPEAR_TIMEOUT 处提前退出，远小于 120s
        self.assertLessEqual(len(waits), 30)

    async def test_settle_captcha_keeps_waiting_once_captcha_appears(self):
        class _Clock:
            def __init__(self):
                self.now = 100.0

            def monotonic(self):
                return self.now

        clock = _Clock()

        class _CaptchaPage:
            async def reload(self, **_kwargs):
                return None

            async def wait_for_timeout(self, ms):
                clock.now += ms / 1000.0

        # 验证码在第 102s 出现，直到第 140s（超过 25s appear 超时，但仍在 120s 内）被用户解除
        async def fake_visible(_page):
            return 102.0 <= clock.now < 140.0

        config = types.SimpleNamespace(enableAutoCaptcha=False, enableHideWindow=False)
        with (
            patch.object(homework.time, "monotonic", side_effect=clock.monotonic),
            patch("modules.slider.captcha_visible", new=fake_visible),
            patch("modules.window_control.should_hide_window", return_value=False),
        ):
            solved = await homework._settle_captcha(_CaptchaPage(), config)

        self.assertTrue(solved)

    async def test_js_has_answer_and_signature_ignore_hidden_inputs(self):
        # 静态契约验证：JS_HAS_ANSWER 只检查可见文本输入，不把 <input type="hidden" value="[]"> 误判为已作答
        self.assertIn("textTypes", homework.JS_HAS_ANSWER)
        self.assertNotIn('i.type !== "radio" && i.type !== "checkbox"', homework.JS_HAS_ANSWER)
        # JS_SIGNATURE 优先读取知到 anchor_<eid> 并过滤 hidden 输入
        self.assertIn("[id^='anchor_']", homework.JS_SIGNATURE)
        self.assertIn('!== "hidden"', homework.JS_SIGNATURE)

    async def test_wait_question_images_polls_until_ready(self):
        calls = {"count": 0}

        class _ImgQuestion:
            async def evaluate(self, _js):
                calls["count"] += 1
                return calls["count"] >= 2

        await homework._wait_question_images(_ImgQuestion(), timeout=2.0)
        self.assertEqual(calls["count"], 2)


if __name__ == "__main__":
    unittest.main()