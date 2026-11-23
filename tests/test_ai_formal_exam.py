import json
import shutil
import tempfile
import unittest
from unittest.mock import patch

import requests

from modules.exam_ctx import (
    EXAM_OPEN,
    EXAM_QUESTION,
    EXAM_SUBMIT,
    FORMAL_ANSWER_SHEET,
    FORMAL_OPEN_DETAIL,
    FORMAL_SAVE_BATCH,
    FORMAL_START_PULL,
    FORMAL_TASK_LIST,
    AiFormalExamCtx,
    AnswerBank,
    ExamCtx,
)


def make_content(question_id, question_type=1):
    return {
        "id": question_id,
        "questionType": question_type,
        "content": f"题干 {question_id}",
        "optionVos": [
            {"id": 1, "content": "<p>选项A</p>", "isCorrect": 0},
            {"id": 2, "content": "<p>选项B</p>", "isCorrect": 1},
            {"id": 3, "content": "<p>选项C</p>", "isCorrect": 0},
        ],
    }


class FakeFormalApi:
    proxy = ""

    def __init__(self, parts, contents, detail=None, tasks=None):
        self.session = requests.Session()
        self.parts = parts
        self.contents = contents
        self.detail = detail or {}
        self.tasks = tasks or []
        self.query_calls = []
        self.strategy_calls = []
        self.batch_payloads = []
        self.fail_batch = False

    def query(
        self,
        url,
        data,
        key=None,
        ok_code=0,
        method="POST",
        content_type="form",
        set_timestamp=True,
        extra_fields=None,
    ):
        self.query_calls.append(url)
        if "getExamQuestionInfo" in url:
            question_id = data["questionId"]
            return {"code": 0, "data": dict(self.contents[question_id])}
        return {"code": 0, "data": {}}

    def query_strategy(self, strategy, url, data, method="POST", **kwargs):
        self.strategy_calls.append(
            {"strategy": strategy, "url": url, "data": data, "method": method}
        )
        if url == FORMAL_START_PULL:
            return {"code": 200, "data": {}}
        if url == FORMAL_ANSWER_SHEET:
            return {"code": 0, "data": {"partSheetVos": self.parts}}
        if url == FORMAL_SAVE_BATCH:
            self.batch_payloads.append(data)
            if self.fail_batch:
                raise RuntimeError("batch save failed")
            return {"code": 0}
        if url == FORMAL_OPEN_DETAIL:
            return {"code": 200, "data": dict(self.detail)}
        if url == FORMAL_TASK_LIST:
            return {"code": 200, "data": {"taskList": self.tasks}}
        if url == EXAM_SUBMIT:
            return {}
        if url == EXAM_OPEN:
            return {"code": 0, "data": {}}
        return {"code": 200, "data": {}}

    def urls(self, strategy=None):
        return [
            call["url"]
            for call in self.strategy_calls
            if strategy is None or call["strategy"] == strategy
        ]


class StubAnswerer:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = 0

    def answer(self, prompt):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return list(self.result)


class FormalExamTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.bank = AnswerBank("course-1", base_dir=self.directory)

    def make_parts(self, question_ids_per_part, question_type=1):
        parts = []
        for ids in question_ids_per_part:
            parts.append(
                {
                    "questionSheetVos": [
                        {"questionId": question_id, "version": 1}
                        for question_id in ids
                    ]
                }
            )
        return parts

    def make_ctx(self, parts, contents, *, detail=None, save_nums=5, auto_submit=True):
        api = FakeFormalApi(parts, contents, detail=detail)
        ctx = AiFormalExamCtx(
            api,
            "course-1",
            exam_test_id="exam-1",
            exam_paper_id="paper-1",
            answer_bank=self.bank,
            class_id="class-1",
            task_id="task-1",
            student_id="stu-1",
            save_nums=save_nums,
            auto_submit=auto_submit,
        )
        ctx.zhidao_answerer = StubAnswerer(result=[2])
        ctx.deepseek_answerer = StubAnswerer(error=AssertionError("should not fallback"))
        return ctx, api


