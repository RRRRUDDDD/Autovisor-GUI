import json
import unittest

from modules.zhidao_homework_api import (
    GET_STU_ANSWER_INFO,
    LOOK_HOMEWORK,
    SAVE_COURSE_TRAN,
    SAVE_STUDENT_ANSWER,
    SUBMIT_HOMEWORK,
    ZhidaoHomeworkApi,
    build_student_answer_item,
    correct_option_ids,
    extract_exam_questions,
    extract_homework_list,
    extract_score,
    format_student_answer,
    normalize_question,
    parse_exam_page_url,
    parse_review,
    question_type_id,
)


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


def _payload(*dtos):
    return {"rt": {"examBase": {"workExamParts": [{"questionDtos": list(dtos)}]}}}


class UrlConstantsTests(unittest.TestCase):
    def test_contract_paths(self):
        base = "https://studentexam-api.zhihuishu.com/studentExam/gateway/t/v1"
        self.assertEqual(SAVE_STUDENT_ANSWER, f"{base}/answer/saveStudentAnswer")
        self.assertEqual(SUBMIT_HOMEWORK, f"{base}/answer/submit")
        self.assertEqual(GET_STU_ANSWER_INFO, f"{base}/answer/getStuAnswerInfo")
        self.assertEqual(SAVE_COURSE_TRAN, f"{base}/student/saveCourseTran")
        self.assertEqual(LOOK_HOMEWORK, f"{base}/student/lookHomework")


class UrlParsingTests(unittest.TestCase):
    def test_parses_homework_and_exam_urls(self):
        base = (
            "https://onlineexamh5new.zhihuishu.com/stuExamWeb.html"
            "#/webExamList/{kind}/458533/tok1/tok2/1000013343/1847/0"
        )
        homework = parse_exam_page_url(base.format(kind="dohomework"))
        self.assertEqual(
            homework,
            {
                "kind": "dohomework",
                "recruitId": "458533",
                "stuExamId": "tok1",
                "examId": "tok2",
                "courseId": "1000013343",
                "schoolId": "1847",
            },
        )
        exam = parse_exam_page_url(base.format(kind="doexam"))
        self.assertEqual(exam["kind"], "doexam")
        self.assertEqual(exam["examId"], "tok2")

    def test_rejects_non_answer_and_truncated_urls(self):
        self.assertEqual(parse_exam_page_url(""), {})
        self.assertEqual(
            parse_exam_page_url(
                "https://onlineexamh5new.zhihuishu.com/#/webExamList?recruitId=x"
            ),
            {},
        )
        self.assertEqual(
            parse_exam_page_url("https://x/#/webExamList/dohomework/1/2/3"),
            {},
        )


