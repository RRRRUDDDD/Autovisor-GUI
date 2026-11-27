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

    def test_fetch_courses_via_api_collects_paginated_share_ai_and_hike(self):
        import threading
        from urllib.parse import quote
        from requests.cookies import RequestsCookieJar, create_cookie
        from modules import zhidao_crypto
        from modules.course_fetcher import (
            AI_COURSE_LIST_URL,
            HIKE_COURSE_LIST_URL,
            LOGIN_USER_INFO_URL,
            SHARE_COURSE_LIST_URL,
            fetch_courses_via_api,
        )
        from modules.zhs_api import ZhsApiClient

        class _FakeResp:
            def __init__(self, payload, status_code=200):
                self._payload = payload
                self.status_code = status_code
                self.text = json.dumps(payload, ensure_ascii=False)

            def json(self):
                return self._payload

            def raise_for_status(self):
                if self.status_code >= 400:
                    import requests

                    err = requests.HTTPError(f"HTTP {self.status_code}")
                    err.response = self
                    raise err

        class _FakeSession:
            def __init__(self):
                self.cookies = RequestsCookieJar()
                caslogc = quote(json.dumps({"uuid": "u-100", "realName": "李明"}))
                self.cookies.set_cookie(create_cookie("CASLOGC", caslogc, domain=".zhihuishu.com"))
                self.cookies.set_cookie(
                    create_cookie("SESSION", "s1", domain="onlineservice-api.zhihuishu.com")
                )
                self.headers = {}
                self.post_pages = []
                self.hike_params = None

            def get(self, url, params=None, timeout=None, allow_redirects=True):
                if url.startswith(LOGIN_USER_INFO_URL):
                    return _FakeResp({"code": 200, "result": {"uuid": "u-100", "realName": "李明", "schoolName": "示范大学"}})
                if url == HIKE_COURSE_LIST_URL:
                    self.hike_params = params
                    return _FakeResp(
                        {
                            "code": 200,
                            "result": {
                                "startInngcourseList": [
                                    {
                                        "courseId": 11549134,
                                        "courseName": "大学生职业生涯规划",
                                        "teacherName": "钟老师",
                                        "studyRate": "0",
                                    }
                                ]
                            },
                        }
                    )
                return _FakeResp({}, status_code=404)

            def post(self, url, data=None, headers=None, timeout=None):
                plain = json.loads(zhidao_crypto.decrypt(data["secretStr"], key=zhidao_crypto.HOME_KEY))
                if url == SHARE_COURSE_LIST_URL:
                    page_no = plain["pageNo"]
                    self.post_pages.append(page_no)
                    if page_no == 1:
                        return _FakeResp(
                            {
                                "code": 200,
                                "result": {
                                    "totalCount": 6,
                                    "courseOpenDtos": [
                                        {
                                            "courseName": f"共享课{i}",
                                            "secret": f"sec000{i}",
                                            "teacherName": "姜老师",
                                            "schoolName": "示范大学",
                                            "progress": "50%",
                                        }
                                        for i in range(1, 6)
                                    ],
                                },
                            }
                        )
                    return _FakeResp(
                        {
                            "code": 200,
                            "result": {
                                "totalCount": 6,
                                "courseOpenDtos": [
                                    {
                                        "courseName": "融合共享课6",
                                        "secret": "fus0006",
                                        "fusionCourseFlag": 1,
                                        "progress": "10%",
                                    }
                                ],
                            },
                        }
                    )
                if url == AI_COURSE_LIST_URL:
                    return _FakeResp(
                        {
                            "status": "200",
                            "msg": "请求成功",
                            "rt": [
                                {
                                    "courseId": "9001",
                                    "classId": "8002",
                                    "secret": "ignored_secret_for_ai",
                                    "courseName": "AI 智慧微积分",
                                    "teacherName": "周老师",
                                }
                            ],
                        }
                    )
                return _FakeResp({}, status_code=404)

        client = ZhsApiClient([])
        client.session = _FakeSession()
        client.lock = threading.RLock()

        res = fetch_courses_via_api([], client=client)
        self.assertTrue(res.session_valid)
        self.assertEqual(res.display_name, "李明")
        self.assertEqual(res.school_name, "示范大学")
        self.assertEqual(client.session.post_pages, [1, 2])
        self.assertEqual(client.session.hike_params["uuid"], "u-100")
        self.assertEqual(res.sources, {"zhidao": 6, "ai": 1, "hike": 1})
        self.assertEqual(len(res.courses), 8)
        urls = [c.url for c in res.courses]
        self.assertIn(
            "https://studyvideoh5.zhihuishu.com/stuStudy?recruitAndCourseId=sec0001",
            urls,
        )
        self.assertIn(
            "https://fusioncourseh5.zhihuishu.com/stuStudy?recruitAndCourseId=fus0006",
            urls,
        )
        self.assertIn(
            "https://ai-smart-course-student-pro.zhihuishu.com/learnPage?courseId=9001&classId=8002",
            urls,
        )
        self.assertIn(
            "https://hike.zhihuishu.com/aidedteaching/sourceLearning/sourceLearning?courseId=11549134",
            urls,
        )

    def test_extract_shared_course_not_misclassified_as_ai_on_detail_or_main_hints(self):
        payload = {
            "mainCourseList": [
                {
                    "courseId": 1001,
                    "recruitId": 2002,
                    "secret": "shareSecret999",
                    "courseName": "高等数学（上）",
                }
            ],
            "aiCourseList": [
                {
                    "courseId": 3001,
                    "classId": 4002,
                    "secret": "aiSecretShouldNotOverride",
                    "courseName": "AI 智慧微积分",
                }
            ],
            "otherCourses": [
                {
                    "courseId": 5001,
                    "classId": 6002,
                    "typeName": "AI 课程",
                    "courseName": "AI 课程实战",
                },
                {
                    "courseId": 7001,
                    "recruitId": 8002,
                    "secret": "trainSecret888",
                    "typeName": "training",
                    "courseName": "工程训练",
                },
            ],
        }
        courses = extract_courses_from_json(
            payload,
            source_url="https://onlineservice-api.zhihuishu.com/courseDetail",
        )
        by_title = {c.title: c for c in courses}
        self.assertEqual(
            by_title["高等数学（上）"].url,
            "https://studyvideoh5.zhihuishu.com/stuStudy?recruitAndCourseId=shareSecret999",
        )
        self.assertEqual(
            by_title["工程训练"].url,
            "https://studyvideoh5.zhihuishu.com/stuStudy?recruitAndCourseId=trainSecret888",
        )
        self.assertEqual(
            by_title["AI 智慧微积分"].url,
            "https://ai-smart-course-student-pro.zhihuishu.com/learnPage?courseId=3001&classId=4002",
        )
        self.assertEqual(
            by_title["AI 课程实战"].url,
            "https://ai-smart-course-student-pro.zhihuishu.com/learnPage?courseId=5001&classId=6002",
        )

        # queryStudentAICourseList 来源 URL 下的 AI 课程也能识别
        ai_from_source = extract_courses_from_json(
            {"rt": [{"courseId": "9101", "classId": "9202", "courseName": "接口来源AI课"}]},
            source_url="https://onlineservice-api.zhihuishu.com/gateway/t/v1/student/queryStudentAICourseList",
        )
        self.assertEqual(len(ai_from_source), 1)
        self.assertEqual(
            ai_from_source[0].url,
            "https://ai-smart-course-student-pro.zhihuishu.com/learnPage?courseId=9101&classId=9202",
        )

    def test_fetch_courses_via_api_refreshes_sso_on_expired_session_and_recovers_hike_and_bad_total(self):
        import threading
        from requests.cookies import RequestsCookieJar, create_cookie
        from modules.course_fetcher import (
            AI_COURSE_LIST_URL,
            HIKE_COURSE_LIST_URL,
            HIKE_SERVICE_SSO_URL,
            LOGIN_USER_INFO_URL,
            ONLINE_SERVICE_SSO_URL,
            SHARE_COURSE_LIST_URL,
            fetch_courses_via_api,
        )
        from modules.zhs_api import ZhsApiClient

        class _FakeResp:
            def __init__(self, payload=None, status_code=200, text="", raise_json=False):
                self._payload = payload
                self.status_code = status_code
                self.text = text or (json.dumps(payload, ensure_ascii=False) if payload is not None else "")
                self._raise_json = raise_json
                self.headers = {}

            def json(self):
                if self._raise_json:
                    raise ValueError("html login page")
                return self._payload

            def raise_for_status(self):
                if self.status_code >= 400:
                    import requests

                    err = requests.HTTPError(f"HTTP {self.status_code}")
                    err.response = self
                    raise err

        class _ExpiredOnlineThenRefreshSession:
            def __init__(self):
                self.cookies = RequestsCookieJar()
                # 存在已过期的 onlineservice SESSION 且 CASLOGC 不含 uuid
                self.cookies.set_cookie(
                    create_cookie("SESSION", "expired-online", domain="onlineservice-api.zhihuishu.com")
                )
                self.cookies.set_cookie(
                    create_cookie("CASTGC", "TGT-valid", domain=".zhihuishu.com")
                )
                self.headers = {}
                self.online_refreshed = False
                self.hike_refreshed = False
                self.user_info_calls = 0
                self.hike_calls = 0

            def get(self, url, params=None, timeout=None, allow_redirects=True):
                if url.startswith(LOGIN_USER_INFO_URL):
                    self.user_info_calls += 1
                    if not self.online_refreshed:
                        return _FakeResp({"code": 401, "result": None}, status_code=401)
                    return _FakeResp(
                        {
                            "code": 200,
                            "result": {
                                "uuid": "u-refreshed",
                                "realName": "刷新用户",
                                "schoolName": "工大",
                            },
                        }
                    )
                if url == HIKE_COURSE_LIST_URL:
                    self.hike_calls += 1
                    if not self.hike_refreshed:
                        # 模拟 302 跳转登录页返回 200 HTML
                        return _FakeResp(None, status_code=200, text="<html>login</html>", raise_json=True)
                    return _FakeResp(
                        {
                            "code": 200,
                            "result": {
                                "startInngcourseList": [
                                    {"courseId": 5566, "courseName": "刷新后拉到的翻转课"}
                                ]
                            },
                        }
                    )
                return _FakeResp({}, status_code=200)

            def post(self, url, data=None, headers=None, timeout=None):
                if url == SHARE_COURSE_LIST_URL:
                    return _FakeResp(
                        {
                            "code": 200,
                            "result": {
                                "totalCount": "bad",
                                "courseOpenDtos": [
                                    {"courseName": "第一页共享课", "secret": "page1sec99"}
                                ],
                            },
                        }
                    )
                if url == AI_COURSE_LIST_URL:
                    return _FakeResp({"status": "200", "rt": []})
                return _FakeResp({}, status_code=404)

        client = ZhsApiClient([])
        fake_sess = _ExpiredOnlineThenRefreshSession()
        client.session = fake_sess
        client.lock = threading.RLock()
        sso_services = []

        def _fake_ensure_sso(service: str) -> bool:
            sso_services.append(service)
            if service == ONLINE_SERVICE_SSO_URL:
                fake_sess.online_refreshed = True
                fake_sess.cookies.set_cookie(
                    create_cookie("SESSION", "fresh-online", domain="onlineservice-api.zhihuishu.com")
                )
            elif service == HIKE_SERVICE_SSO_URL:
                fake_sess.hike_refreshed = True
                fake_sess.cookies.set_cookie(
                    create_cookie("SESSION", "fresh-hike", domain="hikeservice.zhihuishu.com")
                )
            return True

        client.ensure_service_sso = _fake_ensure_sso

        res = fetch_courses_via_api([], client=client)
        self.assertTrue(res.session_valid)
        self.assertEqual(res.display_name, "刷新用户")
        self.assertEqual(res.school_name, "工大")
        self.assertEqual(fake_sess.user_info_calls, 2)
        self.assertEqual(fake_sess.hike_calls, 2)
        self.assertEqual(sso_services, [ONLINE_SERVICE_SSO_URL, HIKE_SERVICE_SSO_URL])
        self.assertEqual(res.sources["zhidao"], 1)
        self.assertEqual(res.sources["hike"], 1)
        urls = [c.url for c in res.courses]
        self.assertIn(
            "https://studyvideoh5.zhihuishu.com/stuStudy?recruitAndCourseId=page1sec99",
            urls,
        )
        self.assertIn(
            "https://hike.zhihuishu.com/aidedteaching/sourceLearning/sourceLearning?courseId=5566",
            urls,
        )

    def test_fetch_courses_via_api_marks_valid_when_only_hike_succeeds_and_expired_on_all_fail(self):
        import threading
        from urllib.parse import quote
        from requests.cookies import RequestsCookieJar, create_cookie
        from modules.course_fetcher import (
            HIKE_COURSE_LIST_URL,
            LOGIN_USER_INFO_URL,
            fetch_courses_via_api,
        )
        from modules.zhs_api import ZhsApiClient

        class _FakeResp:
            def __init__(self, payload=None, status_code=200, text="", raise_json=False):
                self._payload = payload
                self.status_code = status_code
                self.text = text or (json.dumps(payload, ensure_ascii=False) if payload is not None else "")
                self._raise_json = raise_json
                self.headers = {}

            def json(self):
                if self._raise_json:
                    raise ValueError("not json")
                return self._payload

            def raise_for_status(self):
                if self.status_code >= 400:
                    import requests

                    err = requests.HTTPError(f"HTTP {self.status_code}")
                    err.response = self
                    raise err

        class _OnlyHikeSession:
            def __init__(self, hike_ok: bool):
                self.hike_ok = hike_ok
                self.cookies = RequestsCookieJar()
                caslogc = quote(json.dumps({"uuid": "u-hike"}))
                self.cookies.set_cookie(create_cookie("CASLOGC", caslogc, domain=".zhihuishu.com"))
                self.headers = {}

            def get(self, url, params=None, timeout=None, allow_redirects=True):
                if url.startswith(LOGIN_USER_INFO_URL):
                    return _FakeResp({"code": 403, "result": None}, status_code=200)
                if url == HIKE_COURSE_LIST_URL:
                    if not self.hike_ok:
                        return _FakeResp(None, status_code=200, text="<html>login</html>", raise_json=True)
                    return _FakeResp(
                        {
                            "code": 200,
                            "result": {
                                "startInngcourseList": [
                                    {"courseId": 123, "courseName": "仅Hike有效的校内课"}
                                ]
                            },
                        }
                    )
                return _FakeResp({}, status_code=200)

            def post(self, url, data=None, headers=None, timeout=None):
                return _FakeResp({"code": 401, "message": "Forbidden"}, status_code=401)

        client_ok = ZhsApiClient([])
        client_ok.session = _OnlyHikeSession(hike_ok=True)
        client_ok.lock = threading.RLock()
        res_ok = fetch_courses_via_api([], client=client_ok)
        self.assertTrue(res_ok.session_valid)
        self.assertEqual(res_ok.sources["zhidao"], 0)
        self.assertEqual(res_ok.sources["ai"], 0)
        self.assertEqual(res_ok.sources["hike"], 1)

        client_exp = ZhsApiClient([])
        client_exp.session = _OnlyHikeSession(hike_ok=False)
        client_exp.lock = threading.RLock()
        res_exp = fetch_courses_via_api([], client=client_exp)
        self.assertFalse(res_exp.session_valid)
        self.assertEqual(res_exp.sources["hike"], 0)

    def test_fetch_courses_via_api_uses_compact_retry_and_timeout_budget(self):
        from unittest.mock import patch
        from modules.course_fetcher import _API_FETCH_TIMEOUT, fetch_courses_via_api
        from modules.zhs_api import ZhsApiClient

        captured_init = {}
        timeouts = []

        class _BudgetClient(ZhsApiClient):
            def __init__(self, cookies, proxy="", user_agent="", *, total_retries=3, timeout=20):
                captured_init["total_retries"] = total_retries
                captured_init["timeout"] = timeout
                super().__init__(
                    cookies,
                    proxy=proxy,
                    user_agent=user_agent,
                    total_retries=total_retries,
                    timeout=timeout,
                )

                class _TimeoutRecordingSession:
                    def __init__(self, cookies_jar):
                        self.cookies = cookies_jar
                        self.headers = {}

                    def get(self, url, params=None, timeout=None, allow_redirects=True):
                        timeouts.append((url, timeout))
                        return type(
                            "R",
                            (),
                            {
                                "status_code": 200,
                                "headers": {},
                                "text": "{}",
                                "json": lambda self: {"code": 200, "result": {"uuid": "u-b"}},
                            },
                        )()

                    def post(self, url, data=None, headers=None, timeout=None):
                        timeouts.append((url, timeout))
                        return type(
                            "R",
                            (),
                            {
                                "status_code": 200,
                                "headers": {},
                                "text": "{}",
                                "raise_for_status": lambda self: None,
                                "json": lambda self: {
                                    "code": 200,
                                    "status": "200",
                                    "result": {"totalCount": 0, "courseOpenDtos": []},
                                    "rt": [],
                                },
                            },
                        )()

                    def close(self):
                        pass

                self.session = _TimeoutRecordingSession(self.session.cookies)

        with patch("modules.zhs_api.ZhsApiClient", _BudgetClient):
            fetch_courses_via_api(
                [{"name": "CASTGC", "value": "tgt-1", "domain": ".zhihuishu.com"}]
            )

        self.assertEqual(captured_init, {"total_retries": 1, "timeout": _API_FETCH_TIMEOUT})
        self.assertEqual(_API_FETCH_TIMEOUT, 8)
        self.assertTrue(timeouts)
        for _url, t in timeouts:
            self.assertEqual(t, 8)

    def test_fetch_courses_via_api_does_not_use_shared_course_school_as_user_school(self):
        import threading
        from requests.cookies import RequestsCookieJar
        from modules.course_fetcher import (
            AI_COURSE_LIST_URL,
            LOGIN_USER_INFO_URL,
            SHARE_COURSE_LIST_URL,
            fetch_courses_via_api,
        )
        from modules.zhs_api import ZhsApiClient

        class _Resp:
            def __init__(self, payload):
                self._payload = payload
                self.status_code = 200
                self.headers = {}
                self.text = json.dumps(payload, ensure_ascii=False)

            def json(self):
                return self._payload

            def raise_for_status(self):
                return None

        class _Sess:
            def __init__(self):
                self.cookies = RequestsCookieJar()
                self.headers = {}

            def get(self, url, params=None, timeout=None, allow_redirects=True):
                if url.startswith(LOGIN_USER_INFO_URL):
                    # 用户信息未返回 schoolName
                    return _Resp({"code": 200, "result": {"uuid": "u-no-school", "realName": "赵六"}})
                return _Resp({"code": 200, "result": {}})

            def post(self, url, data=None, headers=None, timeout=None):
                if url == SHARE_COURSE_LIST_URL:
                    return _Resp(
                        {
                            "code": 200,
                            "result": {
                                "totalCount": 1,
                                "courseOpenDtos": [
                                    {
                                        "courseName": "外校共享通识课",
                                        "secret": "extsec1234",
                                        "schoolName": "清华大学",
                                    }
                                ],
                            },
                        }
                    )
                if url == AI_COURSE_LIST_URL:
                    return _Resp({"status": "200", "rt": []})
                return _Resp({"code": 200})

        client = ZhsApiClient.__new__(ZhsApiClient)
        client.session = _Sess()
        client.lock = threading.RLock()
        client.timeout = 8
        client.proxy = ""
        client.ensure_service_sso = lambda _svc: True

        res = fetch_courses_via_api([], client=client)
        self.assertTrue(res.session_valid)
        self.assertEqual(res.display_name, "赵六")
        # 绝不能把共享课开课学校（清华大学）当成学生本人的学校
        self.assertEqual(res.school_name, "")
        self.assertEqual(len(res.courses), 1)
        self.assertEqual(res.courses[0].school, "清华大学")

    def test_extract_courses_and_profile_ignores_course_endpoint_school(self):
        from modules.course_fetcher import extract_courses_and_profile_from_json

        course_resp = {
            "code": 200,
            "result": {
                "schoolInfo": {"schoolName": "错误的外校开课单位"},
                "courseOpenDtos": [
                    {
                        "courseName": "共享通识课",
                        "secret": "sec123456",
                        "schoolName": "清华大学",
                    }
                ],
            },
        }
        courses, name, school = extract_courses_and_profile_from_json(
            course_resp,
            source_url="https://onlineservice-api.zhihuishu.com/gateway/t/v1/student/course/share/queryShareCourseInfo",
        )
        self.assertEqual(len(courses), 1)
        self.assertEqual(name, "")
        self.assertEqual(school, "")

        user_resp = {
            "code": 200,
            "result": {"realName": "王五", "schoolName": "真实就读大学"},
        }
        _, name2, school2 = extract_courses_and_profile_from_json(
            user_resp,
            source_url="https://onlineservice-api.zhihuishu.com/gateway/f/v1/login/getLoginUserInfo",
        )
        self.assertEqual(name2, "王五")
        self.assertEqual(school2, "真实就读大学")

    def test_extract_courses_from_captured_merges_responses_dom_and_validates_host(self):
        from modules.course_fetcher import extract_courses_from_captured

        responses = [
            {
                "url": "https://onlineservice-api.zhihuishu.com/gateway/f/v1/login/getLoginUserInfo",
                "body": json.dumps(
                    {
                        "code": 200,
                        "result": {"realName": "张三", "schoolName": "示范大学"},
                    }
                ),
            },
            {
                "url": "https://onlineservice-api.zhihuishu.com/gateway/t/v1/student/course/share/queryShareCourseInfo",
                "body": json.dumps(
                    {
                        "result": {
                            "courseOpenDtos": [
                                {
                                    "courseName": "进入学习",
                                    "secret": "abcdef1234",
                                    "progress": 50,
                                }
                            ]
                        }
                    }
                ),
            },
        ]
        dom_courses = [
            {
                "url": "http://studyvideoh5.zhihuishu.com/stuStudy?recruitAndCourseId=abcdef1234#/",
                "title": "数据结构与算法",
                "teacher": "刘教授",
            },
            {
                "url": "https://evil.example.com/?x=studyvideoh5.zhihuishu.com",
                "title": "钓鱼链接应被过滤",
            },
        ]
        courses, name, school = extract_courses_from_captured(
            responses, dom_courses, dom_user_name="备选名称"
        )
        self.assertEqual(name, "张三")
        self.assertEqual(school, "示范大学")
        self.assertEqual(len(courses), 1)
        self.assertEqual(courses[0].title, "数据结构与算法")
        self.assertEqual(courses[0].teacher, "刘教授")
        self.assertEqual(courses[0].progress, "50%")
        self.assertEqual(
            courses[0].url,
            "https://studyvideoh5.zhihuishu.com/stuStudy?recruitAndCourseId=abcdef1234",
        )

    def test_append_courses_to_config_normalizes_urls_before_dedupe(self):
        courses = [
            FetchedCourse(
                title="已存在课程不同写法",
                url="http://studyvideoh5.zhihuishu.com/stuStudy?recruitAndCourseId=dup1#/",
                course_type="智慧共享课",
            ),
            FetchedCourse(
                title="新课程",
                url="//wisdom-mooc.zhihuishu.com/play/new1#/",
                course_type="AI 慕课",
            ),
            FetchedCourse(
                title="新课程重复",
                url="https://wisdom-mooc.zhihuishu.com/play/new1",
                course_type="AI 慕课",
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            ini_path = os.path.join(tmp, "config.ini")
            with open(ini_path, "w", encoding="utf-8") as f:
                f.write(
                    "[course-url]\n"
                    "URL1 = https://studyvideoh5.zhihuishu.com/stuStudy?recruitAndCourseId=dup1\n"
                )
            added = append_courses_to_config(ini_path, courses)
            self.assertEqual(added, 1)
            with open(ini_path, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertIn("URL2 = https://wisdom-mooc.zhihuishu.com/play/new1", content)
            self.assertNotIn("URL3 =", content)

    def test_fetch_courses_from_page_tries_api_first_without_waiting(self):
        import asyncio
        from unittest.mock import patch
        from modules.course_fetcher import ApiCourseFetchResult, fetch_courses_from_page

        class _FakeContext:
            async def cookies(self):
                return [{"name": "CASTGC", "value": "tgt", "domain": ".zhihuishu.com"}]

        class _FakePage:
            def __init__(self):
                self.context = _FakeContext()
                self.goto_called = False
                self.wait_called = False

            def on(self, *_args, **_kwargs):
                pass

            def remove_listener(self, *_args, **_kwargs):
                pass

            async def goto(self, *_args, **_kwargs):
                self.goto_called = True

            async def wait_for_timeout(self, *_args, **_kwargs):
                self.wait_called = True

        page = _FakePage()
        fake_res = ApiCourseFetchResult(
            courses=[
                FetchedCourse(
                    title="高等数学",
                    url="https://studyvideoh5.zhihuishu.com/stuStudy?recruitAndCourseId=fast1",
                    course_type="智慧共享课",
                )
            ],
            session_valid=True,
        )
        with patch("modules.course_fetcher.fetch_courses_via_api", return_value=fake_res) as m_api:
            result = asyncio.run(fetch_courses_from_page(page, wait_seconds=5.0))

        m_api.assert_called_once()
        self.assertFalse(page.goto_called)
        self.assertFalse(page.wait_called)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].title, "高等数学")

    def test_classify_wenda_study_resource_and_dedupe_hike_preferring_file_id(self):
        wenda_url = "https://wenda.zhihuishu.com/stu/courseInfo/studyResource?courseId=11559973&polymas=1"
        bare_hike = "https://hike.zhihuishu.com/aidedteaching/sourceLearning/sourceLearning?courseId=11559973"
        full_hike = "https://hike.zhihuishu.com/aidedteaching/sourceLearning/sourceLearning?courseId=11559973&fileId=35338435"

        self.assertEqual(classify_course_url(wenda_url), "翻转 / 智慧课")
        self.assertIsNone(classify_course_url("https://wenda.zhihuishu.com/question/123"))

        merged = dedupe_courses(
            [
                FetchedCourse(title="2026秋大学物理B-2_802", url=wenda_url, course_type="翻转 / 智慧课", teacher="郭娟"),
                FetchedCourse(title="翻转 / 智慧课", url=bare_hike, course_type="翻转 / 智慧课"),
                FetchedCourse(title="2026秋大学物理B-2_802", url=full_hike, course_type="翻转 / 智慧课", progress="20%"),
            ]
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].url, full_hike)
        self.assertEqual(merged[0].teacher, "郭娟")
        self.assertEqual(merged[0].progress, "20%")

    def test_fetch_courses_via_api_enriches_hike_course_with_file_id(self):
        import threading
        from urllib.parse import quote
        from requests.cookies import RequestsCookieJar, create_cookie
        from modules.course_fetcher import (
            AI_COURSE_LIST_URL,
            HIKE_COURSE_LIST_URL,
            LOGIN_USER_INFO_URL,
            SHARE_COURSE_LIST_URL,
            fetch_courses_via_api,
        )
        from modules.hike_navigation import HIKE_RESOURCE_MENU_TREE_URL
        from modules.zhs_api import ZhsApiClient

        class _FakeResp:
            def __init__(self, payload, status_code=200):
                self._payload = payload
                self.status_code = status_code

            def json(self):
                return self._payload

        class _HikeTreeSession:
            def __init__(self):
                self.cookies = RequestsCookieJar()
                self.cookies.set_cookie(
                    create_cookie("CASLOGC", quote(json.dumps({"uuid": "u-phys"})), domain=".zhihuishu.com")
                )

            def get(self, url, params=None, headers=None, timeout=None):
                if url == LOGIN_USER_INFO_URL:
                    return _FakeResp({"code": 200, "result": {"uuid": "u-phys", "realName": "测试生"}})
                if url == HIKE_COURSE_LIST_URL:
                    return _FakeResp(
                        {
                            "code": 200,
                            "result": {
                                "startInngcourseList": [
                                    {"courseId": 11559973, "courseName": "2026秋大学物理B-2_802", "studyRate": "20"}
                                ]
                            },
                        }
                    )
                if url == HIKE_RESOURCE_MENU_TREE_URL:
                    return _FakeResp(
                        {
                            "status": 200,
                            "rt": [
                                {
                                    "id": 1,
                                    "type": 0,
                                    "childList": [
                                        {"id": 999, "type": 1, "dataType": 1, "name": "1.静电场.pdf"},
                                        {
                                            "id": 35338435,
                                            "type": 1,
                                            "dataType": 3,
                                            "name": "矢量运算与常用坐标系",
                                            "totalTime": 769,
                                            "studyTime": 0,
                                        },
                                    ],
                                }
                            ],
                        }
                    )
                return _FakeResp({}, status_code=404)

            def post(self, url, data=None, headers=None, timeout=None):
                if url == SHARE_COURSE_LIST_URL:
                    return _FakeResp({"code": 200, "result": {"totalCount": 0, "courseOpenDtos": []}})
                if url == AI_COURSE_LIST_URL:
                    return _FakeResp({"status": "200", "rt": []})
                return _FakeResp({}, status_code=404)

        client = ZhsApiClient([])
        client.session = _HikeTreeSession()
        client.lock = threading.RLock()

        res = fetch_courses_via_api([], client=client)
        self.assertEqual(res.sources["hike"], 1)
        self.assertEqual(len(res.courses), 1)
        self.assertEqual(
            res.courses[0].url,
            "https://hike.zhihuishu.com/aidedteaching/sourceLearning/sourceLearning?courseId=11559973&fileId=35338435",
        )


if __name__ == "__main__":
    unittest.main()
