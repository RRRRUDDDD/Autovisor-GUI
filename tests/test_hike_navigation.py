# encoding=utf-8
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from modules.hike_navigation import (
    HIKE_LAST_STUDY_FILE_URL,
    HIKE_RESOURCE_MENU_TREE_URL,
    HIKE_STU_VIEW_FILE_URL,
    STUDYRESOURCES_SSO_URL,
    build_hike_play_url,
    build_hike_resource_url,
    collect_hike_document_nodes,
    complete_hike_document_nodes,
    fetch_hike_file_id_via_api,
    is_hike_404_page,
    mark_hike_documents_read_via_api,
    needs_hike_file_id_resolution,
    parse_hike_course_url,
    pick_hike_file_id_from_last_study,
    pick_hike_file_id_from_tree,
    resolve_hike_playback_url,
)
from modules.lesson_navigation import HIKE_CATALOG, wait_for_lesson_active


class _FakeResp:
    def __init__(self, payload=None, status_code=200, raise_json=False):
        self._payload = payload
        self.status_code = status_code
        self._raise_json = raise_json

    def json(self):
        if self._raise_json:
            raise ValueError("not json")
        return self._payload


class HikeUrlParsingTests(unittest.TestCase):
    def test_parse_and_detect_hike_urls(self):
        bare = "https://hike.zhihuishu.com/aidedteaching/sourceLearning/sourceLearning?courseId=11559973"
        full = "https://hike.zhihuishu.com/aidedteaching/sourceLearning/sourceLearning?courseId=11559973&fileId=35338435"
        wenda = "https://wenda.zhihuishu.com/stu/courseInfo/studyResource?courseId=11559973&polymas=1"
        other = "https://studyvideoh5.zhihuishu.com/stuStudy?recruitAndCourseId=abc"
        wenda_qa = "https://wenda.zhihuishu.com/question/detail?id=999"

        self.assertEqual(
            parse_hike_course_url(bare),
            {"courseId": "11559973", "fileId": "", "taskId": "", "isResourcePage": False},
        )
        self.assertTrue(needs_hike_file_id_resolution(bare))

        self.assertEqual(
            parse_hike_course_url(full),
            {
                "courseId": "11559973",
                "fileId": "35338435",
                "taskId": "",
                "isResourcePage": False,
            },
        )
        self.assertFalse(needs_hike_file_id_resolution(full))

        self.assertEqual(
            parse_hike_course_url(wenda),
            {"courseId": "11559973", "fileId": "", "taskId": "", "isResourcePage": True},
        )
        self.assertTrue(needs_hike_file_id_resolution(wenda))

        self.assertEqual(parse_hike_course_url(other), {})
        self.assertFalse(needs_hike_file_id_resolution(other))
        self.assertEqual(parse_hike_course_url(wenda_qa), {})
        self.assertFalse(needs_hike_file_id_resolution(wenda_qa))

    def test_build_urls(self):
        self.assertEqual(
            build_hike_play_url("11559973", "35338435"),
            "https://hike.zhihuishu.com/aidedteaching/sourceLearning/sourceLearning?courseId=11559973&fileId=35338435",
        )
        self.assertEqual(
            build_hike_play_url("11559973"),
            "https://hike.zhihuishu.com/aidedteaching/sourceLearning/sourceLearning?courseId=11559973",
        )
        self.assertEqual(
            build_hike_resource_url("11559973"),
            "https://wenda.zhihuishu.com/stu/courseInfo/studyResource?courseId=11559973&polymas=1",
        )