class NormalizeTests(unittest.TestCase):
    def test_question_type_id(self):
        self.assertEqual(question_type_id(2), 2)
        self.assertEqual(question_type_id({"id": 14}), 14)
        self.assertEqual(question_type_id("3"), 3)
        self.assertEqual(question_type_id(99), 1)
        self.assertEqual(question_type_id(None), 1)

    def test_normalize_question_keeps_eid_and_question_id_separate(self):
        dto = {
            "eid": "e-1",
            "id": 101,
            "name": "<p>题干</p>",
            "questionType": {"id": 2},
            "questionOptions": [{"id": 11, "content": "甲"}, {"id": 12, "content": "乙"}],
        }
        question = normalize_question(dto)
        self.assertEqual(question["eid"], "e-1")
        self.assertEqual(question["question_id"], "101")
        self.assertEqual(question["type"], 2)
        self.assertEqual(question["type_name"], "multiple")
        self.assertEqual(question["options"][1], {"id": "12", "content": "乙"})

        looked = normalize_question({"id": 90001, "name": "回读题"})
        self.assertEqual(looked["eid"], "")
        self.assertEqual(looked["question_id"], "90001")

    def test_extract_exam_questions_supports_part_and_flat_lists(self):
        questions = extract_exam_questions(
            _payload(
                {"eid": "a", "name": "A", "questionType": {"id": 1}},
                {"eid": "b", "name": "B", "questionType": {"id": 3}},
            )
        )
        self.assertEqual([item["eid"] for item in questions], ["a", "b"])

        flat = extract_exam_questions(
            {"rt": [{"eid": "c", "name": "C", "questionType": {"id": 14}}]}
        )
        self.assertEqual(flat[0]["type"], 14)

        looked = extract_exam_questions({"rt": [{"id": 90001, "name": "回读"}]})
        self.assertEqual(looked[0]["eid"], "")
        self.assertEqual(looked[0]["question_id"], "90001")

        self.assertEqual(extract_exam_questions(None), [])
        self.assertEqual(extract_exam_questions({"rt": {"examBase": {}}}), [])

    def test_extract_homework_list_maps_ids_and_next(self):
        parsed = extract_homework_list(
            {
                "rt": {
                    "next": -1,
                    "studentHomeworkList": [
                        {
                            "id": "S1",
                            "examId": "E1",
                            "recruitId": "R1",
                            "courseId": "C1",
                            "examName": "作业一",
                            "state": 1,
                            "isMarking": 2,
                        }
                    ],
                }
            }
        )
        self.assertIsNone(parsed["next"])
        item = parsed["items"][0]
        self.assertEqual(item["studentExamId"], "S1")
        self.assertEqual(item["examId"], "E1")
        self.assertEqual(item["examName"], "作业一")
        self.assertEqual(item["isMarking"], 2)

        paged = extract_homework_list({"rt": {"next": "1", "studentHomeworkList": []}})
        self.assertEqual(paged["next"], 1)
        self.assertEqual(paged["items"], [])


class ReviewTests(unittest.TestCase):
    def test_correct_option_ids_reads_flag_and_lists(self):
        dto = {
            "correctOptions": [{"id": 1}, 2],
            "questionOptions": [
                {"id": 3, "isCorrect": 0},
                {"id": 4, "isCorrect": True},
            ],
        }
        self.assertEqual(correct_option_ids(dto), ["1", "2", "4"])

    def test_parse_review_merges_look_and_answer_info(self):
        looked = _payload(
            {
                "eid": "q1",
                "isCorrect": 0,
                "userAnswerVo": {"answer": "11"},
            },
            {
                "eid": "q2",
                "isCorrect": 1,
                "correctOptions": [21, 22],
                "userAnswerVo": {"answer": [21, 22]},
            },
        )
        info = {"rt": [{"eid": "q3", "isRight": 0, "userAnswer": "错答"}]}
        review = parse_review(looked, info)
        self.assertFalse(review["q1"]["correct"])
        self.assertEqual(review["q1"]["user_answer"], "11")
        self.assertTrue(review["q2"]["correct"])
        self.assertEqual(review["q2"]["correct_options"], ["21", "22"])
        self.assertEqual(review["q2"]["user_answer"], "21,22")
        self.assertFalse(review["q3"]["correct"])

    def test_parse_review_keys_look_homework_by_numeric_id(self):
        looked = {"rt": [{"id": 90001, "isCorrect": 0, "userAnswerVo": {"answer": "11"}}]}
        info = {"rt": [{"questionId": 90001, "isRight": 0, "userAnswer": "12"}]}
        review = parse_review(looked, info)
        self.assertIn("90001", review)
        self.assertFalse(review["90001"]["correct"])
        self.assertEqual(review["90001"]["user_answer"], "11")


