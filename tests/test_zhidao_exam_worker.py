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

    def test_do_exam_captcha_minus_12_propagates(self):
        from modules.zhs_api import CaptchaRequiredError

        class CaptchaExamApi(FakeExamApi):
            def do_exam(self, **kwargs):
                raise CaptchaRequiredError("接口要求验证码(code=-12)")

        worker = self._worker(CaptchaExamApi([]))
        with self.assertRaises(CaptchaRequiredError):
            worker.run(
                recruit_id="R1",
                course_id="C1",
                exam_id="E1",
                student_exam_id="S1",
                school_id="9",
            )

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
        self.assertEqual(stats["image_questions"], 1)
        self.assertEqual(api.saved, [])
        self.assertEqual(api.submit_calls, [])

    def test_mixed_text_and_image_questions_saves_text_and_skips_submit(self):
        api = FakeExamApi(
            [],
            questions=[
                _question(eid="q_txt1", content="1+1=?", options=((11, "1"), (12, "2"))),
                _question(eid="q_img", content='<img src="https://cdn.example/fig.png">'),
                _question(eid="q_txt2", content="2+2=?", options=((21, "3"), (22, "4"))),
            ],
        )
        prompts = []

        def fake_answerer(prompt):
            prompts.append(prompt)
            if "1+1=?" in prompt:
                return _ids(12)
            return _ids(22)

        worker = self._worker(api, answerer=fake_answerer, auto_submit=True)
        stats = worker.run(
            recruit_id="R1",
            course_id="C1",
            exam_id="E_MIX",
            student_exam_id="S_MIX",
            school_id="9",
        )
        self.assertTrue(stats["needs_vision"])
        self.assertEqual(stats["image_questions"], 1)
        self.assertEqual(stats["answered"], 2)
        self.assertFalse(stats["submitted"])
        self.assertEqual([item["eid"] for item in api.saved], ["q_txt1", "q_txt2"])
        self.assertEqual([item["answer"] for item in api.saved], ["12", "22"])
        self.assertEqual(api.submit_calls, [])
        self.assertEqual(len(prompts), 2)

    def test_cross_exam_course_bank_hit_skips_llm(self):
        # 章节作业 hw-1 已入库两道题的正确答案（分别演示选项哈希命中与题干命中）
        self.store.save_options_by_id(
            "C1",
            "hw-1",
            "hw_q1",
            [{"id": "101", "content": "甲"}, {"id": "102", "content": "乙"}],
            question_type="1",
            content="旧作业题干A",
        )
        self.store.mark_correct("C1", "hw-1", question_id="hw_q1", correct_options=["102"])

        self.store.save_options_by_id(
            "C1",
            "hw-1",
            "hw_q2",
            [],
            question_type="3",
            content="光在真空中的速度符号是？",
        )
        self.store.mark_correct("C1", "hw-1", question_id="hw_q2", correct_options=["c"])

        api = FakeExamApi(
            [],
            questions=[
                _question(
                    eid="final_eid_1",
                    content="期末略有改写但选项ID相同",
                    options=((101, "甲"), (102, "乙")),
                ),
                {
                    "eid": "final_eid_2",
                    "id": "final_eid_2",
                    "name": "光在真空中的速度符号是？",
                    "questionType": {"id": 3},
                    "questionOptions": [],
                },
            ],
        )

        def boom(_prompt):
            raise AssertionError("跨测验命中同课程题库时不应重复请求 LLM")

        worker = self._worker(api, answerer=boom, auto_submit=True)
        stats = worker.run(
            recruit_id="R1",
            course_id="C1",
            exam_id="final-1",
            student_exam_id="S_FINAL",
            school_id="9",
        )
        self.assertEqual(stats["answered"], 2)
        self.assertTrue(stats["submitted"])
        self.assertEqual([item["answer"] for item in api.saved], ["102", "c"])

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

    def test_run_pending_uses_worker_recruit_and_skips_unstarted_progress_type_2(self):
        from datetime import datetime

        exams = [
            {
                "id": "S_FUTURE",
                "examId": "E_FUTURE",
                "courseId": 1000013343,
                "examName": "未开放期末考试",
                "state": 1,
                "progressType": 2,
                "studentStartTime": None,
                "startTime": "2026-12-01 00:00:00",
                "endDate": "2026-12-04 23:59:59",
            },
            {
                "id": "S_LIVE",
                "examId": "E_LIVE",
                "courseId": 1000013343,
                "examName": "进行中考试",
                "state": 1,
                "progressType": 2,
                "studentStartTime": "2026-11-24 08:30:00",
                "endDate": "2026-11-25 23:59:59",
            },
            {
                "id": "S_EXP",
                "examId": "E_EXP",
                "courseId": 1000013343,
                "examName": "已截止考试",
                "studentStartTime": "2026-10-01 08:30:00",
                "endDate": "2026-10-02 23:59:59",
            },
        ]
        api = FakeExamApi(exams)
        worker = self._worker(api, recruit_id="458533", school_id="1847")
        results = worker.run_pending(now=datetime(2026, 11, 24, 9, 0, 0))

        self.assertEqual(len(results), 3)
        self.assertEqual(results[0]["reason"], "not_started")
        self.assertEqual(results[1]["answered"], 1)
        self.assertEqual(results[2]["reason"], "expired")
        self.assertEqual(len(api.do_calls), 1)
        self.assertEqual(api.do_calls[0]["recruit_id"], "458533")
        self.assertEqual(api.do_calls[0]["school_id"], "1847")

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

    def query(self, strategy, url, data, extra_fields=None, allow_retry=None):
        self.calls.append(
            {
                "strategy": strategy,
                "url": url,
                "data": data,
                "extra": extra_fields,
                "allow_retry": allow_retry,
            }
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
        self.assertIs(self.client.calls[-1]["allow_retry"], False)
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

    def test_submit_marks_captcha_error_non_retryable(self):
        from modules.zhidao_exam_api import ZhidaoExamApi
        from modules.zhs_api import CaptchaRequiredError

        class _CaptchaClient(FakeClient):
            def query(self, strategy, url, data, extra_fields=None, allow_retry=None):
                super().query(
                    strategy, url, data, extra_fields=extra_fields, allow_retry=allow_retry
                )
                raise CaptchaRequiredError("exam submit captcha", code="-12")

        api = ZhidaoExamApi(_CaptchaClient())
        with self.assertRaises(CaptchaRequiredError) as ctx:
            api.submit(
                recruit_id="R1",
                course_id="C1",
                exam_id="E1",
                student_exam_id="S1",
                achieve_count=1,
            )
        self.assertFalse(ctx.exception.retryable)
        self.assertIs(api.client.calls[-1]["allow_retry"], False)

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
        # 真实接口中未开考考试 (state=1, studentStartTime=null) 也常带 progressType=2，绝不能误判为已开考
        self.assertFalse(
            is_exam_started(
                {
                    "state": 1,
                    "studentStartTime": None,
                    "progressType": 2,
                    "startTime": "2026-12-01 00:00:00",
                }
            )
        )
        self.assertTrue(is_exam_started({"state": 2}))
        self.assertEqual(lock_state({"rt": {"state": "0"}}), 0)
        self.assertIsNone(lock_state({"rt": {}}))
        self.assertEqual(extract_school_id({"rt": {"schoolId": 9}}), "9")
        self.assertTrue(submit_succeeded({"rt": {"statu": "1"}}))
        self.assertFalse(submit_succeeded({"rt": {"statu": "0"}}))


class ExamHttpCaptchaTests(unittest.TestCase):
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

    def _valid_fp(self):
        return ("E" + "zY8+/=_-5wUvX6tQ") * 10 + "abcdef:1764200000123"

    def _worker(self, api, **kwargs):
        options = {
            "course_id": "C1",
            "store": self.store,
            "answerer": lambda _prompt: _ids(2),
            "auto_submit": True,
            "sleep": lambda _seconds: None,
        }
        options.update(kwargs)
        return ZhidaoExamWorker(api, **options)

    def test_exam_http_captcha_unlocks_and_retries_do_exam_and_save_answer(self):
        from modules.yidun_http import CaptchaSolution
        from modules.zhs_api import CaptchaRequiredError

        class CaptchaOnceExamApi(FakeExamApi):
            def __init__(self):
                super().__init__([])
                self.do_attempts = 0
                self.save_attempts = 0
                self.validate_calls = []

            def do_exam(self, **kwargs):
                self.do_attempts += 1
                if self.do_attempts == 1:
                    raise CaptchaRequiredError("接口要求验证码(code=-12)", code="-12")
                return super().do_exam(**kwargs)

            def save_student_answer(self, **kwargs):
                self.save_attempts += 1
                if self.save_attempts == 1:
                    raise CaptchaRequiredError("接口要求验证码(code=-12)", code="-12")
                return super().save_student_answer(**kwargs)

            def validate_slide_token(self, **kwargs):
                self.validate_calls.append(kwargs)
                return {"status": "200", "pass": True}

        api = CaptchaOnceExamApi()
        worker = self._worker(
            api,
            captcha_solver=lambda _trigger: CaptchaSolution("CN31_exam_v_i_1", "raw"),
            fp_provider=self._valid_fp,
        )
        stats = worker.run(
            recruit_id="R1",
            course_id="C1",
            exam_id="E1",
            student_exam_id="S1",
            school_id="9",
        )
        self.assertEqual(stats["answered"], 1)
        self.assertTrue(stats["submitted"])
        self.assertEqual(api.do_attempts, 2)
        self.assertEqual(api.save_attempts, 2)
        self.assertEqual(len(api.validate_calls), 2)
        self.assertEqual(api.validate_calls[0]["exam_id"], "E1")

    def test_exam_http_captcha_falls_back_without_fp_or_on_reject(self):
        from modules.yidun_http import CaptchaSolution
        from modules.zhs_api import CaptchaRequiredError

        class CaptchaExamApi(FakeExamApi):
            def __init__(self, *, passed=False):
                super().__init__([])
                self.passed = passed
                self.do_attempts = 0
                self.validate_calls = []

            def do_exam(self, **kwargs):
                self.do_attempts += 1
                raise CaptchaRequiredError("接口要求验证码(code=-12)", code="-12")

            def validate_slide_token(self, **kwargs):
                self.validate_calls.append(kwargs)
                return {"status": "200", "pass": self.passed}

        # 无 fp 静默回退
        api_no_fp = CaptchaExamApi(passed=True)
        worker_no_fp = self._worker(
            api_no_fp,
            captcha_solver=lambda _trigger: CaptchaSolution("CN31_ex_v_i_1", "raw"),
            fp_provider=lambda: None,
        )
        with self.assertRaises(CaptchaRequiredError):
            worker_no_fp.run(
                recruit_id="R1", course_id="C1", exam_id="E1", student_exam_id="S1", school_id="9"
            )
        self.assertEqual(api_no_fp.do_attempts, 1)
        self.assertEqual(api_no_fp.validate_calls, [])

        # validateSlideToken 返回 pass=False 静默回退
        api_rej = CaptchaExamApi(passed=False)
        worker_rej = self._worker(
            api_rej,
            captcha_solver=lambda _trigger: CaptchaSolution("CN31_ex_v_i_1", "raw"),
            fp_provider=self._valid_fp,
        )
        with self.assertRaises(CaptchaRequiredError):
            worker_rej.run(
                recruit_id="R1", course_id="C1", exam_id="E1", student_exam_id="S1", school_id="9"
            )
        self.assertEqual(api_rej.do_attempts, 1)
        self.assertEqual(len(api_rej.validate_calls), 1)

    def test_exam_submit_never_auto_retries_on_captcha(self):
        from modules.yidun_http import CaptchaSolution
        from modules.zhs_api import CaptchaRequiredError

        class SubmitCaptchaExamApi(FakeExamApi):
            def __init__(self):
                super().__init__([])
                self.validate_calls = []

            def submit(self, **kwargs):
                self.submit_calls.append(kwargs)
                raise CaptchaRequiredError("接口要求验证码(code=-12)", code="-12")

            def validate_slide_token(self, **kwargs):
                self.validate_calls.append(kwargs)
                return {"status": "200", "pass": True}

        api = SubmitCaptchaExamApi()
        worker = self._worker(
            api,
            captcha_solver=lambda _trigger: CaptchaSolution("CN31_sub_v_i_1", "raw"),
            fp_provider=self._valid_fp,
        )
        with self.assertRaises(CaptchaRequiredError) as ctx:
            worker.run(
                recruit_id="R1", course_id="C1", exam_id="E1", student_exam_id="S1", school_id="9"
            )
        self.assertFalse(ctx.exception.retryable)
        self.assertEqual(len(api.submit_calls), 1)
        self.assertEqual(api.validate_calls, [])

        # 验证外层 _run_zhidao_with_captcha 绝不会因交卷 -12 过码后二次重跑 worker.run() / submit()
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch
        from modules import homework
        from modules.stages.zhidao_assessment import _run_zhidao_with_captcha

        api2 = SubmitCaptchaExamApi()
        worker2 = self._worker(api2)
        run_calls = 0

        def _run_once():
            nonlocal run_calls
            run_calls += 1
            return worker2.run(
                recruit_id="R1", course_id="C1", exam_id="E1", student_exam_id="S1", school_id="9"
            )

        with patch.object(
            homework, "_settle_captcha", AsyncMock(return_value=True)
        ) as settle_mock:
            with self.assertRaises(CaptchaRequiredError):
                asyncio.run(
                    _run_zhidao_with_captcha(
                        page=MagicMock(),
                        client=MagicMock(),
                        config=MagicMock(),
                        logger=MagicMock(),
                        label="期末考试",
                        captcha_url="",
                        run_fn=_run_once,
                    )
                )
        self.assertEqual(run_calls, 1)
        self.assertEqual(len(api2.submit_calls), 1)
        settle_mock.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()