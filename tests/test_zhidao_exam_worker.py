import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modules.answer_store import AiQuestionStore
from modules.zhidao_exam_worker import ZhidaoExamWorker


def _payload(*questions):
    return {
        "rt": {
            "examBase": {
                "workExamParts": [{"questionDtos": list(questions)}]
            }
        }
    }


def _question(eid="q1", content="1+1=?", options=((1, "1"), (2, "2"))):
    return {
        "eid": eid,
        "id": eid,
        "name": content,
        "questionType": {"id": 1},
        "questionOptions": [
            {"id": oid, "content": text} for oid, text in options
        ],
    }


def _ids(*values):
    return "```answer\n" + json.dumps([{"id": value} for value in values]) + "\n```"


class _QuietLogger:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


class FakeExamApi:
    def __init__(self, exams, questions=None, lock=0, school="999", submit=None):
        self.exams = exams
        self.questions = questions if questions is not None else [_question()]
        self.lock = lock
        self.school = school
        self.submit_payload = submit or {"rt": {"statu": "1"}}
        self.scan_calls = 0
        self.lock_calls = 0
        self.school_calls = 0
        self.do_calls = []
        self.saved = []
        self.submit_calls = []

    def get_student_final_exam(self, **kwargs):
        self.scan_calls += 1
        return {"rt": self.exams}

    def get_save_answer_lock_result(self, **kwargs):
        self.lock_calls += 1
        return {"rt": {"state": self.lock}}

    def get_login_school_info(self):
        self.school_calls += 1
        return {"rt": {"schoolId": self.school}}

    def do_exam(self, **kwargs):
        self.do_calls.append(kwargs)
        return _payload(*self.questions)

    def save_student_answer(self, **kwargs):
        self.saved.append(kwargs)
        return {"status": "200", "rt": {}}

    def submit(self, **kwargs):
        self.submit_calls.append(kwargs)
        return self.submit_payload


