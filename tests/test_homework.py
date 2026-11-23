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
        self.assertEqual(
            homework.parse_vision_answer(" thinking推理过程\nA"),
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
        self.assertEqual(stats, {"ok": 0, "fill": 0, "fail": 0})

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
        self.assertEqual(stats, {"ok": 0, "fill": 0, "fail": 0})


if __name__ == "__main__":
    unittest.main()