class AnswerFormatTests(unittest.TestCase):
    def test_format_student_answer(self):
        self.assertEqual(format_student_answer(2, [11, 12]), "11,12")
        self.assertEqual(format_student_answer(3, ["甲", "乙"]), "甲/乙")
        self.assertEqual(format_student_answer(1, [7]), "7")

    def test_build_student_answer_item_matches_contract(self):
        item = build_student_answer_item(
            eid="e1",
            question_type=2,
            answer="1,2",
            recruit_id="R1",
            exam_id="EX",
            stu_exam_id="S1",
            school_id="9",
            exam_type="",
        )
        self.assertEqual(
            item,
            {
                "examId": "EX",
                "recruitId": "R1",
                "stuExamId": "S1",
                "eid": "e1",
                "schoolId": "9",
                "deviceId": "",
                "examType": "",
                "fromType": 3,
                "answer": "1,2",
                "dataIds": "",
                "questionType": 2,
            },
        )

    def test_extract_score(self):
        self.assertEqual(extract_score({"rt": {"score": "87.5"}}), 87.5)
        self.assertEqual(extract_score({"score": 60}), 60.0)
        self.assertEqual(extract_score({"rt": {"examScore": 80}}), 80.0)
        self.assertEqual(extract_score({"rt": {"userScore": "92.5"}}), 92.5)
        # 未判分试卷只有满分 totalScore 时，严禁误判为已拿满分
        self.assertIsNone(extract_score({"rt": {"score": None, "totalScore": 100}}))
        self.assertIsNone(extract_score({"totalScore": 100}))
        self.assertIsNone(extract_score({"rt": {}}))
        self.assertIsNone(extract_score(None))


class HomeworkApiTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.api = ZhidaoHomeworkApi(self.client)

    def _last(self):
        return self.client.calls[-1]

    def test_get_student_homework_body(self):
        self.api.get_student_homework(recruit_id="R1", course_id="100", flag=2)
        call = self._last()
        self.assertEqual(call["strategy"], "homework")
        self.assertEqual(
            call["data"],
            {
                "recruitId": "R1",
                "courseId": 100,
                "flag": 2,
                "pageSize": 100,
                "pageNum": 0,
            },
        )
        self.assertEqual(self.client.sso_calls, 1)

    def test_all_calls_ensure_sso_and_use_homework_strategy(self):
        self.api.do_homework(
            recruit_id="R1",
            exam_id="E1",
            student_exam_id="S1",
            school_id="9",
            course_id="C1",
        )
        self.assertEqual(self._last()["strategy"], "homework")
        self.assertEqual(
            self._last()["data"],
            {
                "recruitId": "R1",
                "examId": "E1",
                "studentExamId": "S1",
                "schoolId": "9",
                "courseId": "C1",
            },
        )

        self.api.save_course_tran(recruit_id="R1", exam_id="E1", course_id="100")
        self.assertEqual(self._last()["url"], SAVE_COURSE_TRAN)
        self.assertEqual(
            self._last()["data"],
            {"recruitId": "R1", "examId": "E1", "description": "", "courseId": 100},
        )

        self.api.look_homework(
            recruit_id="R1",
            student_exam_id="S1",
            exam_id="E1",
            school_id="9",
            course_id="C1",
        )
        self.assertEqual(self._last()["url"], LOOK_HOMEWORK)
        self.assertEqual(
            self._last()["data"],
            {
                "recruitId": "R1",
                "studentExamId": "S1",
                "examId": "E1",
                "schoolId": "9",
                "courseId": "C1",
            },
        )

        self.api.get_stu_answer_info(
            recruit_id="R1",
            stu_exam_id="S1",
            exam_id="E1",
            school_id="9",
            course_id="C1",
            question_ids=[90001, 90002],
        )
        self.assertEqual(self._last()["url"], GET_STU_ANSWER_INFO)
        self.assertEqual(self._last()["data"]["questionIds"], "90001,90002")

        self.assertEqual(self.client.sso_calls, 4)

    def test_get_stu_answer_info_accepts_comma_string(self):
        self.api.get_stu_answer_info(
            recruit_id="R1",
            stu_exam_id="S1",
            exam_id="E1",
            question_ids="90001,90002",
        )
        self.assertEqual(self._last()["data"]["questionIds"], "90001,90002")

    def test_save_student_answer_serializes_contract_item(self):
        self.api.save_student_answer(
            recruit_id="R1",
            exam_id="E1",
            stu_exam_id="S1",
            school_id="9",
            eid="enc_1",
            question_type=1,
            answer="11",
            exam_type="",
        )
        call = self._last()
        self.assertEqual(call["url"], SAVE_STUDENT_ANSWER)
        self.assertEqual(set(call["data"]), {"stuExamAnswer", "recruitId"})
        self.assertEqual(call["data"]["recruitId"], "R1")
        decoded = json.loads(call["data"]["stuExamAnswer"])
        self.assertEqual(decoded[0]["eid"], "enc_1")
        self.assertEqual(decoded[0]["examId"], "E1")
        self.assertEqual(decoded[0]["stuExamId"], "S1")
        self.assertEqual(decoded[0]["fromType"], 3)
        self.assertEqual(decoded[0]["dataIds"], "")
        self.assertEqual(decoded[0]["deviceId"], "")

    def test_submit_uses_exam_id_and_string_achieve_count(self):
        self.api.submit(
            recruit_id="R1", exam_id="E1", stu_exam_id="S1", achieve_count=9
        )
        call = self._last()
        self.assertEqual(call["url"], SUBMIT_HOMEWORK)
        self.assertEqual(
            call["data"],
            {
                "recruitId": "R1",
                "examId": "E1",
                "stuExamId": "S1",
                "achieveCount": "9",
            },
        )

    def test_query_course_uses_zhidao_strategy(self):
        from modules.zhidao_homework_api import QUERY_COURSE

        self.api.query_course("4e5d515d")
        call = self._last()
        self.assertEqual(call["strategy"], "zhidao")
        self.assertEqual(call["url"], QUERY_COURSE)
        self.assertEqual(call["data"], {"recruitAndCourseId": "4e5d515d"})
        self.assertEqual(self.client.sso_calls, 0)