class FormalExamFlowTests(FormalExamTestCase):
    def test_start_pull_paper_then_open_without_paper_id_and_batches(self):
        ids = list(range(1001, 1013))
        parts = self.make_parts([ids[:7], ids[7:]])
        contents = {question_id: make_content(question_id) for question_id in ids}
        ctx, api = self.make_ctx(parts, contents, save_nums=5, auto_submit=True)

        with patch("modules.exam_ctx.time.sleep"):
            success, answered, total = ctx.start(submit=True)

        self.assertTrue(success)
        self.assertEqual((answered, total), (12, 12))
        self.assertEqual(api.urls()[:2], [FORMAL_START_PULL, EXAM_OPEN])
        # 多份 partSheetVos 必须全部展开
        self.assertEqual(
            [len(payload["answerList"]) for payload in api.batch_payloads],
            [5, 5, 2],
        )
        for payload in api.batch_payloads:
            self.assertEqual(payload["examTestId"], "exam-1")
            self.assertEqual(payload["examPaperId"], "paper-1")
            self.assertEqual(payload["recruitId"], "course-1")
            for item in payload["answerList"]:
                self.assertEqual(item["answer"], "2")
                self.assertEqual(item["questionType"], 1)
                self.assertIsNone(item["dataVos"])

        open_call = next(
            call for call in api.strategy_calls if call["url"] == EXAM_OPEN
        )
        self.assertEqual(open_call["strategy"], "ai_exam")
        self.assertNotIn("examPaperId", open_call["data"])
        self.assertEqual(
            open_call["data"], {"examTestId": "exam-1", "courseId": "course-1"}
        )
        submit_call = next(
            call for call in api.strategy_calls if call["url"] == EXAM_SUBMIT
        )
        self.assertEqual(submit_call["strategy"], "ai_exam_submit")
        self.assertEqual(
            submit_call["data"],
            {
                "examTestId": "exam-1",
                "recruitId": "course-1",
                "examPaperId": "paper-1",
            },
        )
        pull_call = next(
            call for call in api.strategy_calls if call["url"] == FORMAL_START_PULL
        )
        self.assertEqual(pull_call["strategy"], "ai_task")
        self.assertEqual(
            pull_call["data"],
            {
                "courseId": "course-1",
                "classId": "class-1",
                "examTestId": "exam-1",
                "pageNumber": 1,
                "pageSize": 10,
            },
        )

    def test_multi_choice_answer_uses_separator(self):
        parts = self.make_parts([[2001]])
        contents = {2001: make_content(2001, question_type=2)}
        ctx, api = self.make_ctx(parts, contents)
        ctx.zhidao_answerer = StubAnswerer(result=[2, 3])

        with patch("modules.exam_ctx.time.sleep"):
            ctx.start(submit=False)

        self.assertEqual(
            api.batch_payloads[0]["answerList"][0]["answer"], "2#@#3"
        )
        self.assertEqual(api.batch_payloads[0]["answerList"][0]["questionType"], 2)

    def test_fill_question_uses_slash_separator(self):
        parts = self.make_parts([[4001]])
        contents = {
            4001: {
                "id": 4001,
                "questionType": 3,
                "content": "请填写两个空",
                "optionVos": [],
            }
        }
        ctx, api = self.make_ctx(parts, contents)
        ctx.zhidao_answerer = StubAnswerer(result=["甲", "乙"])

        with patch("modules.exam_ctx.time.sleep"):
            success, answered, total = ctx.start(submit=False)

        self.assertFalse(success)
        self.assertEqual((answered, total), (1, 1))
        item = api.batch_payloads[0]["answerList"][0]
        self.assertEqual(item["answer"], "甲/乙")
        self.assertEqual(item["questionType"], 3)

    def test_batch_save_failure_keeps_answers_out_and_skips_submit(self):
        parts = self.make_parts([[7001, 7002]])
        contents = {
            question_id: make_content(question_id) for question_id in (7001, 7002)
        }
        ctx, api = self.make_ctx(parts, contents, save_nums=2, auto_submit=True)
        api.fail_batch = True

        with patch("modules.exam_ctx.time.sleep"):
            success, answered, total = ctx.start(submit=True)

        self.assertFalse(success)
        self.assertEqual(answered, 0)
        self.assertEqual(total, 2)
        self.assertEqual(len(api.batch_payloads), 1)
        self.assertEqual(ctx._failed_saves, 2)
        self.assertNotIn(EXAM_SUBMIT, api.urls())
        self.assertEqual(ctx._answered, {})

    def test_submit_false_skips_submit_and_detail(self):
        parts = self.make_parts([[3001]])
        contents = {3001: make_content(3001)}
        ctx, api = self.make_ctx(parts, contents, auto_submit=True)

        with patch("modules.exam_ctx.time.sleep"):
            success, answered, total = ctx.start(submit=False)

        self.assertFalse(success)
        self.assertEqual((answered, total), (1, 1))
        self.assertEqual(api.batch_payloads[0]["answerList"][0]["answer"], "2")
        self.assertNotIn(EXAM_SUBMIT, api.urls())
        self.assertNotIn(FORMAL_OPEN_DETAIL, api.urls())

    def test_auto_submit_setting_is_default_when_submit_is_none(self):
        parts = self.make_parts([[3101]])
        contents = {3101: make_content(3101)}
        ctx, api = self.make_ctx(parts, contents, auto_submit=False)

        with patch("modules.exam_ctx.time.sleep"):
            ctx.start()

        self.assertNotIn(EXAM_SUBMIT, api.urls())

        ctx2, api2 = self.make_ctx(parts, contents, auto_submit=True)
        with patch("modules.exam_ctx.time.sleep"):
            ctx2.start()
        self.assertIn(EXAM_SUBMIT, api2.urls())