class HikeTreePickerTests(unittest.TestCase):
    def test_prefers_first_unfinished_video_over_docs_and_finished_videos(self):
        tree = [
            {
                "id": 10,
                "type": 0,
                "name": "课程ppt",
                "childList": [
                    {"id": 1001, "type": 1, "dataType": 1, "name": "1.静电场.pdf", "totalTime": 1, "studyTime": 0}
                ],
            },
            {
                "id": 20,
                "type": 0,
                "name": "补充教学视频",
                "childList": [
                    {
                        "id": 21,
                        "type": 0,
                        "name": "空目录",
                        "childList": [],
                    },
                    {
                        "id": 35338435,
                        "type": 1,
                        "dataType": 3,
                        "name": "矢量运算与常用坐标系",
                        "totalTime": 769,
                        "studyTime": 729,  # > 91%, finished
                    },
                    {
                        "id": 35338436,
                        "type": 1,
                        "dataType": 3,
                        "name": "电通量、高斯定理",
                        "totalTime": 900,
                        "studyTime": None,  # unfinished
                    },
                ],
            },
        ]
        self.assertEqual(pick_hike_file_id_from_tree(tree), "35338436")
        self.assertEqual(pick_hike_file_id_from_tree(tree, review_mode=True), "35338435")

    def test_falls_back_to_first_finished_video_then_doc(self):
        all_done = [
            {
                "id": 1,
                "type": 0,
                "childList": [
                    {"id": 11, "type": 1, "dataType": 1, "name": "讲义.pdf"},
                    {"id": 22, "type": 1, "dataType": 3, "name": "已看完视频", "totalTime": 100, "studyTime": 100},
                ],
            }
        ]
        self.assertEqual(pick_hike_file_id_from_tree(all_done), "22")

        docs_only = [
            {"id": 1, "type": 0, "childList": [{"id": 99, "type": 1, "dataType": 1, "name": "讲义.pdf"}]}
        ]
        self.assertEqual(pick_hike_file_id_from_tree(docs_only), "99")

    def test_pick_from_last_study_payload(self):
        self.assertEqual(
            pick_hike_file_id_from_last_study({"status": 200, "rt": {"id": 35338435, "type": 3}}),
            "35338435",
        )
        self.assertEqual(pick_hike_file_id_from_last_study({"status": 200, "rt": None}), "")


class HikeApiResolverTests(unittest.TestCase):
    def test_refreshes_studyresources_sso_on_non_json_then_resolves_file_id(self):
        class _FakeSession:
            def __init__(self):
                self.cookies = []
                self.authed = False
                self.calls = []

            def get(self, url, params=None, headers=None, timeout=None):
                self.calls.append((url, params))
                if url == HIKE_RESOURCE_MENU_TREE_URL:
                    if not self.authed:
                        return _FakeResp(None, status_code=200, raise_json=True)
                    return _FakeResp(
                        {
                            "status": 200,
                            "rt": [
                                {
                                    "id": 1,
                                    "type": 0,
                                    "childList": [
                                        {
                                            "id": 35338435,
                                            "type": 1,
                                            "dataType": 3,
                                            "totalTime": 769,
                                            "studyTime": 0,
                                        }
                                    ],
                                }
                            ],
                        }
                    )
                return _FakeResp({}, status_code=404)

        sess = _FakeSession()
        sso_calls = []

        class _FakeClient:
            def __init__(self):
                self.session = sess
                self.lock = threading.RLock()

            def ensure_service_sso(self, service: str) -> bool:
                sso_calls.append(service)
                sess.authed = True
                return True

        fid = fetch_hike_file_id_via_api(_FakeClient(), "11559973", uuid="u1")
        self.assertEqual(fid, "35338435")
        self.assertEqual(sso_calls, [STUDYRESOURCES_SSO_URL])
        self.assertEqual(len(sess.calls), 2)

    def test_falls_back_to_last_study_file_when_tree_empty(self):
        class _FakeSession:
            def __init__(self):
                self.cookies = []

            def get(self, url, params=None, headers=None, timeout=None):
                if url == HIKE_RESOURCE_MENU_TREE_URL:
                    return _FakeResp({"status": 200, "rt": []})
                if url == HIKE_LAST_STUDY_FILE_URL:
                    return _FakeResp({"status": 200, "rt": {"id": 35338435}})
                return _FakeResp({}, status_code=404)

        client = SimpleNamespace(session=_FakeSession(), lock=threading.RLock())
        fid = fetch_hike_file_id_via_api(client, "11559973", uuid="XKJ3R1PW")
        self.assertEqual(fid, "35338435")


class HikeRuntimeResolverTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolves_bare_hike_url_via_cookies_api(self):
        bare = "https://hike.zhihuishu.com/aidedteaching/sourceLearning/sourceLearning?courseId=11559973"
        context = MagicMock()
        context.cookies = AsyncMock(
            return_value=[{"name": "CASTGC", "value": "tgt", "domain": ".zhihuishu.com"}]
        )
        page = SimpleNamespace(context=context, url="about:blank")
        logger = MagicMock()

        with patch(
            "modules.hike_navigation._resolve_file_id_from_cookies",
            return_value="35338435",
        ) as m_resolve:
            result = await resolve_hike_playback_url(page, bare, logger=logger)

        m_resolve.assert_called_once()
        self.assertEqual(
            result,
            "https://hike.zhihuishu.com/aidedteaching/sourceLearning/sourceLearning?courseId=11559973&fileId=35338435",
        )

    async def test_falls_back_to_wenda_page_in_browser_fetch(self):
        wenda = "https://wenda.zhihuishu.com/stu/courseInfo/studyResource?courseId=11559973&polymas=1"

        class _FakePage:
            def __init__(self):
                self.url = "about:blank"
                self.context = SimpleNamespace(cookies=AsyncMock(return_value=[]))
                self.visited = []

            async def goto(self, url, **_kwargs):
                self.visited.append(url)
                self.url = url

            async def wait_for_selector(self, *_args, **_kwargs):
                return None

            async def evaluate(self, _script, _arg=None):
                return {
                    "status": 200,
                    "rt": [
                        {
                            "id": 1,
                            "type": 0,
                            "childList": [
                                {"id": 35338436, "type": 1, "dataType": 3, "totalTime": 500, "studyTime": 0}
                            ],
                        }
                    ],
                }

        page = _FakePage()
        resolved = await resolve_hike_playback_url(page, wenda)
        self.assertEqual(page.visited, [wenda])
        self.assertEqual(
            resolved,
            "https://hike.zhihuishu.com/aidedteaching/sourceLearning/sourceLearning?courseId=11559973&fileId=35338436",
        )

    async def test_returns_original_url_when_wenda_redirects_to_login(self):
        bare = "https://hike.zhihuishu.com/aidedteaching/sourceLearning/sourceLearning?courseId=11559973"

        class _RedirectLoginPage:
            def __init__(self):
                self.url = "about:blank"
                self.context = SimpleNamespace(cookies=AsyncMock(return_value=[]))

            async def goto(self, _url, **_kwargs):
                self.url = "https://login.zhihuishu.com/?service=..."

        page = _RedirectLoginPage()
        resolved = await resolve_hike_playback_url(page, bare)
        self.assertEqual(resolved, bare)
        self.assertIn("login", page.url)

    async def test_is_hike_404_page_detects_chinese_404_banner(self):
        class _Loc:
            def __init__(self, text):
                self._text = text

            async def inner_text(self, timeout=None):
                return self._text

        page_404 = SimpleNamespace(locator=lambda _sel: _Loc("404 抱歉~您所访问的页面不存在！"))
        page_ok = SimpleNamespace(locator=lambda _sel: _Loc("2026秋大学物理B-2_802"))
        self.assertTrue(await is_hike_404_page(page_404))
        self.assertFalse(await is_hike_404_page(page_ok))


class HikeLessonReloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_wait_for_lesson_active_survives_full_page_navigation(self):
        calls = 0

        class _ReloadingLocator:
            async def evaluate(self, _script, _arg=None, timeout=None):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise RuntimeError(
                        "Page.evaluate: Execution context was destroyed, most likely because of a navigation"
                    )
                if calls == 2:
                    return False
                return True

        ok = await wait_for_lesson_active(_ReloadingLocator(), HIKE_CATALOG, timeout_ms=3000)
        self.assertTrue(ok)
        self.assertEqual(calls, 3)


