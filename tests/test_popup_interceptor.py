import asyncio
import unittest

from modules.popup_interceptor import (
    POPUP_ANSWER_ENDPOINT,
    PopupAnswerInterceptor,
    normalize_option_text,
    strip_option_prefix,
)


def make_question(question_id, options, title=""):
    return {
        "testQuestion": {
            "questionId": question_id,
            "name": title,
            "questionOptions": [
                {"content": content, "result": result}
                for content, result in options
            ],
        }
    }


def make_payload(question_id, options, title=""):
    return {
        "code": 0,
        "data": {
            "lessonTestQuestionUseInterfaceDtos": [
                make_question(question_id, options, title)
            ]
        },
    }


def make_multi_payload(*questions):
    return {
        "code": 0,
        "data": {"lessonTestQuestionUseInterfaceDtos": list(questions)},
    }


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakePage:
    def __init__(self):
        self.listeners = {}

    def on(self, event, handler):
        self.listeners.setdefault(event, []).append(handler)

    def remove_listener(self, event, handler):
        self.listeners.setdefault(event, []).remove(handler)


class FakeResponse:
    def __init__(self, url, payload=None, error=None):
        self.url = url
        self._payload = payload
        self._error = error

    async def json(self):
        if self._error is not None:
            raise self._error
        return self._payload


class NormalizeTests(unittest.TestCase):
    def test_html_and_prefix_are_cleaned(self):
        self.assertEqual(
            normalize_option_text("<p> A. 社会主义初级阶段 </p>"),
            "A. 社会主义初级阶段",
        )
        self.assertEqual(strip_option_prefix("A. 社会主义初级阶段"), "社会主义初级阶段")
        self.assertEqual(strip_option_prefix("(B) 改革开放"), "改革开放")
        self.assertEqual(strip_option_prefix("1、第一项"), "第一项")
        self.assertEqual(strip_option_prefix("1. 第一项"), "第一项")
        self.assertEqual(strip_option_prefix("无序号选项"), "无序号选项")

    def test_decimal_values_are_not_stripped_as_option_prefix(self):
        self.assertEqual(strip_option_prefix("1.5"), "1.5")
        self.assertEqual(strip_option_prefix("2.5"), "2.5")
        self.assertEqual(strip_option_prefix("1.25 千克"), "1.25 千克")


class ExtractionTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.interceptor = PopupAnswerInterceptor(FakePage(), clock=self.clock)

    def test_single_choice_result_flag(self):
        self.interceptor.ingest(
            make_payload(
                11,
                [
                    ("<p>A. 错误选项</p>", 0),
                    ("<p>B. 正确选项</p>", "1"),
                ],
            )
        )
        self.assertTrue(self.interceptor.is_correct("<p>B. 正确选项</p>"))
        self.assertTrue(self.interceptor.is_correct("正确选项"))
        self.assertTrue(self.interceptor.is_correct("B. 正确选项"))
        self.assertFalse(self.interceptor.is_correct("错误选项"))

    def test_multiple_correct_options(self):
        self.interceptor.ingest(
            make_payload(
                12,
                [
                    ("甲", "1"),
                    ("乙", "1"),
                    ("丙", 0),
                ],
            )
        )
        self.assertTrue(self.interceptor.is_correct("甲"))
        self.assertTrue(self.interceptor.is_correct("乙"))
        self.assertFalse(self.interceptor.is_correct("丙"))
        self.assertEqual(self.interceptor.correct_texts(), ["乙", "甲"])

    def test_question_scoped_lookup(self):
        self.interceptor.ingest(make_payload(21, [("甲", 1)]))
        self.interceptor.ingest(make_payload(22, [("乙", 1)]))
        self.assertTrue(self.interceptor.is_correct("甲", question_id=21))
        self.assertFalse(self.interceptor.is_correct("甲", question_id=22))
        self.assertTrue(self.interceptor.is_correct("乙", question_id=22))

    def test_missing_result_field_is_ignored_for_llm_fallback(self):
        self.interceptor.ingest(
            make_payload(31, [("没有标注的选项", None), ("另一个选项", "")])
        )
        self.assertEqual(self.interceptor.correct_texts(), [])
        self.assertFalse(self.interceptor.is_correct("没有标注的选项"))
        self.assertEqual(self.interceptor.ingest({"data": {}}), 0)
        self.assertEqual(self.interceptor.ingest(None), 0)

    def test_option_content_fallback_keys(self):
        payload = {
            "data": {
                "lessonTestQuestionUseInterfaceDtos": [
                    {
                        "questionId": 41,
                        "questionOptions": [
                            {"optionContent": "<p>回退字段</p>", "result": 1}
                        ],
                    }
                ]
            }
        }
        self.assertEqual(self.interceptor.ingest(payload), 1)
        self.assertTrue(self.interceptor.is_correct("回退字段", question_id=41))

    def test_two_judgement_questions_are_isolated_by_title(self):
        self.interceptor.ingest(
            make_multi_payload(
                make_question(71, [("正确", 1), ("错误", 0)], "地球是圆的"),
                make_question(72, [("正确", 0), ("错误", 1)], "太阳从西边升起"),
            )
        )
        dom = ["A. 正确", "B. 错误"]
        self.assertEqual(
            self.interceptor.match_choices(dom, question_title="地球是圆的"), [0]
        )
        self.assertEqual(
            self.interceptor.match_choices(dom, question_title="太阳从西边升起"), [1]
        )
        self.assertEqual(
            self.interceptor.match_choices(dom, question_id="72"), [1]
        )
        self.assertEqual(self.interceptor.match_choices(dom, question_id="71"), [0])

    def test_ambiguous_option_subset_falls_back_without_cross_pollution(self):
        self.interceptor.ingest(
            make_multi_payload(
                make_question(73, [("正确", 1), ("错误", 0)], "判断题一"),
                make_question(74, [("正确", 0), ("错误", 1)], "判断题二"),
            )
        )
        self.assertEqual(self.interceptor.match_choices(["正确", "错误"]), [])
        self.assertEqual(self.interceptor.match_choices([]), [])

    def test_single_question_matches_by_option_subset(self):
        self.interceptor.ingest(
            make_payload(75, [("甲", 1), ("乙", 0), ("丙", 0)], "任意题干")
        )
        self.assertEqual(self.interceptor.match_choices(["A. 甲", "B. 乙"]), [0])

    def test_decimal_options_keep_their_value(self):
        self.interceptor.ingest(
            make_payload(76, [("1.5", 0), ("2.5", 1)], "计算题")
        )
        self.assertEqual(
            self.interceptor.match_choices(["1.5", "2.5"], question_title="计算题"),
            [1],
        )

    def test_ttl_expiry_clears_cache(self):
        self.interceptor = PopupAnswerInterceptor(
            FakePage(), ttl=10, clock=self.clock
        )
        self.interceptor.ingest(make_payload(51, [("会过期", 1)]))
        self.assertTrue(self.interceptor.is_correct("会过期"))
        self.clock.advance(11)
        self.assertFalse(self.interceptor.is_correct("会过期"))
        self.assertEqual(self.interceptor.correct_texts(), [])