class ExamWorkerTests(unittest.TestCase):
    def setUp(self):
        patcher = patch("modules.zhidao_exam_worker.logger", _QuietLogger())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = AiQuestionStore(
            db_path=root / "bank.db", data_root=root, migrate=False
        )

    def _worker(self, api, answerer=None, **kwargs):
        options = {
            "course_id": "C1",
            "store": self.store,
            "answerer": answerer or (lambda _prompt: _ids(2)),
            "auto_submit": True,
            "sleep": lambda _seconds: None,
        }
        options.update(kwargs)
        return ZhidaoExamWorker(api, **options)

    def test_run_pending_skips_not_started_and_submits_started(self):
        exams = [
            {
                "examId": "E0",
                "studentExamId": "S0",
                "recruitId": "R1",
                "courseId": "C1",
                "examName": "未开始",
                "state": 1,
                "progressType": 1,
            },
            {
                "examId": "E1",
                "studentExamId": "S1",
                "recruitId": "R1",
                "courseId": "C1",
                "schoolId": "",
                "examName": "进行中",
                "studentStartTime": "2026-11-23 10:00:00",
            },
        ]
        api = FakeExamApi(exams)
        worker = self._worker(api)
        results = worker.run_pending()

        self.assertEqual(api.scan_calls, 1)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["reason"], "not_started")
        self.assertEqual(results[0]["exam_id"], "E0")
        self.assertEqual(len(api.do_calls), 1)
        self.assertEqual(api.do_calls[0]["exam_id"], "E1")
        self.assertEqual(api.do_calls[0]["school_id"], "999")
        self.assertEqual(api.school_calls, 1)

        self.assertEqual(len(api.saved), 1)
        self.assertEqual(api.saved[0]["eid"], "q1")
        self.assertEqual(api.saved[0]["answer"], "2")
        self.assertEqual(api.saved[0]["exam_id"], "E1")
        self.assertEqual(api.saved[0]["student_exam_id"], "S1")

        self.assertEqual(results[1]["answered"], 1)
        self.assertTrue(results[1]["submitted"])
        self.assertEqual(len(api.submit_calls), 1)
        self.assertEqual(api.submit_calls[0]["achieve_count"], 1)
        self.assertEqual(api.submit_calls[0]["student_exam_id"], "S1")

        row = self.store.get_by_eid("C1", "q1", "E1")
        self.assertIsNotNone(row)

    def test_locked_exam_is_skipped(self):
        api = FakeExamApi([], lock=1)
        worker = self._worker(api)
        stats = worker.run(
            recruit_id="R1",
            course_id="C1",
            exam_id="E1",
            student_exam_id="S1",
            school_id="9",
        )
        self.assertTrue(stats["skipped"])
        self.assertEqual(stats["lock_state"], 1)
        self.assertEqual(api.do_calls, [])
        self.assertEqual(api.saved, [])
        self.assertEqual(api.submit_calls, [])

    def test_auto_submit_false_only_saves(self):
        api = FakeExamApi([])
        worker = self._worker(api, auto_submit=False)
        stats = worker.run(
            recruit_id="R1",
            course_id="C1",
            exam_id="E1",
            student_exam_id="S1",
            school_id="9",
        )
        self.assertEqual(stats["answered"], 1)
        self.assertFalse(stats["submitted"])
        self.assertEqual(api.submit_calls, [])
        self.assertEqual(len(api.saved), 1)

    def test_submit_failure_keeps_submitted_false(self):
        api = FakeExamApi([], submit={"rt": {"statu": "0"}})
        worker = self._worker(api)
        stats = worker.run(
            recruit_id="R1",
            course_id="C1",
            exam_id="E1",
            student_exam_id="S1",
            school_id="9",
        )
        self.assertFalse(stats["submitted"])
        self.assertEqual(len(api.submit_calls), 1)

    def test_image_question_requests_vision_fallback(self):
        api = FakeExamApi(
            [],
            questions=[
                _question(
                    content='<img src="https://cdn.example/q.png">',
                )
            ],
        )

        def boom(_prompt):
            raise AssertionError("图片题不应调用文本模型")

        worker = self._worker(api, answerer=boom)
        stats = worker.run(
            recruit_id="R1",
            course_id="C1",
            exam_id="E1",
            student_exam_id="S1",
            school_id="9",
        )
        self.assertTrue(stats["needs_vision"])
        self.assertEqual(api.saved, [])
        self.assertEqual(api.submit_calls, [])

    def test_captcha_message_raises_captcha_required(self):
        from modules.zhs_api import CaptchaRequiredError

        class CaptchaApi(FakeExamApi):
            def save_student_answer(self, **kwargs):
                self.saved.append(kwargs)
                return {"message": "请完成滑块验证"}

        api = CaptchaApi([])
        worker = self._worker(api)
        with self.assertRaises(CaptchaRequiredError):
            worker.run(
                recruit_id="R1",
                course_id="C1",
                exam_id="E1",
                student_exam_id="S1",
                school_id="9",
            )
        self.assertEqual(api.submit_calls, [])

    def test_run_pending_stores_answers_under_exam_course(self):
        exams = [
            {
                "examId": "E9",
                "studentExamId": "S9",
                "recruitId": "R9",
                "courseId": "C9",
                "examName": "其他课程考试",
                "studentStartTime": "2026-11-23 10:00:00",
            }
        ]
        api = FakeExamApi(exams)
        worker = self._worker(api)
        worker.run_pending()

        self.assertIsNotNone(self.store.get_by_eid("C9", "q1", "E9"))
        self.assertIsNone(self.store.get_by_eid("C1", "q1", "E9"))

    def test_bank_stats_logged_even_when_run_fails(self):
        class BoomApi(FakeExamApi):
            def get_save_answer_lock_result(self, **kwargs):
                raise RuntimeError("lock query failed")

        class Bank:
            def __init__(self):
                self.calls = 0

            def log_stats(self):
                self.calls += 1

        bank = Bank()
        worker = self._worker(BoomApi([]), question_bank=bank)
        with self.assertRaises(RuntimeError):
            worker.run(
                recruit_id="R1",
                course_id="C1",
                exam_id="E1",
                student_exam_id="S1",
                school_id="9",
            )
        self.assertEqual(bank.calls, 1)


class FakeClient:
    def __init__(self):
        self.sso_calls = 0
        self.calls = []

    def ensure_exam_sso(self):
        self.sso_calls += 1

    def query(self, strategy, url, data, extra_fields=None):
        self.calls.append(
            {"strategy": strategy, "url": url, "data": data, "extra": extra_fields}
        )
        return {"status": "200", "rt": {}}