class FormalExamCacheTests(FormalExamTestCase):
    def test_cross_exam_cache_hit_skips_llm(self):
        question_id = 4001
        self.bank.store.upsert_ai_question(
            "course-1",
            "old-exam",
            question_id,
            1,
            question_plain="跨考试的完整题干内容",
            answer="3",
        )
        parts = self.make_parts([[question_id]])
        content = make_content(question_id)
        content["content"] = "跨考试的完整题干内容"
        ctx, api = self.make_ctx(parts, {question_id: content})
        ctx.zhidao_answerer = StubAnswerer(error=AssertionError("should hit cache"))
        ctx.deepseek_answerer = StubAnswerer(error=AssertionError("should hit cache"))

        with patch("modules.exam_ctx.time.sleep"):
            ctx.start(submit=False)

        self.assertEqual(
            api.batch_payloads[0]["answerList"][0]["answer"], "3"
        )

    def test_same_exam_sqlite_cache_is_used(self):
        question_id = 4101
        self.bank.store.upsert_ai_question(
            "course-1",
            "exam-1",
            question_id,
            1,
            question_plain="同场考试的题干内容",
            answer="1",
        )
        parts = self.make_parts([[question_id]])
        content = make_content(question_id)
        content["content"] = "同场考试的题干内容"
        ctx, _ = self.make_ctx(parts, {question_id: content})
        ctx.zhidao_answerer = StubAnswerer(error=AssertionError("should hit sqlite"))

        with patch("modules.exam_ctx.time.sleep"):
            ctx.start(submit=False)

        self.assertEqual(
            ctx._answered[str(question_id)]["answer"], "1"
        )


class FormalExamReadbackTests(FormalExamTestCase):
    def test_detail_allow_lookup_writes_answers_back(self):
        parts = self.make_parts([[5001]])
        contents = {5001: make_content(5001)}
        ctx, api = self.make_ctx(
            parts, contents, detail={"isLookAnswer": 1}, auto_submit=True
        )
        ctx.zhidao_answerer = StubAnswerer(result=[1])

        with patch("modules.exam_ctx.time.sleep"):
            success, _, _ = ctx.start(submit=True)

        self.assertTrue(success)
        detail_call = next(
            call for call in api.strategy_calls if call["url"] == FORMAL_OPEN_DETAIL
        )
        self.assertEqual(detail_call["strategy"], "ai_task")
        self.assertEqual(
            detail_call["data"],
            {
                "classId": "class-1",
                "courseId": "course-1",
                "examTestId": "exam-1",
                "examPaperId": "paper-1",
                "examId": "paper-1",
                "studentId": "stu-1",
                "taskId": "task-1",
                "taskType": None,
            },
        )
        self.assertEqual(ctx.answer_bank.get(5001, 1)["answer"], "2")

    def test_detail_denied_does_not_read_back(self):
        parts = self.make_parts([[5101]])
        contents = {5101: make_content(5101)}
        ctx, api = self.make_ctx(
            parts,
            contents,
            detail={"isLookAnswer": 0, "isAllowShowDetail": 0},
            auto_submit=True,
        )

        with patch("modules.exam_ctx.time.sleep"):
            ctx.start(submit=True)

        self.assertIn(FORMAL_OPEN_DETAIL, api.urls())
        self.assertIsNone(ctx.answer_bank.get(5101, 1))

    def test_detail_failure_skips_readback_safely(self):
        parts = self.make_parts([[5201]])
        contents = {5201: make_content(5201)}
        ctx, api = self.make_ctx(parts, contents, auto_submit=True)
        original = api.query_strategy

        def flaky(strategy, url, data, method="POST", **kwargs):
            if url == FORMAL_OPEN_DETAIL:
                raise RuntimeError("network")
            return original(strategy, url, data, method, **kwargs)

        api.query_strategy = flaky
        with patch("modules.exam_ctx.time.sleep"):
            success, answered, total = ctx.start(submit=True)

        self.assertTrue(success)
        self.assertEqual((answered, total), (1, 1))
        self.assertIsNone(ctx.answer_bank.get(5201, 1))


