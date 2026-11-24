# encoding=utf-8
import unittest
from unittest.mock import AsyncMock, patch

from modules import quiz_page


class _FakeQuizPage:
    def __init__(self, groups):
        self.groups = groups
        self.eval_calls = []

    async def evaluate(self, script, arg=None):
        self.eval_calls.append((script, arg))
        if script is quiz_page.JS_CLICK:
            return True
        return None

    async def wait_for_timeout(self, _ms):
        return None


class QuizPageCheckboxTests(unittest.IsolatedAsyncioTestCase):
    async def test_multiple_choice_unchecks_wrong_options_with_want_checked_false(self):
        groups = [
            {
                "name": "q1",
                "type": "checkbox",
                "options": [
                    {"text": "选项A", "idx": 0, "sel": True},
                    {"text": "选项B", "idx": 1, "sel": True},  # 错选，需取消
                    {"text": "选项C", "idx": 2, "sel": False},  # 漏选，需勾选
                ],
            }
        ]
        questions = [
            {
                "type": "multiple",
                "question": "多选题测试",
                "options": ["选项A", "选项B", "选项C"],
                "answer": [0, 2],
                "confidence": "high",
            }
        ]
        page = _FakeQuizPage(groups)
        click_calls = []
        orig_click = quiz_page._click

        async def fake_click(p, key, idx, want_checked=True):
            click_calls.append((key, idx, want_checked))
            return await orig_click(p, key, idx, want_checked=want_checked)

        with patch.object(quiz_page, "_click", side_effect=fake_click):
            ok, fail, filled = await quiz_page._answer_questions_once(
                page, questions, groups, pass_name="测试"
            )

        self.assertEqual((ok, fail, filled), (1, 0, 0))
        self.assertEqual(click_calls, [("q1", 2, True), ("q1", 1, False)])
        # Verify JS_CLICK received explicit wantChecked flags
        js_click_args = [arg for script, arg in page.eval_calls if script is quiz_page.JS_CLICK]
        self.assertEqual(js_click_args, [["q1", 2, True], ["q1", 1, False]])
        self.assertIn("el.checked === targetChecked", quiz_page.JS_CLICK)

    async def test_extract_quiz_text_prefers_scoped_container_and_page_cache_dedups_llm(self):
        class _ScopedPage:
            def __init__(self):
                self.body_calls = 0

            async def evaluate(self, script, arg=None):
                if script is quiz_page.JS_EXTRACT_QUIZ_TEXT:
                    return "1. 下列哪项正确？\nA. 选项甲\nB. 选项乙\n这是一个精简主容器测验文本。"
                if script is quiz_page.JS_COLLECT:
                    return [
                        {
                            "name": "q1",
                            "type": "radio",
                            "options": [
                                {"text": "选项甲", "idx": 0, "sel": False},
                                {"text": "选项乙", "idx": 1, "sel": False},
                            ],
                        }
                    ]
                if script is quiz_page.JS_CLICK:
                    return True
                return None

            async def inner_text(self, selector):
                self.body_calls += 1
                return "整页噪音导航栏..." * 50

            async def wait_for_timeout(self, _ms):
                return None

        page = _ScopedPage()
        extracted = await quiz_page._extract_quiz_text(page)
        self.assertIn("精简主容器测验文本", extracted)
        self.assertEqual(page.body_calls, 0)

        # Page cache avoids duplicate LLM calls between pass 1 ("首答") and pass 2 ("复核")
        solved_questions = [
            {
                "type": "single",
                "question": "下列哪项正确？",
                "options": ["选项甲", "选项乙"],
                "answer": [0],
                "confidence": "high",
            }
        ]
        solve_mock = AsyncMock(return_value=solved_questions)
        shared_cache = {}
        with (
            patch.object(quiz_page.llm, "solve_quiz_page", solve_mock),
            patch.object(quiz_page, "_click_confirm", AsyncMock(return_value=False)),
            patch.object(quiz_page, "_click_next", AsyncMock(return_value=False)),
        ):
            r1 = await quiz_page._answer_all_pages(
                page, "首答", page_cache=shared_cache
            )
            r2 = await quiz_page._answer_all_pages(
                page, "复核", page_cache=shared_cache
            )

        self.assertEqual(r1["ok"], 1)
        self.assertEqual(r2["ok"], 1)
        self.assertEqual(solve_mock.await_count, 1)

    async def test_scoped_empty_falls_back_to_body_and_failed_round_does_not_cache(self):
        # JS_EXTRACT_QUIZ_TEXT must NOT include stem-only .subject_describe
        self.assertNotIn("'.subject_describe'", quiz_page.JS_EXTRACT_QUIZ_TEXT)

        class _FallbackPage:
            def __init__(self):
                self.body_calls = 0

            async def evaluate(self, script, arg=None):
                if script is quiz_page.JS_EXTRACT_QUIZ_TEXT:
                    return "只有导航和标题文字超过三十个字符但没有选项内容，导致首轮解析为空。"
                if script is quiz_page.JS_COLLECT:
                    return [
                        {
                            "name": "q1",
                            "type": "radio",
                            "options": [
                                {"text": "正确选项", "idx": 0, "sel": False},
                                {"text": "干扰选项", "idx": 1, "sel": False},
                            ],
                        }
                    ]
                if script is quiz_page.JS_CLICK:
                    return True
                return None

            async def inner_text(self, selector):
                self.body_calls += 1
                return "1. 完整页面题目？\nA. 正确选项\nB. 干扰选项"

            async def wait_for_timeout(self, _ms):
                return None

        page = _FallbackPage()
        bad_q = [
            {
                "type": "single",
                "question": "完整页面题目？",
                "options": ["不存在的选项"],
                "answer": [0],
                "confidence": "low",
            }
        ]
        good_q = [
            {
                "type": "single",
                "question": "完整页面题目？",
                "options": ["正确选项", "干扰选项"],
                "answer": [0],
                "confidence": "high",
            }
        ]
        # 1st call on scoped returns [] -> falls back to body returning bad_q (fail=1, not cached)
        # 2nd pass ("复核"): scoped -> [], body -> good_q (re-queries LLM instead of reusing failed cache)
        solve_mock = AsyncMock(side_effect=[[], bad_q, [], good_q])
        shared_cache = {}
        with (
            patch.object(quiz_page.llm, "solve_quiz_page", solve_mock),
            patch.object(quiz_page, "_click_confirm", AsyncMock(return_value=False)),
            patch.object(quiz_page, "_click_next", AsyncMock(return_value=False)),
        ):
            r1 = await quiz_page._answer_all_pages(page, "首答", page_cache=shared_cache)
            r2 = await quiz_page._answer_all_pages(page, "复核", page_cache=shared_cache)

        self.assertGreaterEqual(page.body_calls, 2)
        self.assertEqual(r1["fail"], 1)
        self.assertEqual(r2["ok"], 1)
        self.assertEqual(solve_mock.await_count, 4)

    async def test_format_selected_options_and_skips_already_checked_single_choice(self):
        q = {
            "type": "single",
            "question": "1+1=?",
            "options": ["1", "2", "3"],
            "answer": [1],
            "confidence": "high",
        }
        self.assertEqual(quiz_page._format_selected_options(q, [1]), "B.2")
        self.assertEqual(quiz_page._format_selected_options(q, [9]), "#9")

        groups = [
            {
                "name": "radio1",
                "type": "radio",
                "options": [
                    {"text": "1", "idx": 0, "sel": False},
                    {"text": "2", "idx": 1, "sel": True},  # 已经选中正确答案
                    {"text": "3", "idx": 2, "sel": False},
                ],
            }
        ]
        page = _FakeQuizPage(groups)
        click_mock = AsyncMock(return_value=True)
        with patch.object(quiz_page, "_click", click_mock):
            ok, fail, filled = await quiz_page._answer_questions_once(
                page, [q], groups, pass_name="复核"
            )
        self.assertEqual((ok, fail, filled), (1, 0, 0))
        click_mock.assert_not_awaited()

    async def test_fill_blank_question_writes_textbox(self):
        class _TextboxPage:
            def __init__(self):
                self.filled_args = []

            async def evaluate(self, script, arg=None):
                if script is quiz_page.JS_TEXTBOXES:
                    return [{"i": 0}]
                if script is quiz_page.JS_FILL:
                    self.filled_args.append(arg)
                    return True
                return None

            async def wait_for_timeout(self, _ms):
                return None

        page = _TextboxPage()
        questions = [
            {
                "type": "fill",
                "question": "中国的首都是____",
                "options": [],
                "answer": ["北京"],
                "confidence": "high",
            }
        ]
        ok, fail, filled = await quiz_page._answer_questions_once(
            page, questions, [], pass_name="首答"
        )
        self.assertEqual((ok, fail, filled), (0, 0, 1))
        self.assertEqual(page.filled_args, [[0, "北京", False]])


if __name__ == "__main__":
    unittest.main()