class ExamApiContractTests(unittest.TestCase):
    def setUp(self):
        from modules.zhidao_exam_api import ZhidaoExamApi

        self.client = FakeClient()
        self.api = ZhidaoExamApi(self.client)

    def test_exam_endpoints_use_zhidao_exam_without_sso(self):
        self.api.get_student_final_exam(course_id="100", recruit_id="R1", flag=1)
        self.assertEqual(self.client.calls[-1]["strategy"], "zhidao_exam")
        self.assertEqual(
            self.client.calls[-1]["data"],
            {"courseId": 100, "recruitId": "R1", "flag": 1},
        )
        self.api.do_exam(
            recruit_id="R1",
            exam_id="E1",
            student_exam_id="S1",
            school_id="9",
            course_id="C1",
        )
        self.assertEqual(self.client.calls[-1]["strategy"], "zhidao_exam")
        self.assertEqual(self.client.calls[-1]["data"]["deviceId"], "")
        self.assertEqual(self.client.sso_calls, 0)

    def test_lock_and_school_use_homework_strategy(self):
        self.api.get_save_answer_lock_result(exam_id="E1", recruit_id="R1")
        self.assertEqual(self.client.calls[-1]["strategy"], "homework")
        self.assertEqual(
            self.client.calls[-1]["data"], {"examId": "E1", "recruitId": "R1"}
        )
        self.api.get_login_school_info()
        self.assertEqual(self.client.calls[-1]["strategy"], "homework")
        self.assertEqual(self.client.calls[-1]["data"], {})
        self.assertEqual(self.client.sso_calls, 2)

    def test_save_student_answer_sets_source_and_exam_type(self):
        self.api.save_student_answer(
            recruit_id="R1",
            course_id="C1",
            exam_id="E1",
            student_exam_id="S1",
            school_id="9",
            eid="enc_1",
            question_type=2,
            answer="1,2",
        )
        call = self.client.calls[-1]
        self.assertEqual(call["strategy"], "homework")
        self.assertEqual(call["extra"], {"source": "1"})
        self.assertEqual(self.client.sso_calls, 1)
        self.assertEqual(set(call["data"]), {"stuExamAnswer", "recruitId"})
        item = json.loads(call["data"]["stuExamAnswer"])[0]
        self.assertEqual(item["examType"], 1)
        self.assertEqual(item["fromType"], 3)
        self.assertEqual(item["eid"], "enc_1")
        self.assertEqual(item["stuExamId"], "S1")
        self.assertEqual(item["schoolId"], "9")
        self.assertEqual(item["examId"], "E1")
        self.assertEqual(item["questionType"], 2)
        self.assertEqual(item["answer"], "1,2")

    def test_submit_uses_stu_exam_id_and_string_achieve_count(self):
        self.api.submit(
            recruit_id="R1",
            course_id="C1",
            exam_id="E1",
            student_exam_id="S1",
            achieve_count=5,
        )
        self.assertEqual(
            self.client.calls[-1]["data"],
            {
                "recruitId": "R1",
                "courseId": "C1",
                "examId": "E1",
                "stuExamId": "S1",
                "achieveCount": "5",
            },
        )

    def test_helper_parsers(self):
        from modules.zhidao_exam_api import (
            extract_final_exams,
            extract_school_id,
            is_exam_started,
            lock_state,
            submit_succeeded,
        )

        exams = extract_final_exams(
            {
                "rt": [
                    {"examId": "E1", "studentExamId": "S1", "studentStartTime": "t"},
                    {"id": "E2", "state": 1},
                ]
            }
        )
        self.assertEqual([exam["examId"] for exam in exams], ["E1", ""])
        self.assertEqual(exams[1]["studentExamId"], "E2")
        self.assertTrue(is_exam_started(exams[0]))
        self.assertFalse(is_exam_started(exams[1]))
        self.assertTrue(is_exam_started({"progressType": 2}))
        self.assertEqual(lock_state({"rt": {"state": "0"}}), 0)
        self.assertIsNone(lock_state({"rt": {}}))
        self.assertEqual(extract_school_id({"rt": {"schoolId": 9}}), "9")
        self.assertTrue(submit_succeeded({"rt": {"statu": "1"}}))
        self.assertFalse(submit_succeeded({"rt": {"statu": "0"}}))


if __name__ == "__main__":
    unittest.main()