class HikeDocumentMarkingTests(unittest.IsolatedAsyncioTestCase):
    def test_collect_hike_document_nodes_filters_videos_finished_and_quizzes(self):
        tree = [
            {
                "id": 10,
                "type": 0,
                "name": "课件资料",
                "childList": [
                    {"id": 1001, "type": 1, "dataType": 1, "name": "1.静电场.pdf", "totalTime": 1, "studyTime": 0},
                    {"id": 1002, "type": 1, "dataType": 1, "name": "已读讲义.pdf", "totalTime": 1, "studyTime": 1},
                    {"id": 1003, "fileId": 2003, "dataType": None, "name": "附件文档.docx"},
                    {"id": 1004, "fileId": None, "dataType": None, "name": "章节讨论"},
                    {"id": 1005, "type": 1, "dataType": 3, "name": "教学视频.mp4", "totalTime": 300, "studyTime": 0},
                    {"id": 1006, "type": 1, "dataType": 2, "name": "已完成标记.pptx", "isFinish": 1},
                ],
            }
        ]
        docs = collect_hike_document_nodes(tree)
        self.assertEqual(
            [(d["fileId"], d["name"]) for d in docs],
            [("1001", "1.静电场.pdf"), ("2003", "附件文档.docx")],
        )

    def test_mark_hike_documents_read_via_api_calls_stu_view_file_with_sso_retry(self):
        class _FakeSession:
            def __init__(self):
                self.cookies = []
                self.authed = False
                self.calls = []

            def get(self, url, params=None, headers=None, timeout=None):
                self.calls.append((url, dict(params or {})))
                if not self.authed:
                    return _FakeResp(None, status_code=401)
                if url == HIKE_RESOURCE_MENU_TREE_URL:
                    return _FakeResp(
                        {
                            "status": 200,
                            "rt": [
                                {
                                    "id": 1,
                                    "type": 0,
                                    "childList": [
                                        {"id": 1001, "type": 1, "dataType": 1, "name": "讲义.pdf"},
                                        {"id": 1002, "type": 1, "dataType": 3, "name": "视频.mp4"},
                                    ],
                                }
                            ],
                        }
                    )
                if url == HIKE_STU_VIEW_FILE_URL:
                    return _FakeResp({"status": 200, "rt": {"fileId": 1001, "totalTime": 1}})
                return _FakeResp({}, status_code=404)

        sess = _FakeSession()
        sso_calls = []

        class _FakeClient:
            def __init__(self):
                self.session = sess
                self.lock = threading.RLock()

            def ensure_service_sso(self, service: str) -> bool:
                sso_calls.append(service)
                sess.authed = True
                return True

        summary = mark_hike_documents_read_via_api(
            _FakeClient(), "11559973", delay=0.0
        )
        self.assertEqual(summary, {"found": 1, "done": 1, "failed": 0})
        self.assertEqual(sso_calls, [STUDYRESOURCES_SSO_URL])
        view_calls = [p for u, p in sess.calls if u == HIKE_STU_VIEW_FILE_URL]
        self.assertEqual(view_calls, [{"courseId": "11559973", "fileId": "1001"}])

    async def test_complete_hike_document_nodes_skips_review_mode_and_non_hike(self):
        hike_url = "https://hike.zhihuishu.com/aidedteaching/sourceLearning/sourceLearning?courseId=11559973&fileId=35338435"
        context = MagicMock()
        context.cookies = AsyncMock(
            return_value=[{"name": "CASTGC", "value": "tgt", "domain": ".zhihuishu.com"}]
        )
        page = SimpleNamespace(context=context, url=hike_url)

        with patch(
            "modules.hike_navigation._mark_hike_documents_from_cookies",
            return_value={"found": 2, "done": 2, "failed": 0},
        ) as m_mark:
            res_review = await complete_hike_document_nodes(
                page, hike_url, config=SimpleNamespace(reviewMode=True)
            )
            self.assertEqual(res_review, {"found": 0, "done": 0, "failed": 0})
            m_mark.assert_not_called()

            res_other = await complete_hike_document_nodes(
                SimpleNamespace(context=context, url="https://studyvideoh5.zhihuishu.com/stuStudy"),
                "https://studyvideoh5.zhihuishu.com/stuStudy",
            )
            self.assertEqual(res_other, {"found": 0, "done": 0, "failed": 0})
            m_mark.assert_not_called()

            res_ok = await complete_hike_document_nodes(
                page, hike_url, config=SimpleNamespace(reviewMode=False), logger=MagicMock()
            )
            self.assertEqual(res_ok, {"found": 2, "done": 2, "failed": 0})
            m_mark.assert_called_once()


if __name__ == "__main__":
    unittest.main()
