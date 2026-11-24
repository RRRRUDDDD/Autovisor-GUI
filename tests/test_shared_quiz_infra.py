# encoding=utf-8
import unittest

from modules.answer_parser import (
    QUESTION_TYPE_NAMES,
    QuestionType,
    answer_separator,
    extract_answer_block,
    letters_to_indexes,
    parse_answer_ids,
    parse_homework_answer,
    parse_reference_answer_text,
    parse_vision_answer,
)
from modules.answer_service import LlmCircuitBreaker, QuestionBankHelper
from modules.exam_ctx import ExamApi
from modules.llm import NonRetryableLLMError
from modules.quiz_dom import (
    PlaywrightError,
    TargetClosedError,
    click_button_by_text,
    confirm_dialog,
    is_target_closed_error,
)
from modules.text_utils import (
    brief,
    cache_key,
    compact_text,
    loose_key,
    normalize_text,
    parse_percent,
    similarity,
    strip_think_tags,
)
from modules.zhs_api import ZhsApiClient


class TextUtilsTests(unittest.TestCase):
    def test_text_normalization_and_keys(self):
        self.assertEqual(normalize_text("  a \n\t  b  "), "a b")
        self.assertEqual(compact_text(" a\u3000b \n c "), "abc")
        self.assertEqual(brief("<p>hello   <b>world</b></p>", limit=8, strip_html=True), "hello wo...")
        self.assertEqual(strip_think_tags("<think>reasoning</think>```answer\n[1]\n```"), "```answer\n[1]\n```")
        self.assertEqual(strip_think_tags("final<think>unclosed"), "final")
        self.assertEqual(cache_key("测试 题干（ ）（）"), "测试题干")
        self.assertEqual(loose_key("A_B，测试 123！"), "ab测试123")
        self.assertEqual(similarity("a b", "ab", compact=True), 1.0)
        self.assertEqual(parse_percent("学习进度 85.5%", default=0, require_symbol=True), 85)
        self.assertEqual(parse_percent("无百分比", default=0, require_symbol=True), 0)
        self.assertEqual(parse_percent("92", default=None, require_symbol=False), 92)


class AnswerParserTests(unittest.TestCase):
    def test_question_type_and_parsers(self):
        self.assertEqual(QUESTION_TYPE_NAMES[QuestionType.SINGLE], "单选题")
        self.assertEqual(answer_separator(QuestionType.FILL), "/")
        self.assertEqual(answer_separator(QuestionType.MULTIPLE), "#@#")
        self.assertEqual(
            extract_answer_block("<think>x</think>```answer\n[1, 2]\n```"),
            "[1, 2]",
        )
        self.assertEqual(parse_answer_ids('```answer\n[{"id": 10}, {"id": 20}]\n```'), [10, 20])
        self.assertEqual(letters_to_indexes("ABD", option_count=4), [0, 1, 3])
        self.assertEqual(parse_vision_answer("<think>hmm</think>AC", 4), ([0, 2], ""))
        self.assertEqual(parse_vision_answer("FILL: 牛顿第二定律"), ([], "牛顿第二定律"))
        self.assertEqual(parse_reference_answer_text("参考答案：A、C"), {"letters": ["A", "C"]})
        ids, fills = parse_homework_answer(
            '```answer\n["B"]\n```',
            {"type": 1, "options": [{"id": "101", "content": "甲"}, {"id": "102", "content": "乙"}]},
        )
        self.assertEqual((ids, fills), (["102"], []))


class AnswerServiceAndExamApiTests(unittest.TestCase):
    def test_exam_api_uses_composition_not_inheritance(self):
        self.assertFalse(issubclass(ExamApi, ZhsApiClient))
        with ExamApi([]) as api:
            self.assertIsInstance(api.client, ZhsApiClient)
            self.assertIs(api.session, api.client.session)

    def test_circuit_breaker_trips_on_non_retryable_llm_error(self):
        breaker = LlmCircuitBreaker()
        calls = []

        def custom_fail():
            calls.append("llm")
            raise NonRetryableLLMError("HTTP 401 API Key 无效", status=401)

        with self.assertRaises(ValueError):
            breaker.run_fallback_chain(
                [
                    ("智慧树AI", lambda: [], False),
                    ("DeepSeek", custom_fail, True),
                ]
            )
        self.assertTrue(breaker.disabled)
        self.assertTrue(breaker.warned)
        # Subsequent call skips custom LLM completely
        with self.assertRaises(ValueError):
            breaker.run_fallback_chain([("DeepSeek", custom_fail, True)])
        self.assertEqual(calls, ["llm"])

    def test_question_bank_helper_delegates_and_closes(self):
        class _FakeBank:
            def __init__(self):
                self.closed = False

            def enabled(self):
                return True

            def query(self, title, options, qtype):
                return f"ans:{title}:{qtype}"

            def close(self):
                self.closed = True

        bank = _FakeBank()
        helper = QuestionBankHelper(bank)
        helper.owns_client = True
        self.assertEqual(helper.query("T", ["A"], 1), "ans:T:1")
        helper.close()
        self.assertTrue(bank.closed)


class QuizDomTests(unittest.IsolatedAsyncioTestCase):
    async def test_target_closed_detection_and_button_helpers(self):
        self.assertTrue(is_target_closed_error(TargetClosedError("closed")))
        self.assertTrue(
            is_target_closed_error(
                PlaywrightError("Target page, context or browser has been closed")
            )
        )
        self.assertFalse(is_target_closed_error(ValueError("other")))

        class _FakePage:
            def __init__(self):
                self.calls = []

            async def evaluate(self, script, arg=None):
                self.calls.append((script, arg))
                return True

        page = _FakePage()
        self.assertTrue(await click_button_by_text(page, ["提交"], allow_substring=False))
        self.assertTrue(await confirm_dialog(page, delay_s=0.0))
        self.assertEqual(len(page.calls), 2)


if __name__ == "__main__":
    unittest.main()
