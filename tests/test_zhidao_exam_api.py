# encoding=utf-8
import json
import unittest
from unittest.mock import MagicMock

from modules import zhidao_exam_api as api_mod
from modules.zhidao_exam_api import (
    DO_EXAM,
    GET_LOGIN_SCHOOL_INFO,
    GET_SAVE_ANSWER_LOCK_RESULT,
    GET_STUDENT_FINAL_EXAM,
    SUBMIT_EXAM,
    ZhidaoExamApi,
    extract_final_exams,
    extract_school_id,
    is_exam_started,
    lock_state,
    normalize_final_exam,
    submit_succeeded,
)
from modules.zhidao_homework_api import SAVE_STUDENT_ANSWER


class ZhidaoExamApiParsersTests(unittest.TestCase):
    def test_normalize_and_extract_final_exams_handles_various_shapes(self):
        payload = {
            "rt": {
                "examList": [
                    {
                        "examId": 1001,
                        "stuExamId": 2001,
                        "recruitID": 3001,
                        "courseId": 4001,
                        "schoolId": 5001,
                        "name": "期末综合考试",
                        "studentStartTime": "2026-06-01 09:00:00",
                        "progressType": 2,
                        "state": 2,
                    },
                    {"examName": "无 ID 项应被忽略"},
                    "non-dict-ignored",
                ]
            }
        }
        exams = extract_final_exams(payload)
        self.assertEqual(len(exams), 1)
        exam = exams[0]
        self.assertEqual(exam["examId"], "1001")
        self.assertEqual(exam["studentExamId"], "2001")
        self.assertEqual(exam["recruitId"], "3001")
        self.assertEqual(exam["courseId"], "4001")
        self.assertEqual(exam["schoolId"], "5001")
        self.assertEqual(exam["examName"], "期末综合考试")
        self.assertEqual(extract_final_exams(None), [])
        self.assertEqual(extract_final_exams({"rt": "bad"}), [])

    def test_is_exam_started_never_misclassifies_unopened_progress_type_2(self):
        unopened = normalize_final_exam(
            {
                "examId": "1",
                "state": 1,
                "progressType": 2,
                "studentStartTime": None,
            }
        )
        self.assertFalse(is_exam_started(unopened))

        started_by_time = normalize_final_exam(
            {
                "examId": "2",
                "state": 1,
                "studentStartTime": "2026-06-01 10:00:00",
            }
        )
        self.assertTrue(is_exam_started(started_by_time))

        started_by_state = normalize_final_exam(
            {
                "examId": "3",
                "state": "2",
                "studentStartTime": "",
            }
        )
        self.assertTrue(is_exam_started(started_by_state))

    def test_lock_state_school_id_and_submit_succeeded(self):
        self.assertEqual(lock_state({"rt": {"state": "1"}}), 1)
        self.assertEqual(lock_state({"lockState": 0}), 0)
        self.assertIsNone(lock_state({"rt": {"state": "invalid"}}))
        self.assertIsNone(lock_state("not-a-dict"))

        self.assertEqual(extract_school_id({"rt": {"schoolId": 888}}), "888")
        self.assertEqual(extract_school_id({"rt": {"schoolID": "999"}}), "999")
        self.assertEqual(extract_school_id({"rt": []}), "")

        self.assertTrue(submit_succeeded({"rt": {"statu": 1}}))
        self.assertTrue(submit_succeeded({"status": "1"}))
        self.assertFalse(submit_succeeded({"rt": {"statu": 0}}))
        self.assertFalse(submit_succeeded(None))


class ZhidaoExamApiClientRoutingTests(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        self.client.query.return_value = {"code": 0, "rt": {}}
        self.api = ZhidaoExamApi(self.client)

    def test_exam_calls_use_zhidao_exam_strategy_without_exam_sso(self):
        self.api.get_student_final_exam(course_id="4001", recruit_id="3001", flag=1)
        self.client.ensure_exam_sso.assert_not_called()
        self.client.query.assert_called_once_with(
            "zhidao_exam",
            GET_STUDENT_FINAL_EXAM,
            {"courseId": 4001, "recruitId": "3001", "flag": 1},
        )

        self.client.reset_mock()
        self.api.do_exam(
            recruit_id="3001",
            exam_id="1001",
            student_exam_id="2001",
            school_id="5001",
            course_id="4001",
        )
        self.client.query.assert_called_once_with(
            "zhidao_exam",
            DO_EXAM,
            {
                "recruitId": "3001",
                "examId": "1001",
                "studentExamId": "2001",
                "schoolId": "5001",
                "courseId": "4001",
                "deviceId": "",
            },
        )

        self.client.reset_mock()
        self.api.submit(
            recruit_id="3001",
            course_id="4001",
            exam_id="1001",
            student_exam_id="2001",
            achieve_count=20,
        )
        self.client.query.assert_called_once_with(
            "zhidao_exam",
            SUBMIT_EXAM,
            {
                "recruitId": "3001",
                "courseId": "4001",
                "examId": "1001",
                "stuExamId": "2001",
                "achieveCount": "20",
            },
        )

    def test_homework_strategy_calls_ensure_exam_sso_and_source_1(self):
        self.api.get_save_answer_lock_result(exam_id="1001", recruit_id="3001")
        self.client.ensure_exam_sso.assert_called_once()
        self.client.query.assert_called_once_with(
            "homework",
            GET_SAVE_ANSWER_LOCK_RESULT,
            {"examId": "1001", "recruitId": "3001"},
            extra_fields=None,
        )

        self.client.reset_mock()
        self.api.get_login_school_info()
        self.client.ensure_exam_sso.assert_called_once()
        self.client.query.assert_called_once_with(
            "homework",
            GET_LOGIN_SCHOOL_INFO,
            {},
            extra_fields=None,
        )

        self.client.reset_mock()
        self.api.save_student_answer(
            recruit_id="3001",
            course_id="4001",
            exam_id="1001",
            student_exam_id="2001",
            school_id="5001",
            eid="q-99",
            question_type=1,
            answer="12345",
        )
        self.client.ensure_exam_sso.assert_called_once()
        strategy, url, payload = self.client.query.call_args.args
        kwargs = self.client.query.call_args.kwargs
        self.assertEqual(strategy, "homework")
        self.assertEqual(url, SAVE_STUDENT_ANSWER)
        self.assertEqual(kwargs.get("extra_fields"), {"source": "1"})
        self.assertEqual(payload["recruitId"], "3001")
        parsed_answers = json.loads(payload["stuExamAnswer"])
        self.assertEqual(len(parsed_answers), 1)
        self.assertEqual(parsed_answers[0]["examType"], 1)
        self.assertEqual(parsed_answers[0]["answer"], "12345")

    def test_all_exports_present(self):
        for symbol in api_mod.__all__:
            self.assertTrue(hasattr(api_mod, symbol), f"missing export {symbol}")


if __name__ == "__main__":
    unittest.main()