class LifecycleTests(unittest.TestCase):
    def test_attach_and_detach_remove_same_listener(self):
        page = FakePage()
        interceptor = PopupAnswerInterceptor(page)
        interceptor.attach()
        interceptor.attach()
        self.assertEqual(len(page.listeners["response"]), 1)
        self.assertTrue(interceptor.attached)
        interceptor.detach()
        interceptor.detach()
        self.assertEqual(page.listeners["response"], [])
        self.assertFalse(interceptor.attached)

    def test_other_endpoints_are_ignored(self):
        interceptor = PopupAnswerInterceptor(FakePage())
        called = []
        interceptor._consume = lambda response: called.append(response)
        interceptor._on_response(FakeResponse("https://example.com/other"))
        self.assertEqual(called, [])


class PendingTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_response_task_is_tracked_then_consumed(self):
        interceptor = PopupAnswerInterceptor(FakePage())
        response = FakeResponse(
            "https://studyservice.zhihuishu.com/popupAnswer/lessonPopupExam",
            make_payload(81, [("弹题答案", 1)]),
        )
        interceptor._on_response(response)
        self.assertEqual(len(interceptor._pending_tasks), 1)
        for _ in range(3):
            await asyncio.sleep(0)
        self.assertEqual(interceptor._pending_tasks, set())
        self.assertTrue(interceptor.is_correct("弹题答案"))

    async def test_detach_cancels_pending_tasks(self):
        page = FakePage()
        interceptor = PopupAnswerInterceptor(page)
        interceptor.attach()
        response = FakeResponse(
            "https://studyservice.zhihuishu.com/popupAnswer/lessonPopupExam",
            make_payload(82, [("延迟答案", 1)]),
        )
        interceptor._on_response(response)
        self.assertEqual(len(interceptor._pending_tasks), 1)
        interceptor.detach()
        self.assertEqual(interceptor._pending_tasks, set())
        for _ in range(3):
            await asyncio.sleep(0)


class ResponseConsumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_response_json_is_parsed(self):
        interceptor = PopupAnswerInterceptor(FakePage())
        response = FakeResponse(
            "https://studyservice.zhihuishu.com/popupAnswer/lessonPopupExam",
            make_payload(61, [("正确答案", 1)]),
        )
        await interceptor._consume(response)
        self.assertTrue(interceptor.is_correct("正确答案"))

    async def test_broken_json_degrades_silently(self):
        interceptor = PopupAnswerInterceptor(FakePage())
        response = FakeResponse(
            "https://studyservice.zhihuishu.com/popupAnswer/lessonPopupExam",
            error=ValueError("not json"),
        )
        await interceptor._consume(response)
        self.assertEqual(interceptor.correct_texts(), [])
        self.assertFalse(interceptor.is_correct("任意选项"))


if __name__ == "__main__":
    unittest.main()