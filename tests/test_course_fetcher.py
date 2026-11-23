import json
import os
import tempfile
import unittest

from modules.course_fetcher import (
    FetchedCourse,
    append_courses_to_config,
    classify_course_url,
    dedupe_courses,
    extract_courses_from_json,
    save_fetched_courses,
)


class CourseFetcherTests(unittest.TestCase):
    def test_classify_course_url(self):
        self.assertEqual(
            classify_course_url("https://studyvideoh5.zhihuishu.com/stuStudy?recruitAndCourseId=1"),
            "智慧共享课",
        )
        self.assertEqual(
            classify_course_url("https://fusioncourseh5.zhihuishu.com/stuStudy?recruitAndCourseId=2"),
            "融合共享课",
        )
        self.assertEqual(
            classify_course_url("https://wisdom-mooc.zhihuishu.com/study/3"),
            "AI 慕课",
        )
        self.assertEqual(
            classify_course_url("https://ai-smart-course-student-pro.zhihuishu.com/learnPage?courseId=1"),
            "AI 智慧课",
        )
        self.assertEqual(
            classify_course_url("https://hike.zhihuishu.com/aidedteaching/sourceLearning/sourceLearning?courseId=1"),
            "翻转 / 智慧课",
        )
        self.assertIsNone(classify_course_url("https://www.zhihuishu.com/"))

    def test_extract_courses_from_nested_json(self):
        payload = {
            "result": {
                "courseOpenDtos": [
                    {
                        "courseName": "高等数学",
                        "secret": "abc123456",
                        "teacherName": "李老师",
                        "schoolName": "示范大学",
                        "progress": 65,
                    },
                    {
                        "courseName": "融合大学英语",
                        "secret": "fus999999",
                        "typeName": "融合共享课",
                        "progress": "0.5",
                    },
                    {
                        "courseName": "人工智能导论",
                        "studyUrl": "https://wisdom-mooc.zhihuishu.com/play/42",
                        "progress": "80%",
                    },
                ]
            }
        }
        courses = extract_courses_from_json(payload)
        self.assertEqual(len(courses), 3)
        self.assertEqual(
            courses[0].url,
            "https://studyvideoh5.zhihuishu.com/stuStudy?recruitAndCourseId=abc123456",
        )
        self.assertEqual(courses[0].progress, "65%")
        self.assertEqual(
            courses[1].url,
            "https://fusioncourseh5.zhihuishu.com/stuStudy?recruitAndCourseId=fus999999",
        )
        self.assertEqual(courses[1].progress, "50%")
        self.assertEqual(courses[2].course_type, "AI 慕课")

    def test_dedupe_merges_richer_metadata(self):
        url = "https://studyvideoh5.zhihuishu.com/stuStudy?recruitAndCourseId=dup1"
        items = dedupe_courses(
            [
                FetchedCourse(title="智慧共享课", url=url, course_type="智慧共享课"),
                FetchedCourse(
                    title="线性代数",
                    url=url,
                    course_type="智慧共享课",
                    teacher="王教授",
                    progress="30%",
                ),
            ]
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "线性代数")
        self.assertEqual(items[0].teacher, "王教授")
        self.assertEqual(items[0].progress, "30%")

    def test_save_and_append_courses_to_config(self):
        courses = [
            FetchedCourse(
                title="课程A",
                url="https://studyvideoh5.zhihuishu.com/stuStudy?recruitAndCourseId=aaa",
                course_type="智慧共享课",
            ),
            FetchedCourse(
                title="课程B",
                url="https://wisdom-mooc.zhihuishu.com/play/bbb",
                course_type="AI 慕课",
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            json_path = os.path.join(tmp, "data", "courses.json")
            save_fetched_courses(courses, json_path)
            with open(json_path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            self.assertEqual(len(saved), 2)

            ini_path = os.path.join(tmp, "config.ini")
            with open(ini_path, "w", encoding="utf-8") as f:
                f.write(
                    "[course-url]\n"
                    "URL1 = https://studyvideoh5.zhihuishu.com/stuStudy?recruitAndCourseId=aaa\n"
                    "URL2 =\n"
                )
            added = append_courses_to_config(ini_path, courses)
            self.assertEqual(added, 1)
            with open(ini_path, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertIn("URL2 = https://wisdom-mooc.zhihuishu.com/play/bbb", content)
            # Re-running does not duplicate
            self.assertEqual(append_courses_to_config(ini_path, courses), 0)

    def test_extract_hike_ai_fusion_flags_and_skip_recommend_or_ended(self):
        payload = {
            "result": {
                "startInngcourseList": [
                    {
                        "courseId": 123456,
                        "courseName": "中国近现代史纲要",
                        "teacherName": "赵老师",
                        "studyRate": "45%",
                    }
                ],
                "courseOpenDtos": [
                    {
                        "courseName": "融合创新创业",
                        "secret": "fusflag9988",
                        "fusionCourseFlag": 1,
                        "studyProcess": 40,
                    },
                    {
                        "courseTitle": "协议相对链接课",
                        "studyUrl": "//wisdom-mooc.zhihuishu.com/play/777#/",
                    },
                ],
                "aiCourseList": [
                    {
                        "courseId": "501",
                        "classId": "602",
                        "courseName": "AI 智慧线性代数",
                    }
                ],
                "endCourseList": [
                    {"courseId": 88888, "courseName": "已完结课程"}
                ],
                "recommendCourseList": [
                    {"courseName": "推荐课程", "secret": "rec999999"}
                ],
            }
        }
        courses = extract_courses_from_json(
            payload,
            source_url="https://hikeservice.zhihuishu.com/student/course/aided/getMyCourseList",
        )
        urls = [c.url for c in courses]
        self.assertEqual(
            urls,
            [
                "https://hike.zhihuishu.com/aidedteaching/sourceLearning/sourceLearning?courseId=123456",
                "https://fusioncourseh5.zhihuishu.com/stuStudy?recruitAndCourseId=fusflag9988",
                "https://wisdom-mooc.zhihuishu.com/play/777",
                "https://ai-smart-course-student-pro.zhihuishu.com/learnPage?courseId=501&classId=602",
            ],
        )
        self.assertEqual(courses[0].progress, "45%")
        self.assertEqual(courses[1].progress, "40%")

    def test_dedupe_prefers_real_title_over_action_text_and_strips_empty_hash(self):
        items = dedupe_courses(
            [
                FetchedCourse(
                    title="进入学习",
                    url="http://studyvideoh5.zhihuishu.com/stuStudy?recruitAndCourseId=dup2#/",
                    course_type="智慧共享课",
                ),
                FetchedCourse(
                    title="大学物理",
                    url="https://studyvideoh5.zhihuishu.com/stuStudy?recruitAndCourseId=dup2",
                    course_type="智慧共享课",
                    teacher="孙教授",
                ),
            ]
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "大学物理")
        self.assertEqual(items[0].teacher, "孙教授")
        self.assertEqual(
            items[0].url,
            "https://studyvideoh5.zhihuishu.com/stuStudy?recruitAndCourseId=dup2",
        )

    def test_shared_js_scripts_load_from_resources(self):
        from modules.course_fetcher import _load_script

        tab_js = _load_script("zhs_tab_switch.js")
        dom_js = _load_script("zhs_dom_extract.js")
        self.assertIn("clickNextPages", tab_js)
        self.assertIn("walkVue3", dom_js)
        self.assertIn("return { courses, userName }", dom_js)


if __name__ == "__main__":
    unittest.main()
