import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import requests

from modules import zhidao_crypto
from modules.exam_ctx import (
    KNOWLEDGE_RESOURCE_LIST,
    AnswerBank,
    ExamApi,
    ExamApiError,
    ExamCtx,
    ExamRunner,
    ZhidaoAnswerer,
    build_answer_prompt,
    parse_answer_ids,
    parse_review_exam_url,
    select_random_answers,
)


class ParseAnswerTests(unittest.TestCase):
    def test_parses_answer_code_block(self):
        text = '```answer\n[{"id": 11, "content": "A"}, {"id": 12, "content": "B"}]\n```'
        self.assertEqual(parse_answer_ids(text), [11, 12])

    def test_parses_bare_list_and_explained_list(self):
        self.assertEqual(parse_answer_ids("[1, 2, 3]"), [1, 2, 3])
        self.assertEqual(
            parse_answer_ids('依据是 [{"id": 7, "content": "C"}]'),
            [7],
        )

    def test_returns_empty_for_invalid_text(self):
        self.assertEqual(parse_answer_ids("没有答案"), [])
        self.assertEqual(parse_answer_ids(None), [])

    def test_random_answers_stay_within_choices(self):
        choices = [{"id": index} for index in range(1, 5)]
        picked = select_random_answers(choices, 2)
        self.assertEqual(len(picked), 2)
        self.assertTrue(set(picked).issubset({1, 2, 3, 4}))


class ReviewExamUrlTests(unittest.TestCase):
    def test_extracts_exam_ids_from_review_page(self):
        url = (
            "https://studentexamcomh5.zhihuishu.com/studentReviewTestOrExam/"
            "100/0/0/200/%E5%AF%BC%E8%AE%BA/?paperId=300&nodeName=x"
        )
        self.assertEqual(
            parse_review_exam_url(url),
            {
                "examTestId": "100",
                "courseId": "200",
                "nodeName": "导论",
                "paperId": "300",
            },
        )

    def test_ignores_other_pages_and_missing_paper(self):
        self.assertEqual(parse_review_exam_url("https://example.com/x"), {})
        self.assertEqual(
            parse_review_exam_url(
                "https://studentexamcomh5.zhihuishu.com/"
                "studentReviewTestOrExam/100/0/0/200/node/"
            ),
            {},
        )


class AnswerBankTests(unittest.TestCase):
    def test_roundtrip_with_versions_and_merge_file(self):
        with tempfile.TemporaryDirectory() as directory:
            bank = AnswerBank("course-1", base_dir=directory)
            bank.set(100, 1, {"answer": "1#@#2", "question": "q1"})
            bank.set(100, 2, {"answer": "3", "question": "q2"})
            bank.save("exam-9")

            loaded = AnswerBank("course-1", base_dir=directory)
            loaded.load("exam-9")
            self.assertEqual(loaded.get(100, 1)["answer"], "1#@#2")
            self.assertEqual(loaded.get(100, 2)["answer"], "3")
            self.assertIsNone(loaded.get(100, 3))

            for name in ("data.json", "exam-9.json"):
                path = os.path.join(directory, name)
                self.assertTrue(os.path.isfile(path))
                with open(path, encoding="utf-8") as file:
                    self.assertNotIn("version", json.load(file))


class FakeResponse:
    def __init__(self, payload, *, lines=None):
        self.payload = payload
        self.text = json.dumps(payload) if payload is not None else ""
        self._lines = lines
        self.closed = False

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload

    def iter_lines(self):
        if self._lines is not None:
            yield from self._lines
            return
        for line in self.text.splitlines():
            yield line.encode("utf-8")

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, payload=None, *, lines=None):
        self.payload = payload
        self.lines = lines
        self.calls = []

    def post(self, url, data=None, json=None, headers=None, timeout=None, stream=False):
        self.calls.append(
            {
                "url": url,
                "data": data,
                "json": json,
                "headers": headers,
                "stream": stream,
            }
        )
        return FakeResponse(self.payload, lines=self.lines)


