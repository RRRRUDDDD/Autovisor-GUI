import json
import unittest

from modules.zhidao_homework_api import (
    LOOK_HOMEWORK,
    SAVE_COURSE_TRAN,
    SAVE_STUDENT_ANSWER,
    ZhidaoHomeworkApi,
    build_student_answer_item,
    correct_option_ids,
    extract_exam_questions,
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

    def test_normalize_question_extracts_fields(self):
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

        self.assertEqual(extract_exam_questions(None), [])
        self.assertEqual(extract_exam_questions({"rt": {"examBase": {}}}), [])


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


class AnswerFormatTests(unittest.TestCase):
    def test_format_student_answer(self):
        self.assertEqual(format_student_answer(2, [11, 12]), "11,12")
        self.assertEqual(format_student_answer(3, ["甲", "乙"]), "甲/乙")
        self.assertEqual(format_student_answer(1, [7]), "7")

    def test_build_student_answer_item(self):
        item = build_student_answer_item(
            eid="e1", question_type=2, answer="1,2", exam_id="EX", exam_type=""
        )
        self.assertEqual(
            item,
            {
                "questionId": "e1",
                "examId": "EX",
                "questionType": 2,
                "answer": "1,2",
                "examType": "",
                "fromType": 3,
            },
        )

    def test_extract_score(self):
        self.assertEqual(extract_score({"rt": {"score": "87.5"}}), 87.5)
        self.assertEqual(extract_score({"score": 60}), 60.0)
        self.assertIsNone(extract_score({"rt": {}}))
        self.assertIsNone(extract_score(None))


class HomeworkApiTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.api = ZhidaoHomeworkApi(self.client)

    def _last(self):
        return self.client.calls[-1]

    def test_all_calls_ensure_sso_and_use_homework_strategy(self):
        self.api.get_student_homework(2)
        self.assertEqual(self._last()["strategy"], "homework")
        self.assertEqual(self._last()["data"], {"flag": 2})
        self.assertEqual(self.client.sso_calls, 1)

        self.api.do_homework(
            recruit_id=1, course_id=2, homework_id=3, stu_exam_id=4, school_id=5
        )
        self.assertEqual(self._last()["data"]["schoolId"], "5")
        self.assertEqual(self._last()["data"]["deviceId"], "")

        self.api.save_course_tran(
            recruit_id=1, course_id=2, homework_id=3, stu_exam_id=4
        )
        self.assertEqual(self._last()["url"], SAVE_COURSE_TRAN)
        self.assertEqual(self._last()["data"]["state"], 0)

        self.api.look_homework(recruit_id=1, course_id=2, homework_id=3, stu_exam_id=4)
        self.assertEqual(self._last()["url"], LOOK_HOMEWORK)

        self.api.get_stu_answer_info(
            recruit_id=1, course_id=2, homework_id=3, stu_exam_id=4
        )
        self.assertEqual(self.client.sso_calls, 5)

    def test_save_student_answer_serializes_item(self):
        item = build_student_answer_item(
            eid="e1", question_type=1, answer="11", exam_id="EX", exam_type=""
        )
        self.api.save_student_answer(
            recruit_id=1, course_id=2, homework_id=3, stu_exam_id=4, answer_item=item
        )
        call = self._last()
        self.assertEqual(call["url"], SAVE_STUDENT_ANSWER)
        decoded = json.loads(call["data"]["stuExamAnswer"])
        self.assertEqual(decoded, [item])
        self.assertEqual(decoded[0]["fromType"], 3)

    def test_submit_achieve_count(self):
        self.api.submit(
            recruit_id=1, course_id=2, homework_id=3, stu_exam_id=4, achieve_count=9
        )
        self.assertEqual(self._last()["data"]["achieveCount"], 9)


if __name__ == "__main__":
    unittest.main()