class FormalExamTaskListTests(FormalExamTestCase):
    def test_scan_pending_tasks_uses_ai_task_strategy(self):
        tasks = [{"taskId": 1, "courseId": "c"}]
        api = FakeFormalApi([], {}, tasks=tasks)
        ctx = AiFormalExamCtx(
            api, "course-1", exam_test_id="exam-1", exam_paper_id="paper-1",
            answer_bank=self.bank,
        )
        self.assertEqual(ctx.scan_pending_tasks(), tasks)
        call = api.strategy_calls[0]
        self.assertEqual(call["strategy"], "ai_task")
        self.assertEqual(call["url"], FORMAL_TASK_LIST)
        self.assertEqual(call["data"], {"taskType": 1, "status": 0})

    def test_scan_pending_tasks_accepts_plain_list_and_skips_bad_items(self):
        api = FakeFormalApi([], {})
        ctx = AiFormalExamCtx(
            api, "course-1", exam_test_id="exam-1", exam_paper_id="paper-1",
            answer_bank=self.bank,
        )
        api.query_strategy = lambda *args, **kwargs: {
            "code": 200,
            "data": [{"taskId": 2}, "bad"],
        }
        self.assertEqual(ctx.scan_pending_tasks(), [{"taskId": 2}])

    def test_from_task_maps_public_fields(self):
        api = FakeFormalApi([], {})
        ctx = AiFormalExamCtx.from_task(
            api,
            {
                "courseId": "c-1",
                "classId": "cl-1",
                "examTestId": "e-1",
                "examId": "p-1",
                "taskId": "t-1",
                "studentId": "s-1",
            },
            save_nums=3,
            auto_submit=True,
            answer_bank=self.bank,
        )
        self.assertEqual(ctx.course_id, "c-1")
        self.assertEqual(ctx.class_id, "cl-1")
        self.assertEqual(ctx.exam_test_id, "e-1")
        self.assertEqual(ctx.exam_paper_id, "p-1")
        self.assertEqual(ctx.task_id, "t-1")
        self.assertEqual(ctx.student_id, "s-1")
        self.assertEqual(ctx.save_nums, 3)
        self.assertTrue(ctx.auto_submit)

    def test_open_exam_detail_returns_empty_on_failure(self):
        api = FakeFormalApi([], {})
        ctx = AiFormalExamCtx(
            api, "course-1", exam_test_id="exam-1", exam_paper_id="paper-1",
            answer_bank=self.bank,
        )

        def boom(*args, **kwargs):
            raise RuntimeError("network")

        api.query_strategy = boom
        self.assertEqual(ctx.open_exam_detail(), {})


class FormalExamAliasTests(unittest.TestCase):
    def test_exam_ctx_is_homework_alias(self):
        from modules.exam_ctx import AiExamBase, AiHomeworkCtx

        self.assertIs(ExamCtx, AiHomeworkCtx)
        self.assertTrue(issubclass(AiHomeworkCtx, AiExamBase))
        self.assertTrue(issubclass(AiFormalExamCtx, AiExamBase))
        self.assertFalse(issubclass(AiHomeworkCtx, AiFormalExamCtx))


class FormalExamInjectionTests(unittest.TestCase):
    def test_from_task_injects_question_bank(self):
        api = FakeFormalApi([], {})
        bank = object()
        ctx = AiFormalExamCtx.from_task(
            api,
            {"courseId": "c1", "examTestId": "e1", "examPaperId": "p1"},
            question_bank=bank,
        )
        self.assertIs(ctx.question_bank, bank)
        self.assertTrue(ctx._question_bank_ready)


if __name__ == "__main__":
    unittest.main()