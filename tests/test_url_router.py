# encoding=utf-8
import unittest

from modules import url_router


class ParseCourseUrlTest(unittest.TestCase):
    def test_recruit_and_course_id(self):
        parsed = url_router.parse_course_url(
            "https://studyvideoh5.zhihuishu.com/stuStudy?recruitAndCourseId=abc123"
        )
        self.assertEqual(parsed, {"kind": "course", "recruitAndCourseId": "abc123"})

    def test_missing_recruit_id_returns_empty(self):
        self.assertEqual(
            url_router.parse_course_url(
                "https://fusioncourseh5.zhihuishu.com/stuStudy?foo=bar"
            ),
            {},
        )


class ParseExamUrlTest(unittest.TestCase):
    def test_dohomework_five_segments(self):
        parsed = url_router.parse_exam_url(
            "https://studentexam-api.zhihuishu.com/webExamList/dohomework/"
            "1001/2002/3003/4004/5005"
        )
        self.assertEqual(
            parsed,
            {
                "kind": "dohomework",
                "recruitId": "1001",
                "stuExamId": "2002",
                "examId": "3003",
                "courseId": "4004",
                "schoolId": "5005",
            },
        )

    def test_doexam_hash_route_with_extra_segments(self):
        parsed = url_router.parse_exam_url(
            "https://exam.zhihuishu.com/#/webExamList/doexam/"
            "11/22/33/44/55?requestId=xyz"
        )
        self.assertEqual(parsed["kind"], "doexam")
        self.assertEqual(parsed["examId"], "33")
        self.assertEqual(parsed["schoolId"], "55")

    def test_percent_encoded_hash(self):
        parsed = url_router.parse_exam_url(
            "https://exam.zhihuishu.com/#%2FwebExamList%2Fdohomework%2F1%2F2%2F3%2F4%2F5"
        )
        self.assertEqual(parsed["kind"], "dohomework")
        self.assertEqual(parsed["recruitId"], "1")

    def test_insufficient_segments_returns_empty(self):
        self.assertEqual(
            url_router.parse_exam_url(
                "https://exam.zhihuishu.com/webExamList/dohomework/1/2/3"
            ),
            {},
        )


class ParseLearnAndKnowledgeTest(unittest.TestCase):
    def test_learn_page_path(self):
        parsed = url_router.parse_learn_page(
            "https://ai-smart-course-student-pro.zhihuishu.com/learnPage/111/222/333"
        )
        self.assertEqual(
            parsed,
            {
                "kind": "learnPage",
                "courseId": "111",
                "nodeUid": "222",
                "classId": "333",
            },
        )

    def test_learn_page_query(self):
        parsed = url_router.parse_learn_page(
            "https://ai-smart-course-student-pro.zhihuishu.com/learnPage"
            "?courseId=111&classId=333&nodeUid=222"
        )
        self.assertEqual(parsed["courseId"], "111")
        self.assertEqual(parsed["classId"], "333")
        self.assertEqual(parsed["nodeUid"], "222")

    def test_learn_page_query_requires_ids(self):
        self.assertEqual(
            url_router.parse_learn_page(
                "https://ai-smart-course-student-pro.zhihuishu.com/learnPage?courseId=111"
            ),
            {},
        )

    def test_knowledge_study_path_and_query(self):
        self.assertEqual(
            url_router.parse_knowledge_study(
                "https://ai-smart-course-student-pro.zhihuishu.com/knowledgeStudy/777/888"
            ),
            {"kind": "knowledgeStudy", "courseId": "777", "classId": "888"},
        )
        self.assertEqual(
            url_router.parse_knowledge_study(
                "https://ai-smart-course-student-pro.zhihuishu.com/knowledgeStudy"
                "?courseId=777&classId=888"
            ),
            {"kind": "knowledgeStudy", "courseId": "777", "classId": "888"},
        )

    def test_test_detail(self):
        self.assertEqual(
            url_router.parse_test_detail(
                "https://ai-smart-course-student-pro.zhihuishu.com/testDetail/1/2/3/4"
            ),
            {
                "kind": "testDetail",
                "courseId": "1",
                "classId": "2",
                "examTestId": "3",
                "examPaperId": "4",
            },
        )


class ParseUrlDispatchTest(unittest.TestCase):
    def test_priority_prefers_task_over_course_query(self):
        url = (
            "https://ai-smart-course-student-pro.zhihuishu.com/knowledgeStudy/1/2"
            "?recruitAndCourseId=abc"
        )
        self.assertEqual(url_router.parse_url(url)["kind"], "knowledgeStudy")

    def test_unknown_and_empty(self):
        for value in ("", None, "not-a-url", "https://example.com/whatever"):
            self.assertEqual(url_router.parse_url(value), {})

    def test_task_kind_and_is_task_url(self):
        self.assertEqual(
            url_router.task_kind("https://studyvideoh5.zhihuishu.com/stuStudy?recruitAndCourseId=x"),
            "course",
        )
        self.assertEqual(url_router.task_kind("https://example.com/none"), "")
        self.assertTrue(
            url_router.is_task_url(
                "https://exam.zhihuishu.com/webExamList/doexam/1/2/3/4/5"
            )
        )
        self.assertFalse(url_router.is_task_url("https://example.com/none"))


class ExamPageDelegationTest(unittest.TestCase):
    def test_zhidao_homework_api_delegates_to_router(self):
        from modules.zhidao_homework_api import parse_exam_page_url

        url = (
            "https://studentexam-api.zhihuishu.com/webExamList/dohomework/"
            "1001/2002/3003/4004/5005"
        )
        self.assertEqual(parse_exam_page_url(url), url_router.parse_exam_url(url))
        self.assertEqual(parse_exam_page_url("https://example.com/none"), {})


if __name__ == "__main__":
    unittest.main()