class ZhidaoAnswererTests(unittest.TestCase):
    def test_sign_zhidao_ai_uses_sort_ascii_md5(self):
        import hashlib

        url, body = zhidao_crypto.sign_zhidao_ai(
            "https://dmb-chat.zhihuishu.com/gateway/t/v1/completions/chat/stream",
            {
                "terminalType": 1,
                "question": "1+1?",
                "relationType": 1,
                "timestamp": 1700000000000,
                "empty": "",
                "missing": None,
            },
        )
        expected_input = (
            "8ZflKEagfLquestion=1+1?&relationType=1&terminalType=1&timestamp=1700000000000"
        )
        expected_sig = hashlib.md5(expected_input.encode("utf-8")).hexdigest()
        self.assertIn(f"sign={expected_sig}", url)
        self.assertEqual(body["question"], "1+1?")

    def test_chat_parses_dmb_sse_stream_and_sends_required_headers(self):
        sse_lines = [
            b'data:{"messageType":"TEXT","messageSource":1,"end":false,"context":{}}',
            b'data:{"messageType":"TEXT","message":"```answer\\n","end":false}',
            b'data:{"messageType":"TEXT","message":"[{\\"id\\": 2, \\"content\\": \\"B\\"}]\\n```","end":false}',
            (
                b'data:{"messageType":"TEXT","end":true,'
                b'"context":{"answer":"```answer\\n[{\\"id\\": 2, \\"content\\": \\"B\\"}]\\n```"}}'
            ),
        ]
        session = FakeSession(lines=sse_lines)
        answerer = ZhidaoAnswerer(session)
        reply = answerer.chat("prompt")
        self.assertIn("```answer", reply)
        self.assertEqual(answerer.answer("prompt"), [2])
        call = session.calls[0]
        self.assertIn("dmb-chat.zhihuishu.com/gateway/t/v1/completions/chat/stream", call["url"])
        self.assertIn("sign=", call["url"])
        self.assertEqual(call["headers"]["service-code"], "SI_POLYMAS")
        self.assertEqual(call["headers"]["Accept"], "text/event-stream")
        self.assertEqual(call["json"]["question"], "prompt")
        self.assertEqual(call["json"]["terminalType"], 1)
        self.assertEqual(call["json"]["relationType"], 1)
        self.assertIsInstance(call["json"]["timestamp"], int)

    def test_chat_raises_on_api_error_or_empty_response(self):
        err_answerer = ZhidaoAnswerer(
            FakeSession({"code": "509", "message": "请求操作执行异常", "data": None})
        )
        with self.assertRaises(ExamApiError):
            err_answerer.chat("prompt")

        empty_answerer = ZhidaoAnswerer(FakeSession(lines=[]))
        with self.assertRaises(ValueError):
            empty_answerer.chat("prompt")

    def test_chat_remains_compatible_with_choices_payload(self):
        payload = {
            "choices": [
                {"message": {"content": '```answer\n[{"id": 2, "content": "B"}]\n```'}}
            ]
        }
        answerer = ZhidaoAnswerer(FakeSession(payload))
        self.assertIn("```answer", answerer.chat("prompt"))
        self.assertEqual(answerer.answer("prompt"), [2])


class ExamApiTests(unittest.TestCase):
    def test_query_encrypts_payload_and_checks_code(self):
        api = ExamApi([{"name": "CAS", "value": "token"}])
        fake = FakeSession({"code": 0, "data": {"ok": True}})
        api.session = fake
        ret = api.query("https://example.com/api", {"courseId": 123})
        self.assertTrue(ret["data"]["ok"])
        form = fake.calls[0]["data"]
        payload = json.loads(
            zhidao_crypto.decrypt(form["secretStr"], key=zhidao_crypto.EXAM_KEY)
        )
        self.assertEqual(payload["courseId"], 123)
        self.assertEqual(payload["dateFormate"], form["dateFormate"])

    def test_query_raises_on_unexpected_code(self):
        api = ExamApi([])
        api.session = FakeSession({"code": -12, "message": "captcha"})
        with self.assertRaises(ExamApiError):
            api.query("https://example.com/api", {"courseId": 123})

    def test_list_knowledge_resources_uses_ai_task_strategy(self):
        api = ExamApi([])
        calls = {}

        def fake_query(strategy, url, data, method="POST", **kwargs):
            calls.update(strategy=strategy, url=url, data=data, method=method)
            return {"code": 200, "data": {"resourceList": [{"id": 1}]}}

        api.query_strategy = fake_query
        resources = api.list_knowledge_resources("c-1", "cl-1", "k-1")
        self.assertEqual(resources, [{"id": 1}])
        self.assertEqual(calls["strategy"], "ai_task")
        self.assertEqual(calls["url"], KNOWLEDGE_RESOURCE_LIST)
        self.assertEqual(
            calls["data"],
            {"courseId": "c-1", "classId": "cl-1", "knowledgeId": "k-1"},
        )

    def test_list_knowledge_resources_returns_empty_on_failure(self):
        api = ExamApi([])

        def boom(*args, **kwargs):
            raise RuntimeError("network")

        api.query_strategy = boom
        self.assertEqual(api.list_knowledge_resources("c", "cl", "k"), [])


