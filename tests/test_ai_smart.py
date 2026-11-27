import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from modules.ai_smart import (
    build_targets,
    incomplete_targets,
    is_ai_smart_url,
    parse_clock,
    parse_percent,
    parse_segment_range,
    run_ai_smart_course,
)
from modules.course_runner import CourseOutcome


class AiSmartTests(unittest.TestCase):
    def test_detects_only_the_ai_smart_host(self):
        self.assertTrue(
            is_ai_smart_url(
                "https://ai-smart-course-student-pro.zhihuishu.com/"
                "singleCourse/knowledgeStudy/1/2"
            )
        )
        self.assertFalse(
            is_ai_smart_url("https://example.com/ai-smart-course-student-pro.zhihuishu.com")
        )
        self.assertFalse(is_ai_smart_url("https://fusioncourseh5.zhihuishu.com/x"))
        self.assertFalse(is_ai_smart_url(None))

    def test_parses_progress_and_clock_values(self):
        self.assertEqual(parse_percent("学习进度 100%"), 100)
        self.assertEqual(parse_percent("学习进度 9.8%"), 9)
        self.assertEqual(parse_percent("not started"), 0)
        self.assertEqual(parse_clock("05:00"), 300)
        self.assertEqual(parse_clock("01:02:03"), 3723)
        self.assertIsNone(parse_clock("bad"))

    def test_parses_video_segment_range(self):
        self.assertEqual(parse_segment_range("00:00-05:00"), (0, 300))
        self.assertEqual(
            parse_segment_range("01:02:03 - 01:05:00"),
            (3723, 3900),
        )
        self.assertEqual(parse_segment_range("no range"), (None, None))

    def test_flattens_outline_and_filters_completed_targets(self):
        outline = {
            "modules": [
                {
                    "title": "导热基本定律及稳态导热",
                    "units": [
                        {
                            "title": "导热微分方程及定解条件",
                            "items": [
                                {
                                    "title": "直角坐标系中的导热微分方程",
                                    "progress": "学习进度 100%",
                                },
                                {
                                    "title": "导热微分方程",
                                    "progress": "学习进度 35%",
                                },
                            ],
                        }
                    ],
                }
            ]
        }
        targets = build_targets(outline)
        self.assertEqual(len(targets), 2)
        self.assertEqual(targets[0].progress, 100)
        self.assertEqual(targets[1].progress, 35)
        self.assertEqual(targets[1].label, "导热基本定律及稳态导热 / 导热微分方程及定解条件 / 导热微分方程")
        self.assertEqual(incomplete_targets(outline), [targets[1]])


class AiSmartReviewModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_review_mode_replays_completed_videos_skips_docs_and_loops_until_limit(self):
        outline = {
            "modules": [
                {
                    "title": "模块一",
                    "units": [
                        {
                            "title": "单元一",
                            "items": [
                                {"title": "知识点一", "progress": "学习进度 100%"},
                            ],
                        }
                    ],
                }
            ]
        }
        doc_res = SimpleNamespace(name="doc")
        video_res = SimpleNamespace(name="vid")
        page = Mock()
        page.is_closed = Mock(return_value=False)
        watch_calls = 0

        async def fake_watch(*args, **kwargs):
            nonlocal watch_calls
            self.assertTrue(kwargs.get("review_mode"))
            watch_calls += 1
            if watch_calls == 1:
                return (True, False)
            return (False, True)

        with (
            patch("modules.ai_smart._load_home", AsyncMock()),
            patch("modules.ai_smart.extract_outline", AsyncMock(return_value=outline)),
            patch("modules.ai_smart.open_target", AsyncMock(return_value=True)),
            patch(
                "modules.ai_smart.list_required_resources",
                AsyncMock(return_value=[doc_res, video_res]),
            ),
            patch(
                "modules.ai_smart.resource_type",
                AsyncMock(side_effect=lambda res: "book" if res is doc_res else "video"),
            ),
            patch(
                "modules.ai_smart.resource_title",
                AsyncMock(return_value="视频一"),
            ),
            patch("modules.ai_smart.activate_resource", AsyncMock(return_value=True)),
            patch(
                "modules.ai_smart.complete_document_resource",
                AsyncMock(),
            ) as complete_doc,
            patch(
                "modules.ai_smart.watch_video_resource",
                AsyncMock(side_effect=fake_watch),
            ) as watch_video,
            patch("modules.ai_smart._time_limit_reached", return_value=False),
        ):
            outcome = await run_ai_smart_course(
                page,
                "https://ai-smart-course-student-pro.zhihuishu.com/learn",
                SimpleNamespace(limitMaxTime=30, reviewMode=True),
                Mock(),
                asyncio.Event(),
            )

        self.assertIs(outcome, CourseOutcome.TIME_LIMIT)
        complete_doc.assert_not_awaited()
        self.assertEqual(watch_video.await_count, 2)

    async def test_review_mode_finishes_one_round_when_no_time_limit(self):
        outline = {
            "modules": [
                {
                    "title": "模块一",
                    "units": [
                        {
                            "title": "单元一",
                            "items": [
                                {"title": "知识点一", "progress": "学习进度 100%"},
                            ],
                        }
                    ],
                }
            ]
        }
        video_res = SimpleNamespace(name="vid")
        page = Mock()
        page.is_closed = Mock(return_value=False)

        with (
            patch("modules.ai_smart._load_home", AsyncMock()),
            patch("modules.ai_smart.extract_outline", AsyncMock(return_value=outline)),
            patch("modules.ai_smart.open_target", AsyncMock(return_value=True)),
            patch(
                "modules.ai_smart.list_required_resources",
                AsyncMock(return_value=[video_res]),
            ),
            patch("modules.ai_smart.resource_type", AsyncMock(return_value="video")),
            patch("modules.ai_smart.resource_title", AsyncMock(return_value="视频一")),
            patch("modules.ai_smart.activate_resource", AsyncMock(return_value=True)),
            patch(
                "modules.ai_smart.watch_video_resource",
                AsyncMock(return_value=(True, False)),
            ) as watch_video,
        ):
            outcome = await run_ai_smart_course(
                page,
                "https://ai-smart-course-student-pro.zhihuishu.com/learn",
                SimpleNamespace(limitMaxTime=0, reviewMode=True),
                Mock(),
                asyncio.Event(),
            )

        self.assertIs(outcome, CourseOutcome.COMPLETED)
        watch_video.assert_awaited_once()

    async def test_run_resources_stage_does_not_skip_free_exam_nodes(self):
        from modules.stages.ai_smart_stage import run_resources_stage

        visited_urls = []

        async def fake_complete(_page, learn_url, *_args):
            visited_urls.append(learn_url)
            return {"ok": True, "done": 1, "skipped": 0, "failed": 0}

        payload = {
            "courseId": "100",
            "classId": "200",
            "nodes": [
                {"nodeUid": "n1", "name": "普通节点", "freeExam": 0},
                {"nodeUid": "n2", "name": "免考节点", "freeExam": 1},
            ],
        }
        with (
            patch("modules.stages.ai_smart_stage._av", side_effect=lambda _name, fb: fb),
            patch("modules.stages.ai_smart_stage.enumerate_nodes", AsyncMock(return_value=payload)),
            patch("modules.stages.ai_smart_stage.complete_resources_for_node", AsyncMock(side_effect=fake_complete)),
        ):
            result = await run_resources_stage(
                Mock(),
                "https://ai-smart-course-student-pro.zhihuishu.com/singleCourse/knowledgeStudy/100/200",
                SimpleNamespace(limitMaxTime=0),
                Mock(),
                asyncio.Event(),
            )

        self.assertEqual(result, {"done": 2, "failed": 0})
        self.assertEqual(len(visited_urls), 2)
        self.assertTrue(any("/n2/" in url for url in visited_urls))

    async def test_skip_ai_free_exam_config_controls_exam_and_mastery_stages_only(self):
        from modules.ai_exam_runner import ExamRunner
        from modules.stages.ai_smart_stage import run_exam_stage, run_mastery_stage

        nodes = [
            {"nodeUid": "n1", "name": "普通节点", "freeExam": 0},
            {"nodeUid": "n2", "name": "免考节点", "freeExam": 1},
        ]

        # ExamRunner respects skip_free_exam=True (default) vs False
        class _FakeExamApi:
            proxy = ""
            session = None

            def __init__(self):
                self.queried = []

            def query_paper(self, course_id, class_id, knowledge_id):
                self.queried.append(knowledge_id)
                return {"paperId": "p1", "examTestId": "e1", "highMasteryScore": 100}

        api_skip = _FakeExamApi()
        s_skip = ExamRunner("100", "200", api_skip, skip_free_exam=True).run(nodes)
        self.assertEqual(api_skip.queried, ["n1"])
        self.assertEqual(s_skip["skipped"], ["免考节点"])
        self.assertEqual(s_skip["done"], ["普通节点"])

        api_all = _FakeExamApi()
        s_all = ExamRunner("100", "200", api_all, skip_free_exam=False).run(nodes)
        self.assertEqual(api_all.queried, ["n1", "n2"])
        self.assertEqual(s_all["done"], ["普通节点", "免考节点"])

        # run_exam_stage and run_mastery_stage forward config.skip_ai_free_exam
        captured_runner_kwargs = {}

        class _SpyRunner:
            def __init__(self, *args, **kwargs):
                captured_runner_kwargs.update(kwargs)

            def run(self, _nodes, _deadline):
                return {"done": [], "skipped": [], "failed": []}

        payload = {"courseId": "100", "classId": "200", "nodes": nodes}
        fake_api = Mock()
        with (
            patch("modules.stages.ai_smart_stage._av", side_effect=lambda _name, fb: fb),
            patch("modules.stages.ai_smart_stage.enumerate_nodes", AsyncMock(return_value=payload)),
            patch("modules.stages.ai_smart_stage.build_page_exam_api", AsyncMock(return_value=fake_api)),
            patch("modules.stages.ai_smart_stage.ExamRunner", _SpyRunner),
        ):
            await run_exam_stage(
                Mock(),
                "https://ai-smart-course-student-pro.zhihuishu.com/singleCourse/knowledgeStudy/100/200",
                SimpleNamespace(
                    enableExamApi=True,
                    enableFormalExamApi=False,
                    masteryTarget=100,
                    enablePptReference=False,
                    limitMaxTime=0,
                    skip_ai_free_exam=False,
                ),
                Mock(),
                run_formal=False,
            )
        self.assertIs(captured_runner_kwargs.get("skip_free_exam"), False)

        mastery_spy = AsyncMock(return_value={"done": [], "skipped": [], "failed": []})
        with (
            patch("modules.stages.ai_smart_stage._av", side_effect=lambda _name, fb: fb),
            patch("modules.stages.ai_smart_stage.enumerate_nodes", AsyncMock(return_value=payload)),
            patch("modules.stages.ai_smart_stage.run_course_mastery", mastery_spy),
        ):
            await run_mastery_stage(
                Mock(),
                "https://ai-smart-course-student-pro.zhihuishu.com/singleCourse/knowledgeStudy/100/200",
                SimpleNamespace(masteryTarget=100, forceRedoAnswered=False, skip_ai_free_exam=False),
                Mock(),
            )
        self.assertIs(mastery_spy.await_args.kwargs.get("skip_free_exam"), False)

    async def test_ai_non_video_fast_complete_skips_videos_and_falls_back_to_dom(self):
        from modules.exam_api import (
            STUDY_RECORD_COMPLETED,
            collect_incomplete_non_video_resources,
            complete_non_video_resources_via_api,
            is_video_knowledge_resource,
        )
        from modules.stages.ai_smart_stage import run_resources_stage

        resources = [
            {
                "studyStatus": 0,
                "resourcesDetail": {
                    "resourcesUid": "101",
                    "resourcesName": "课件.pptx",
                    "resourcesType": 1,
                    "resourcesDistributeType": 4,
                    "resourcesSyncType": 1,
                },
            },
            {
                "studyStatus": 0,
                "resourcesDetail": {
                    "resourcesUid": "102",
                    "resourcesName": "导读文本",
                    "resourcesType": 2,
                    "resourcesDistributeType": 1,
                    "resourcesSyncType": 1,
                },
            },
            {
                "studyStatus": 1,
                "resourcesDetail": {
                    "resourcesUid": "103",
                    "resourcesName": "已完成文档",
                    "resourcesType": 2,
                    "resourcesDistributeType": 1,
                    "resourcesSyncType": 1,
                },
            },
            {
                "studyStatus": 0,
                "resourcesDetail": {
                    "resourcesUid": "104",
                    "resourcesName": "选学文档",
                    "resourcesType": 2,
                    "resourcesDistributeType": 1,
                    "resourcesSyncType": 2,
                },
            },
            {
                "studyStatus": 0,
                "resourcesDetail": {
                    "resourcesUid": "105",
                    "resourcesName": "主视频",
                    "resourcesType": 1,
                    "resourcesDistributeType": 3,
                    "resourcesSyncType": 1,
                },
            },
        ]
        self.assertTrue(is_video_knowledge_resource(resources[4]))
        self.assertFalse(is_video_knowledge_resource(resources[0]))
        targets = collect_incomplete_non_video_resources(resources)
        self.assertEqual([t["resourcesUid"] for t in targets], [101, 102])

        completed_calls = []

        class _FakeResourceApi:
            def __init__(self, by_node):
                self.by_node = by_node
                self.closed = False

            def list_knowledge_resources(self, course_id, class_id, knowledge_id):
                return self.by_node.get(knowledge_id, [])

            def complete_study_resource(self, course_id, class_id, knowledge_id, resources_uid):
                completed_calls.append((STUDY_RECORD_COMPLETED, knowledge_id, resources_uid))
                return True

            def close(self):
                self.closed = True

        api = _FakeResourceApi({"n_doc": resources[:2], "n_mixed": [resources[0], resources[4]]})
        res_doc = complete_non_video_resources_via_api(
            api, "100", "200", "n_doc", delay=0.0
        )
        self.assertTrue(res_doc["ok"])
        self.assertEqual(res_doc["done"], 2)
        self.assertFalse(res_doc["has_incomplete_video"])
        self.assertEqual(
            completed_calls,
            [(STUDY_RECORD_COMPLETED, "n_doc", 101), (STUDY_RECORD_COMPLETED, "n_doc", 102)],
        )

        # In run_resources_stage: doc-only node skips DOM, mixed video+doc node falls through to DOM
        completed_calls.clear()
        dom_visited = []

        async def fake_dom(_page, learn_url, *_args):
            dom_visited.append(learn_url)
            return {"ok": True, "done": 1, "skipped": 0, "failed": 0}

        payload = {
            "courseId": "100",
            "classId": "200",
            "nodes": [
                {"nodeUid": "n_doc", "name": "纯文档节点", "freeExam": 1},
                {"nodeUid": "n_mixed", "name": "含视频节点", "freeExam": 0},
            ],
        }
        with (
            patch("modules.stages.ai_smart_stage._av", side_effect=lambda _name, fb: fb),
            patch("modules.stages.ai_smart_stage.enumerate_nodes", AsyncMock(return_value=payload)),
            patch("modules.stages.ai_smart_stage.build_page_exam_api", AsyncMock(return_value=api)),
            patch("modules.stages.ai_smart_stage.complete_resources_for_node", AsyncMock(side_effect=fake_dom)),
            patch("modules.exam_api.time.sleep", return_value=None),
        ):
            stage_res = await run_resources_stage(
                Mock(),
                "https://ai-smart-course-student-pro.zhihuishu.com/singleCourse/knowledgeStudy/100/200",
                SimpleNamespace(limitMaxTime=0, skip_ai_free_exam=True),
                Mock(),
                asyncio.Event(),
            )
        self.assertTrue(api.closed)
        # 2 docs on n_doc + 1 doc on n_mixed via API + 1 video on n_mixed via DOM = 4
        self.assertEqual(stage_res, {"done": 4, "failed": 0})
        self.assertEqual(len(dom_visited), 1)
        self.assertIn("/n_mixed/", dom_visited[0])


if __name__ == "__main__":
    unittest.main()
