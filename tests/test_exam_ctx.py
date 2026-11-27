import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import requests

from modules import llm, zhidao_crypto
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
    def test_roundtrip_with_versions_and_sqlite_ssot(self):
        with tempfile.TemporaryDirectory() as directory:
            bank = AnswerBank("course-1", base_dir=directory)
            bank.set(100, 1, {"answer": "1#@#2", "question": "q1"})
            bank.set(100, 2, {"answer": "3", "question": "q2"})
            bank.save("exam-9")

            # SQLite is the single source of truth: no per-question JSON rewrite
            self.assertTrue(os.path.isfile(os.path.join(directory, "questions_bank.db")))
            self.assertFalse(os.path.exists(os.path.join(directory, "data.json")))
            self.assertFalse(os.path.exists(os.path.join(directory, "exam-9.json")))

            loaded = AnswerBank("course-1", base_dir=directory)
            loaded.load("exam-9")
            self.assertEqual(loaded.get(100, 1)["answer"], "1#@#2")
            self.assertEqual(loaded.get(100, 2)["answer"], "3")
            self.assertIsNone(loaded.get(100, 3))

    def test_migrates_legacy_json_once_into_sqlite(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy_path = os.path.join(directory, "exam-old.json")
            with open(legacy_path, "w", encoding="utf-8") as f:
                json.dump(
                    {"200": {"question": "旧版题干", "answer": "9", "version": 1}},
                    f,
                    ensure_ascii=False,
                )
            bank = AnswerBank("course-legacy", base_dir=directory)
            bank.load("exam-old")
            self.assertEqual(bank.get(200, 1)["answer"], "9")


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

    def test_chat_early_stops_after_answer_block_outside_think_and_enforces_deadline(self):
        import time as _time

        class _CountingResponse:
            def __init__(self, lines, delay=0.0):
                self._lines = list(lines)
                self._delay = delay
                self.reads = 0

            def raise_for_status(self):
                pass

            def iter_lines(self):
                for line in self._lines:
                    if self._delay:
                        _time.sleep(self._delay)
                    self.reads += 1
                    yield line

            def close(self):
                pass

        # 1) <think> 内的 decoy 代码块不会触发早停，真正代码块闭合后立即停止
        early_lines = [
            b'data:{"message":"<think>```answer\\n[{\\"id\\": 9}]\\n```</think>","end":false}',
            b'data:{"message":"```answer\\n[{\\"id\\": 2}]\\n```","end":false}',
            b'data:{"message":"TRAILING_SHOULD_NOT_BE_READ","end":false}',
        ]
        resp = _CountingResponse(early_lines)
        content = ZhidaoAnswerer._consume_response(resp, timeout=5.0)
        self.assertIn('"id": 2', content)
        self.assertNotIn("TRAILING_SHOULD_NOT_BE_READ", content)
        self.assertNotIn("<think>", content)
        self.assertEqual(resp.reads, 2)

        # 2) 超时 deadline 防卡死
        slow_lines = [b'data:{"message":"slow","end":false}'] * 20
        slow_resp = _CountingResponse(slow_lines, delay=0.03)
        with self.assertRaisesRegex(RuntimeError, "超时"):
            ZhidaoAnswerer._consume_response(slow_resp, timeout=0.05)


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

    def test_reference_materials_are_trimmed_before_question_and_choices(self):
        huge = "课件重点内容" * 6000
        prompt = build_answer_prompt(
            1,
            "核心题干",
            [{"id": 1, "content": "选项甲"}],
            [{"name": "课件", "content": huge}],
        )
        self.assertLessEqual(len(prompt), 28100)
        self.assertIn("核心题干", prompt)
        self.assertIn("选项甲", prompt)
        self.assertIn("已截断", prompt)

        unlimited = build_answer_prompt(
            1,
            "核心题干",
            [{"id": 1, "content": "选项甲"}],
            [{"name": "课件", "content": "短资料"}],
        )
        self.assertIn("短资料", unlimited)
        self.assertNotIn("已截断", unlimited)

    def test_question_and_choices_are_never_truncated(self):
        long_question = "题干" * 20000
        prompt = build_answer_prompt(
            1,
            long_question,
            [{"id": 1, "content": "选项甲"}],
            [{"name": "课件", "content": "不应出现"}],
        )
        self.assertIn(long_question, prompt)
        self.assertIn("选项甲", prompt)
        self.assertNotIn("不应出现", prompt)

    def test_non_retryable_llm_error_warns_once_and_skips_subsequent_llm(self):
        ctx, _ = self.make_ctx()
        ctx.zhidao_answerer = StubAnswerer(error=RuntimeError("no cookie"))
        calls = []

        class _FailFastAnswerer:
            def answer(self, prompt):
                calls.append(prompt)
                raise llm.NonRetryableLLMError("HTTP 401 API Key 无效")

        ctx.deepseek_answerer = _FailFastAnswerer()
        with patch("modules.exam_ctx.time.sleep"):
            first, source1 = ctx.get_question_answer(make_question())
            second, source2 = ctx.get_question_answer(make_question())
        self.assertEqual(source1, "随机")
        self.assertEqual(source2, "随机")
        self.assertEqual(len(calls), 1)
        self.assertTrue(ctx._llm_disabled)
        self.assertTrue(ctx._llm_warned)

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
        with patch("modules.exam_ctx.time.sleep"):
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

    def test_runner_prefers_high_mastery_score_and_normalizes_free_exam(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        bank = AnswerBank("course-m", base_dir=directory)

        class MasteryApi:
            proxy = ""
            session = None

            def __init__(self):
                self.queried = []
                self.calls_by_kid = {}

            def query_paper(self, course_id, class_id, knowledge_id):
                self.queried.append(knowledge_id)
                n = self.calls_by_kid.get(knowledge_id, 0) + 1
                self.calls_by_kid[knowledge_id] = n
                if knowledge_id == "k-high":
                    # highMasteryScore 优先于 masteryScore
                    return {"examTestId": "e1", "paperId": "p1", "highMasteryScore": 100, "masteryScore": 0}
                if knowledge_id == "k-zero":
                    # highMasteryScore == 0 时不得回退 masteryScore == 100
                    return {"examTestId": "e2", "paperId": "p2", "highMasteryScore": 0 if n == 1 else 100, "masteryScore": 100}
                return {"examTestId": "e3", "paperId": "p3", "masteryScore": 100}

            def list_knowledge_resources(self, *_args):
                return []

        ran = []

        class StubCtx:
            def __init__(self, *args, **kwargs):
                ran.append(kwargs["knowledge_id"])

            def start(self, reference_materials=None):
                return True, 1, 1

        api = MasteryApi()
        runner = ExamRunner("course-m", "class-m", api, target=100, max_attempts=2, bank=bank, enable_ppt=False)
        nodes = [
            {"nodeUid": "k-high", "name": "高掌握度", "freeExam": "0"},
            {"nodeUid": "k-zero", "name": "零掌握度", "freeExam": ""},
            {"nodeUid": "k-free", "name": "免考", "freeExam": "1"},
        ]
        with patch("modules.exam_ctx.ExamCtx", StubCtx), patch("modules.exam_ctx.time.sleep"):
            summary = runner.run(nodes)

        self.assertEqual(summary["done"], ["高掌握度", "零掌握度"])
        self.assertEqual(summary["skipped"], ["免考"])
        self.assertEqual(ran, ["k-zero"])

    def test_homework_ctx_fill_readback_stores_cleaned_text_not_option_ids(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        bank = AnswerBank("course-fill", base_dir=directory)
        api = ExamApi([])
        ctx = ExamCtx(
            api,
            "course-fill",
            "k-fill",
            "t-fill",
            "p-fill",
            op_extra={"courseName": "高等数学", "theme": "微积分", "knowledgePoint": "导数"},
            answer_bank=bank,
        )
        recorded_prompts = []

        class _RecordingAnswerer:
            def answer(self, prompt):
                recorded_prompts.append(prompt)
                return ["导数", "积分"]

        ctx.zhidao_answerer = _RecordingAnswerer()
        question_data = {
            "id": 601,
            "version": 1,
            "questionType": 3,
            "content": "<p>微积分两大核心概念是___和___</p>",
            "optionVos": [
                {"id": 7001, "content": "<p>导数</p>", "isCorrect": 1},
                {"id": 7002, "content": "<p>积分</p>", "isCorrect": 1},
            ],
            "userAnswerVo": {"isCorrect": 1, "answer": "导数/积分"},
        }
        with (
            patch.object(ctx, "open_exam"),
            patch.object(ctx, "get_sheet_content", return_value=[{"questionId": 601, "version": 1}]),
            patch.object(ctx, "get_question_content", return_value=question_data),
            patch.object(ctx, "save_answer", return_value=True),
            patch.object(ctx, "submit_exam"),
        ):
            success, correct, total = ctx.start(
                reference_materials=[{"name": "PPT", "content": "导数与积分互为逆运算"}],
                submit=True,
            )
        self.assertTrue(success)
        self.assertEqual((correct, total), (1, 1))
        self.assertIn("高等数学", recorded_prompts[0])
        self.assertIn("导数与积分互为逆运算", recorded_prompts[0])
        rec = bank.get(601, 1)
        self.assertEqual(rec["answer"], "导数/积分")
        self.assertEqual(rec["answer_content"], "导数\n积分")
        ctx.close()
        api.close()


class Phase2ExamCtxHardeningTests(unittest.TestCase):
    def test_homework_ctx_tracks_failed_saves_and_skips_submit(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        bank = AnswerBank("course-fail", base_dir=directory)
        api = ExamApi([])
        ctx = ExamCtx(api, "course-fail", "k-1", "t-1", "p-1", answer_bank=bank)
        ctx.zhidao_answerer = StubAnswerer(result=[11])
        question_data = {
            "id": 501,
            "questionType": 1,
            "content": "<p>测试题</p>",
            "optionVos": [
                {"id": 11, "content": "A"},
                {"id": 12, "content": "B"},
            ],
        }
        with (
            patch.object(ctx, "open_exam"),
            patch.object(
                ctx,
                "get_sheet_content",
                return_value=[{"questionId": 501, "version": 1}],
            ),
            patch.object(ctx, "get_question_content", return_value=dict(question_data)),
            patch.object(ctx, "save_answer", return_value=False),
            patch.object(ctx, "submit_exam") as submit_mock,
        ):
            success, correct, total = ctx.start(submit=True)
        self.assertFalse(success)
        self.assertEqual(correct, 0)
        self.assertEqual(total, 1)
        self.assertEqual(ctx._failed_saves, 1)
        submit_mock.assert_not_called()
        api.close()

    def test_submit_exam_does_not_retry_recursively_on_error(self):
        calls = []

        class _FailSubmitApi(ExamApi):
            def query(self, url, data, **kwargs):
                calls.append((url, kwargs.get("allow_retry")))
                raise RuntimeError("read timed out")

        api = _FailSubmitApi([])
        ctx = ExamCtx(api, "c1", "k1", "t1", "p1")
        with self.assertRaises(RuntimeError):
            ctx.submit_exam()
        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0][1], False)
        ctx.close()
        api.close()

    def test_open_exam_deduplicates_heartbeat_and_joins_on_close(self):
        class _OkApi:
            session = None
            proxy = ""

            def query(self, url, data, **kwargs):
                return {"code": 0, "data": {}}

        ctx = ExamCtx(_OkApi(), "c1", "k1", "t1", "p1")
        ctx.open_exam()
        first_hb = ctx._heartbeat
        self.assertIsNotNone(first_hb)
        self.assertTrue(first_hb.is_alive())
        # 重复调用 open_exam 不应拉起第二个心跳线程
        ctx.open_exam()
        self.assertIs(ctx._heartbeat, first_hb)
        ctx.close()
        self.assertFalse(first_hb.is_alive())

    def test_ai_exam_base_gives_zhidao_answerer_isolated_session(self):
        with ExamApi([{"name": "CASTGC", "value": "TGT-iso", "domain": ".zhihuishu.com"}]) as api:
            with ExamCtx(api, "c1", "k1", "t1", "p1") as ctx:
                self.assertIsNot(ctx.zhidao_answerer.session, api.session)
                self.assertIsNone(ctx.zhidao_answerer.lock)
                self.assertEqual(
                    ctx.zhidao_answerer.session.cookies.get("CASTGC"), "TGT-iso"
                )


if __name__ == "__main__":
    unittest.main()