class StubAnswerer:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = 0

    def answer(self, prompt):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


class FakeApi:
    proxy = ""

    def __init__(self):
        self.session = requests.Session()


def make_question(question_type=1):
    return {
        "id": 501,
        "questionType": question_type,
        "content": "题干",
        "optionVos": [
            {"id": 1, "content": "A"},
            {"id": 2, "content": "B"},
            {"id": 3, "content": "C"},
        ],
    }


class ExamAnswerTests(unittest.TestCase):
    def make_ctx(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        bank = AnswerBank("course-1", base_dir=directory)
        ctx = ExamCtx(
            api=FakeApi(),
            course_id="course-1",
            knowledge_id="k1",
            exam_test_id="exam-1",
            exam_paper_id="paper-1",
            answer_bank=bank,
        )
        return ctx, directory

    def test_uses_cached_answer(self):
        ctx, _ = self.make_ctx()
        ctx.answer_bank.set(501, 1, {"answer": "2#@#3"})
        ctx.zhidao_answerer = StubAnswerer(error=AssertionError("should not call"))
        with patch("modules.exam_ctx.time.sleep"):
            answers, source = ctx.get_question_answer(make_question(2))
        self.assertEqual(answers, ["2", "3"])
        self.assertEqual(source, "题库")

    def test_reference_materials_are_injected_into_prompt(self):
        ctx, _ = self.make_ctx()
        ctx.reference_materials = [{"name": "课件", "content": "重点内容"}]
        prompts = []

        class CapturingAnswerer:
            def answer(self, prompt):
                prompts.append(prompt)
                return [2]

        ctx.zhidao_answerer = CapturingAnswerer()
        ctx.deepseek_answerer = StubAnswerer(error=AssertionError("should not fallback"))
        answers, source = ctx.get_question_answer(make_question(1))
        self.assertEqual(answers, [2])
        self.assertEqual(source, "AI")
        self.assertIn("参考资料", prompts[0])
        self.assertIn("重点内容", prompts[0])

        standalone = build_answer_prompt(
            1, "题干", [{"id": 2, "content": "B"}], [{"name": "讲义", "content": "考点"}]
        )
        self.assertIn("讲义", standalone)
        self.assertIn("考点", standalone)

    def test_prefers_zhidao_ai_then_deepseek(self):
        ctx, _ = self.make_ctx()
        zhidao = StubAnswerer(result=[2])
        deepseek = StubAnswerer(error=AssertionError("should not call"))
        ctx.zhidao_answerer = zhidao
        ctx.deepseek_answerer = deepseek
        answers, source = ctx.get_question_answer(make_question())
        self.assertEqual(answers, [2])
        self.assertEqual(source, "AI")
        self.assertEqual(zhidao.calls, 1)
        self.assertEqual(deepseek.calls, 0)

        zhidao = StubAnswerer(error=RuntimeError("no cookie"))
        deepseek = StubAnswerer(result=[3])
        ctx.zhidao_answerer = zhidao
        ctx.deepseek_answerer = deepseek
        answers, source = ctx.get_question_answer(make_question())
        self.assertEqual(answers, [3])
        self.assertEqual(source, "AI")
        self.assertEqual(deepseek.calls, 1)

    def test_falls_back_to_random_when_both_ai_fail(self):
        ctx, _ = self.make_ctx()
        ctx.zhidao_answerer = StubAnswerer(error=RuntimeError("fail"))
        ctx.deepseek_answerer = StubAnswerer(error=RuntimeError("fail"))
        for question_type, expected in ((1, 1), (2, 2)):
            answers, source = ctx.get_question_answer(make_question(question_type))
            self.assertEqual(source, "随机")
            self.assertEqual(len(answers), expected)
            self.assertTrue(set(answers).issubset({1, 2, 3}))

    def test_ignores_stale_cache_ids(self):
        ctx, _ = self.make_ctx()
        ctx.answer_bank.set(501, 1, {"answer": "99"})
        ctx.zhidao_answerer = StubAnswerer(result=[1])
        ctx.deepseek_answerer = StubAnswerer(error=RuntimeError("fail"))
        answers, source = ctx.get_question_answer(make_question())
        self.assertEqual(answers, [1])
        self.assertEqual(source, "AI")

    def test_start_logs_each_answer_and_final_score(self):
        ctx, _ = self.make_ctx()
        ctx.zhidao_answerer = StubAnswerer(result=[2])
        ctx.deepseek_answerer = StubAnswerer(error=AssertionError("should not call"))

        infos = []
        events = []

        class _CaptureLogger:
            def info(self, msg, shift=False):
                infos.append(msg)

            def warn(self, msg, shift=False):
                pass

            def error(self, msg, shift=False):
                pass

            def debug(self, msg):
                pass

            def event(self, name, interval=None, **fields):
                events.append((name, fields))

            @staticmethod
            def summarize_exception(exc):
                return f"{type(exc).__name__}: {exc}"

        question_data = {
            "id": 501,
            "questionType": 1,
            "content": "<p>测试题干</p>",
            "optionVos": [
                {"id": 1, "content": "<p>选项A</p>", "isCorrect": 0},
                {"id": 2, "content": "<p>选项B</p>", "isCorrect": 1},
            ],
            "userAnswerVo": {"isCorrect": 1},
        }
        with (
            patch("modules.exam_ctx.logger", _CaptureLogger()),
            patch.object(ctx, "open_exam"),
            patch.object(ctx, "get_sheet_content", return_value=[{"questionId": 501, "version": 1}]),
            patch.object(ctx, "get_question_content", return_value=dict(question_data)),
            patch.object(ctx, "save_answer", return_value=True),
            patch.object(ctx, "submit_exam"),
        ):
            success, correct, total = ctx.start()

        self.assertTrue(success)
        self.assertEqual((correct, total), (1, 1))
        self.assertTrue(any("接口答题 1/1" in m and "选项B" in m for m in infos))
        self.assertTrue(any("得分 1/1" in m for m in infos))
        answer_events = [fields for name, fields in events if name == "接口答题"]
        self.assertEqual(len(answer_events), 1)
        self.assertEqual(answer_events[0]["方式"], "AI:智慧树AI")
        self.assertIn("选项B", answer_events[0]["答案"])


class StubExtractor:
    def __init__(self):
        self.calls = []

    def collect(self, resources, course_name=""):
        self.calls.append((list(resources), course_name))
        return [{"name": "课件", "content": "重点内容"}]


class RunnerResourceApi:
    proxy = ""
    session = None

    def __init__(self):
        self.paper_calls = 0
        self.resource_calls = []

    def query_paper(self, course_id, class_id, knowledge_id):
        self.paper_calls += 1
        return {
            "examTestId": "exam-r",
            "paperId": "paper-r",
            "masteryScore": 0 if self.paper_calls == 1 else 100,
        }

    def list_knowledge_resources(self, course_id, class_id, knowledge_id):
        self.resource_calls.append((course_id, class_id, knowledge_id))
        return [
            {
                "resourcesDetail": {
                    "resourcesType": 1,
                    "resourcesDistributeType": 4,
                    "resourcesUrl": "https://example.com/lesson.pptx",
                }
            }
        ]


class ExamRunnerReferenceTests(unittest.TestCase):
    def test_runner_collects_and_passes_reference_materials(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        bank = AnswerBank("course-1", base_dir=directory)
        api = RunnerResourceApi()
        runner = ExamRunner(
            "course-1",
            "class-1",
            api,
            target=100,
            max_attempts=2,
            op_extra={"courseName": "测试课程"},
            bank=bank,
        )
        extractor = StubExtractor()
        runner.extractor = extractor
        recorded = {}

        class RecordingCtx:
            def __init__(self, *args, **kwargs):
                recorded["args"] = (args, kwargs)

            def start(self, reference_materials=None):
                recorded["materials"] = reference_materials
                return True, 1, 1

        with (
            patch("modules.exam_ctx.ExamCtx", RecordingCtx),
            patch("modules.exam_ctx.time.sleep"),
        ):
            result = runner._run_node("k-1", "知识点", {}, None)

        self.assertEqual(result, "done")
        self.assertEqual(
            recorded["materials"], [{"name": "课件", "content": "重点内容"}]
        )
        self.assertEqual(len(extractor.calls), 1)
        self.assertEqual(api.resource_calls, [("course-1", "class-1", "k-1")])
        self.assertEqual(extractor.calls[0][1], "测试课程")


if __name__ == "__main__":
    unittest.main()