class CourseMetaAndHelpersTests(unittest.TestCase):
    def test_extract_course_meta_parses_query_course_response(self):
        from modules.zhidao_homework_api import extract_course_meta

        payload = {
            "code": 0,
            "data": {
                "schoolId": 1847,
                "recruitId": 458533,
                "courseInfo": {
                    "courseId": 1000013343,
                    "name": "大学物理思维导引（下）",
                    "courseMode": 2,
                },
            },
        }
        self.assertEqual(
            extract_course_meta(payload),
            {
                "recruitId": "458533",
                "courseId": "1000013343",
                "schoolId": "1847",
                "courseName": "大学物理思维导引（下）",
                "courseMode": 2,
            },
        )
        self.assertEqual(extract_course_meta(None), {})

    def test_build_exam_page_url_and_time_window(self):
        from datetime import datetime
        from modules.zhidao_homework_api import build_exam_page_url, check_time_window

        hw_url = build_exam_page_url(
            "dohomework",
            recruit_id="458533",
            stu_exam_id="S1",
            exam_id="E1",
            course_id="1000013343",
            school_id="1847",
        )
        self.assertEqual(
            parse_exam_page_url(hw_url),
            {
                "kind": "dohomework",
                "recruitId": "458533",
                "stuExamId": "S1",
                "examId": "E1",
                "courseId": "1000013343",
                "schoolId": "1847",
            },
        )
        ex_url = build_exam_page_url(
            "doexam",
            recruit_id="458533",
            stu_exam_id="S2",
            exam_id="E2",
            course_id="1000013343",
            school_id="",
        )
        self.assertEqual(parse_exam_page_url(ex_url)["schoolId"], "0")
        self.assertEqual(parse_exam_page_url(ex_url)["kind"], "doexam")

        now = datetime(2026, 11, 24, 9, 0, 0)
        self.assertEqual(
            check_time_window("2026-10-19 00:00:00", "2026-11-30 23:59:59", now=now),
            (True, ""),
        )
        self.assertEqual(
            check_time_window("2026-12-01 00:00:00", "2026-12-04 23:59:59", now=now),
            (False, "not_started"),
        )
        self.assertEqual(
            check_time_window("2026-10-01 00:00:00", "2026-11-01 00:00:00", now=now),
            (False, "expired"),
        )


if __name__ == "__main__":
    unittest.main()