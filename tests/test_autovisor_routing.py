import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import Autovisor
from modules.url_router import (
    KIND_EXAM,
    KIND_HOMEWORK,
    KIND_KNOWLEDGE_STUDY,
    KIND_LEARN_PAGE,
    KIND_TEST_DETAIL,
    parse_url,
)

HOMEWORK_URL = "https://exam.zhihuishu.com/webExamList/dohomework/R1/S1/E1/C1/SCH1"
EXAM_URL = "https://exam.zhihuishu.com/webExamList/doexam/R1/S1/E1/C1/0"
TEST_DETAIL_URL = "https://ai-smart-course-student-pro.zhihuishu.com/testDetail/100/200/300/400"
LEARN_PAGE_URL = "https://ai-smart-course-student-pro.zhihuishu.com/learnPage/100/200/300"
KNOWLEDGE_URL = "https://ai-smart-course-student-pro.zhihuishu.com/knowledgeStudy/100/200"
CATALOG_URL = "https://studyvideoh5.zhihuishu.com/stuStudy?recruitAndCourseId=abc"


def _config(**overrides):
    values = {
        "reviewMode": False,
        "enableExamApi": True,
        "enableFormalExamApi": True,
        "enablePptReference": True,
        "masteryTarget": 80,
        "limitMaxTime": 0,
        "proxy": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class CourseRouteTests(unittest.TestCase):
    def test_direct_task_links_are_not_sent_to_catalog(self):
        cases = {
            HOMEWORK_URL: KIND_HOMEWORK,
            EXAM_URL: KIND_EXAM,
            TEST_DETAIL_URL: KIND_TEST_DETAIL,
            LEARN_PAGE_URL: KIND_LEARN_PAGE,
        }
        for url, kind in cases.items():
            parsed = parse_url(url)
            self.assertEqual(parsed.get("kind"), kind)
            self.assertEqual(Autovisor.course_route(parsed, url), "task")

    def test_knowledge_study_and_ai_smart_host_use_ai_smart_flow(self):
        parsed = parse_url(KNOWLEDGE_URL)
        self.assertEqual(parsed.get("kind"), KIND_KNOWLEDGE_STUDY)
        self.assertEqual(
            Autovisor.course_route(parsed, KNOWLEDGE_URL), "ai_smart"
        )
        self.assertEqual(
            Autovisor.course_route({}, "https://ai-smart-course-student-pro.zhihuishu.com/home"),
            "ai_smart",
        )

    def test_catalog_url_stays_on_catalog_flow(self):
        self.assertEqual(
            Autovisor.course_route(parse_url(CATALOG_URL), CATALOG_URL),
            "catalog",
        )


class TaskUrlDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_homework_and_exam_urls_use_homework_handler(self):
        logger = MagicMock()
        for url, kind in ((HOMEWORK_URL, KIND_HOMEWORK), (EXAM_URL, KIND_EXAM)):
            parsed = parse_url(url)
            assert parsed.get("kind") == kind
            handler = AsyncMock(return_value={"answered": 3, "submitted": True})
            formal = AsyncMock()
            learn = AsyncMock()
            with (
                patch.object(Autovisor.homework, "auto_answer_homework", handler),
                patch.object(Autovisor, "run_formal_exam_route", formal),
                patch.object(Autovisor, "run_learn_page_route", learn),
            ):
                stats = await Autovisor.run_task_url(
                    None, url, parsed, _config(), logger, None
                )
            handler.assert_awaited_once()
            formal.assert_not_awaited()
            learn.assert_not_awaited()
            self.assertEqual(stats["answered"], 3)
            logger.event.assert_any_call(
                "任务链接", 类型=kind, 作答=3, 已交卷=True
            )

    async def test_test_detail_routes_to_formal_exam(self):
        parsed = parse_url(TEST_DETAIL_URL)
        formal = AsyncMock(return_value={"success": True})
        homework_handler = AsyncMock()
        with (
            patch.object(Autovisor, "run_formal_exam_route", formal),
            patch.object(Autovisor.homework, "auto_answer_homework", homework_handler),
        ):
            await Autovisor.run_task_url(
                None, TEST_DETAIL_URL, parsed, _config(), MagicMock(), None
            )
        formal.assert_awaited_once()
        homework_handler.assert_not_awaited()

    async def test_learn_page_routes_to_single_node_flow(self):
        parsed = parse_url(LEARN_PAGE_URL)
        learn = AsyncMock(return_value={})
        formal = AsyncMock()
        with (
            patch.object(Autovisor, "run_learn_page_route", learn),
            patch.object(Autovisor, "run_formal_exam_route", formal),
        ):
            await Autovisor.run_task_url(
                None, LEARN_PAGE_URL, parsed, _config(), MagicMock(), None
            )
        learn.assert_awaited_once()
        formal.assert_not_awaited()


class LearnPageRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_completes_resources_then_runs_single_node_exam(self):
        parsed = parse_url(LEARN_PAGE_URL)
        resource = AsyncMock(
            return_value={"ok": True, "done": 2, "skipped": 0, "failed": 0}
        )
        runner = MagicMock()
        runner.run.return_value = {"done": ["200"], "skipped": [], "failed": []}
        with (
            patch.object(Autovisor, "complete_resources_for_node", resource),
            patch.object(
                Autovisor, "build_page_exam_api", AsyncMock(return_value=object())
            ),
            patch.object(Autovisor, "ExamRunner", return_value=runner) as runner_cls,
            patch.object(Autovisor, "build_learn_url", return_value="https://learn"),
        ):
            summary = await Autovisor.run_learn_page_route(
                MagicMock(), parsed, _config(), MagicMock(), asyncio.Event()
            )
        resource.assert_awaited_once()
        runner_cls.assert_called_once()
        self.assertEqual(runner_cls.call_args[0][:2], ("100", "300"))
        nodes = runner.run.call_args[0][0]
        self.assertEqual(nodes, [{"nodeUid": "200", "name": "知识点 200"}])
        self.assertEqual(summary["done"], ["200"])

    async def test_missing_node_uid_skips_without_side_effects(self):
        parsed = parse_url("https://ai-smart-course-student-pro.zhihuishu.com/learnPage?courseId=1&classId=2")
        with (
            patch.object(Autovisor, "complete_resources_for_node", AsyncMock()) as resource,
            patch.object(Autovisor, "ExamRunner") as runner,
        ):
            summary = await Autovisor.run_learn_page_route(
                MagicMock(), parsed, _config(), MagicMock(), asyncio.Event()
            )
        self.assertEqual(summary, {})
        resource.assert_not_awaited()
        runner.assert_not_called()


class FormalExamRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_test_detail_parameters_are_forwarded_to_ctx(self):
        parsed = parse_url(TEST_DETAIL_URL)
        api = object()
        ctx = MagicMock()
        ctx.start.return_value = (True, 5, 5)
        with (
            patch.object(
                Autovisor, "build_page_exam_api", AsyncMock(return_value=api)
            ),
            patch.object(Autovisor, "AiFormalExamCtx") as ctx_cls,
            patch.object(
                Autovisor.quiz_page.llm, "conf", return_value={"auto_submit": True}
            ),
        ):
            ctx_cls.from_task.return_value = ctx
            stats = await Autovisor.run_formal_exam_route(
                MagicMock(), parsed, _config(), MagicMock()
            )
        task = ctx_cls.from_task.call_args[0][1]
        self.assertEqual(
            task,
            {
                "courseId": "100",
                "classId": "200",
                "examTestId": "300",
                "examPaperId": "400",
            },
        )
        ctx_cls.from_task.assert_called_once_with(api, task, auto_submit=True)
        self.assertEqual(stats, {"success": True, "answered": 5, "total": 5})

    async def test_test_detail_auto_submit_false_saved_all_succeeds(self):
        parsed = parse_url(TEST_DETAIL_URL)
        api = object()
        ctx = MagicMock()
        ctx._failed_saves = 0
        ctx.start.return_value = (False, 4, 4)
        with (
            patch.object(
                Autovisor, "build_page_exam_api", AsyncMock(return_value=api)
            ),
            patch.object(Autovisor, "AiFormalExamCtx") as ctx_cls,
            patch.object(
                Autovisor.quiz_page.llm, "conf", return_value={"auto_submit": False}
            ),
        ):
            ctx_cls.from_task.return_value = ctx
            stats = await Autovisor.run_formal_exam_route(
                MagicMock(), parsed, _config(), MagicMock()
            )
        self.assertEqual(stats, {"success": True, "answered": 4, "total": 4})

    def test_formal_task_scan_executes_each_task_with_auto_submit(self):
        api = object()
        probe = MagicMock()
        probe.scan_pending_tasks.return_value = [
            {"examTestId": "E1", "classId": "C2"},
            {"examTestId": "E2", "courseId": "C1"},
        ]
        ctx = MagicMock()
        ctx.start.return_value = (True, 2, 2)
        logger = MagicMock()
        with patch.object(
            Autovisor.AiFormalExamCtx,
            "from_task",
            side_effect=[probe, ctx, ctx],
        ) as factory:
            summary = Autovisor.run_formal_exam_tasks(
                api, "C1", "C9", True, logger
            )
        self.assertEqual(
            summary["found"], 2
        )
        self.assertEqual(summary["done"], 2)
        self.assertEqual(summary["failed"], 0)
        calls = factory.call_args_list
        self.assertEqual(calls[0][0][1], {"courseId": "C1", "classId": "C9"})
        self.assertEqual(calls[1][0][1]["classId"], "C2")
        self.assertEqual(calls[2][0][1]["courseId"], "C1")
        self.assertEqual(calls[1][1]["auto_submit"], True)
        self.assertEqual(ctx.start.call_count, 2)
        self.assertEqual(ctx.start.call_args[1], {"submit": True})

    def test_formal_task_failure_is_counted_without_aborting(self):
        api = object()
        probe = MagicMock()
        probe.scan_pending_tasks.return_value = [{"examTestId": "E1"}]
        boom = MagicMock()
        boom.start.side_effect = RuntimeError("no paper")
        logger = MagicMock()
        with patch.object(
            Autovisor.AiFormalExamCtx,
            "from_task",
            side_effect=[probe, boom],
        ):
            summary = Autovisor.run_formal_exam_tasks(
                api, "C1", "", False, logger
            )
        self.assertEqual(summary["found"], 1)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["done"], 0)
        logger.log_exception.assert_called_once()

    def test_auto_submit_false_saved_all_counts_as_done_not_failed(self):
        api = object()
        probe = MagicMock()
        probe.scan_pending_tasks.return_value = [{"examTestId": "E1", "taskName": "单元测试"}]
        ctx = MagicMock()
        ctx._failed_saves = 0
        ctx.start.return_value = (False, 5, 5)
        logger = MagicMock()
        with patch.object(
            Autovisor.AiFormalExamCtx,
            "from_task",
            side_effect=[probe, ctx],
        ):
            summary = Autovisor.run_formal_exam_tasks(
                api, "C1", "CL1", False, logger
            )
        self.assertEqual(summary["found"], 1)
        self.assertEqual(summary["done"], 1)
        self.assertEqual(summary["submitted"], 0)
        self.assertEqual(summary["failed"], 0)
        self.assertTrue(summary["details"][0]["ok"])
        self.assertFalse(summary["details"][0]["submitted"])

    def test_auto_submit_false_with_failed_saves_counts_as_failed(self):
        api = object()
        probe = MagicMock()
        probe.scan_pending_tasks.return_value = [{"examTestId": "E1"}]
        ctx = MagicMock()
        ctx._failed_saves = 2
        ctx.start.return_value = (False, 3, 5)
        logger = MagicMock()
        with patch.object(
            Autovisor.AiFormalExamCtx,
            "from_task",
            side_effect=[probe, ctx],
        ):
            summary = Autovisor.run_formal_exam_tasks(
                api, "C1", "CL1", False, logger
            )
        self.assertEqual(summary["done"], 0)
        self.assertEqual(summary["failed"], 1)
        self.assertFalse(summary["details"][0]["ok"])

    async def test_formal_exam_runs_when_exam_api_disabled(self):
        api = object()
        logger = MagicMock()
        formal_result = {"found": 1, "done": 1, "submitted": 0, "failed": 0, "details": []}
        with (
            patch.object(Autovisor, "enumerate_nodes", AsyncMock()) as enum_nodes,
            patch.object(Autovisor, "ExamRunner") as runner_cls,
            patch.object(Autovisor, "build_page_exam_api", AsyncMock(return_value=api)) as build_api,
            patch.object(Autovisor, "run_formal_exam_tasks", return_value=formal_result) as run_formal,
            patch.object(Autovisor.quiz_page.llm, "conf", return_value={"auto_submit": False}),
        ):
            summary = await Autovisor.run_exam_stage(
                MagicMock(),
                KNOWLEDGE_URL,
                _config(enableExamApi=False, enableFormalExamApi=True),
                logger,
            )
        enum_nodes.assert_not_awaited()
        runner_cls.assert_not_called()
        build_api.assert_awaited_once()
        run_formal.assert_called_once_with(api, "100", "200", False, logger)
        self.assertEqual(summary["formal_exams"], formal_result)

    async def test_formal_exam_runs_when_nodes_are_empty(self):
        api = object()
        logger = MagicMock()
        formal_result = {"found": 1, "done": 1, "submitted": 1, "failed": 0, "details": []}
        with (
            patch.object(
                Autovisor,
                "enumerate_nodes",
                AsyncMock(return_value={"courseId": "100", "classId": "200", "nodes": []}),
            ) as enum_nodes,
            patch.object(Autovisor, "ExamRunner") as runner_cls,
            patch.object(Autovisor, "build_page_exam_api", AsyncMock(return_value=api)) as build_api,
            patch.object(Autovisor, "run_formal_exam_tasks", return_value=formal_result) as run_formal,
            patch.object(Autovisor.quiz_page.llm, "conf", return_value={"auto_submit": True}),
        ):
            summary = await Autovisor.run_exam_stage(
                MagicMock(),
                KNOWLEDGE_URL,
                _config(enableExamApi=True, enableFormalExamApi=True),
                logger,
            )
        enum_nodes.assert_awaited_once()
        runner_cls.assert_not_called()
        build_api.assert_awaited_once()
        run_formal.assert_called_once_with(api, "100", "200", True, logger)
        self.assertEqual(summary["formal_exams"], formal_result)


class ZhidaoSharedCourseAssessmentTests(unittest.IsolatedAsyncioTestCase):
    def test_resolve_zhidao_course_meta_falls_back_to_school_info(self):
        client = MagicMock()
        with (
            patch(
                "modules.zhidao_homework_api.ZhidaoHomeworkApi.query_course",
                return_value={
                    "code": 0,
                    "data": {
                        "recruitId": 458533,
                        "courseInfo": {"courseId": 1000013343, "name": "大学物理"},
                    },
                },
            ),
            patch(
                "modules.zhidao_exam_api.ZhidaoExamApi.get_login_school_info",
                return_value={"status": "200", "rt": {"schoolId": 1847}},
            ),
        ):
            meta = Autovisor.resolve_zhidao_course_meta(client, "abc")
        self.assertEqual(meta["recruitId"], "458533")
        self.assertEqual(meta["courseId"], "1000013343")
        self.assertEqual(meta["schoolId"], "1847")
        self.assertEqual(meta["courseName"], "大学物理")

    async def test_assessment_stage_runs_pending_homework_vision_fallback_and_skips_unstarted_exam(
        self,
    ):
        logger = MagicMock()
        page = MagicMock()
        page.goto = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        page.context = MagicMock()
        page.context.cookies = AsyncMock(return_value=[])
        client = MagicMock()
        client.session = MagicMock()

        hw_worker = MagicMock()
        hw_worker.scan.side_effect = [
            [
                {
                    "studentExamId": "S1",
                    "examId": "E1",
                    "examName": "第一章单元测试",
                    "courseId": "1000013343",
                    "startTime": "2020-01-01 00:00:00",
                    "endDate": "2099-12-31 23:59:59",
                },
                {
                    "studentExamId": "S2",
                    "examId": "E2",
                    "examName": "第二章图片题作业",
                    "courseId": "1000013343",
                    "startTime": "2020-01-01 00:00:00",
                    "endDate": "2099-12-31 23:59:59",
                },
            ],
            [
                {
                    "studentExamId": "S3",
                    "examId": "E3",
                    "examName": "已提交低分作业",
                    "courseId": "1000013343",
                    "score": 10,
                    "totalScore": "20",
                    "isMarking": 0,
                    "backNum": 3,
                    "startTime": "2020-01-01 00:00:00",
                    "endDate": "2099-12-31 23:59:59",
                },
            ],
        ]
        hw_worker.run.side_effect = [
            {"answered": 10, "submitted": True, "score": 20.0, "needs_vision": False},
            {"answered": 4, "submitted": False, "score": None, "needs_vision": True},
            {"answered": 8, "submitted": True, "score": 20.0, "needs_vision": False},
        ]

        exam_worker = MagicMock()
        exam_worker.scan.return_value = [
            {
                "studentExamId": "EX0",
                "examId": "E_FUTURE",
                "examName": "教程考试",
                "courseId": "1000013343",
                "state": 1,
                "progressType": 2,
                "studentStartTime": "",
                "startTime": "2099-12-01 00:00:00",
                "endDate": "2099-12-04 23:59:59",
            },
            {
                "studentExamId": "EX1",
                "examId": "E_LIVE",
                "examName": "进行中考试",
                "courseId": "1000013343",
                "state": 1,
                "studentStartTime": "2026-11-24 08:00:00",
                "endDate": "2099-12-04 23:59:59",
            },
        ]
        exam_worker.run.return_value = {
            "answered": 5,
            "submitted": True,
            "skipped": False,
            "needs_vision": False,
        }

        vision_handler = AsyncMock(return_value={"ok": 2, "fill": 0, "fail": 0})
        cfg = _config(
            enableHomeworkApi=True,
            enableFormalExamApi=True,
            homeworkThreshold=100,
            homeworkMaxRedo=3,
        )
        with (
            patch.object(Autovisor, "build_page_zhs_client", AsyncMock(return_value=client)),
            patch.object(
                Autovisor,
                "resolve_zhidao_course_meta",
                return_value={
                    "recruitId": "458533",
                    "courseId": "1000013343",
                    "schoolId": "1847",
                    "courseName": "大学物理思维导引（下）",
                },
            ),
            patch("modules.zhidao_homework_worker.ZhidaoHomeworkWorker", return_value=hw_worker),
            patch("modules.zhidao_exam_worker.ZhidaoExamWorker", return_value=exam_worker),
            patch.object(Autovisor.quiz_page.llm, "conf", return_value={"auto_submit": True}),
            patch.object(Autovisor.quiz_page.llm, "enabled", return_value=True),
            patch.object(Autovisor.quiz_page.llm, "vision_enabled", return_value=True),
            patch.object(Autovisor.homework, "auto_answer_homework", vision_handler),
        ):
            summary = await Autovisor.run_zhidao_assessment_stage(
                page,
                CATALOG_URL,
                {"kind": "course", "recruitAndCourseId": "abc"},
                cfg,
                logger,
            )

        self.assertEqual(summary["homework"]["found"], 3)
        self.assertEqual(summary["homework"]["done"], 3)
        self.assertEqual(summary["homework"]["failed"], 0)
        self.assertEqual(hw_worker.run.call_count, 3)
        # 图片题作业应打开直达页并跳过 API 直接走视觉模型
        vision_handler.assert_awaited_once_with(page, config=cfg, skip_api=True)
        page.goto.assert_awaited_once()
        self.assertIn("/dohomework/458533/S2/E2/1000013343/1847/0", page.goto.await_args.args[0])

        # 未开考的 progressType=2 考试应跳过，只接管进行中的考试
        self.assertEqual(summary["exam"]["found"], 2)
        self.assertEqual(summary["exam"]["skipped"], 1)
        self.assertEqual(summary["exam"]["done"], 1)
        exam_worker.run.assert_called_once()
        self.assertEqual(exam_worker.run.call_args.kwargs["exam_id"], "E_LIVE")
        self.assertEqual(exam_worker.run.call_args.kwargs["recruit_id"], "458533")

    async def test_vision_fallback_reloads_on_same_document_hash_navigation(self):
        logger = MagicMock()
        page = MagicMock()
        page.url = (
            "https://onlineexamh5new.zhihuishu.com/stuExamWeb.html"
            "#/webExamList/dohomework/458533/S1/E1/1000013343/1847/0"
        )
        page.goto = AsyncMock()
        page.reload = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        vision_handler = AsyncMock(return_value={"ok": 10, "fill": 0, "fail": 0})
        cfg = _config()

        target_url = (
            "https://onlineexamh5new.zhihuishu.com/stuExamWeb.html"
            "#/webExamList/dohomework/458533/S2/E2/1000013343/1847/0"
        )
        with (
            patch.object(Autovisor.quiz_page.llm, "vision_enabled", return_value=True),
            patch.object(Autovisor.homework, "auto_answer_homework", vision_handler),
        ):
            res = await Autovisor._run_vision_fallback_on_url(
                page, target_url, cfg, logger, label="第二章单元测试"
            )

        self.assertEqual(res, {"ok": 10, "fill": 0, "fail": 0})
        page.goto.assert_awaited_once_with(
            target_url, wait_until="domcontentloaded", timeout=30000
        )
        page.reload.assert_awaited_once_with(
            wait_until="domcontentloaded", timeout=30000
        )
        vision_handler.assert_awaited_once_with(page, config=cfg, skip_api=True)

    async def test_assessment_stage_does_not_scan_flag_2_when_auto_submit_false(self):
        logger = MagicMock()
        page = MagicMock()
        client = MagicMock()
        client.session = MagicMock()
        hw_worker = MagicMock()
        hw_worker.scan.return_value = []
        cfg = _config(enableHomeworkApi=True, enableFormalExamApi=False)
        with (
            patch.object(Autovisor, "build_page_zhs_client", AsyncMock(return_value=client)),
            patch.object(
                Autovisor,
                "resolve_zhidao_course_meta",
                return_value={"recruitId": "R1", "courseId": "C1", "schoolId": "S1"},
            ),
            patch("modules.zhidao_homework_worker.ZhidaoHomeworkWorker", return_value=hw_worker),
            patch.object(Autovisor.quiz_page.llm, "conf", return_value={"auto_submit": False}),
            patch.object(Autovisor.quiz_page.llm, "enabled", return_value=True),
        ):
            summary = await Autovisor.run_zhidao_assessment_stage(
                page,
                CATALOG_URL,
                {"kind": "course", "recruitAndCourseId": "abc"},
                cfg,
                logger,
            )
        # auto_submit=False 时不扫描 flag=2，避免重置已提交作业后未重交
        hw_worker.scan.assert_called_once_with(1, recruit_id="R1", course_id="C1")
        self.assertEqual(summary["homework"]["found"], 0)

    async def test_assessment_stage_retries_once_after_captcha(self):
        from modules.zhs_api import CaptchaRequiredError

        logger = MagicMock()
        page = MagicMock()
        page.goto = AsyncMock()
        page.context = MagicMock()
        page.context.cookies = AsyncMock(
            return_value=[
                {
                    "name": "CASLOGC",
                    "value": "v2",
                    "domain": ".zhihuishu.com",
                    "path": "/",
                    "expires": 1893456000,
                    "secure": True,
                    "httpOnly": True,
                }
            ]
        )
        client = MagicMock()
        client.session = MagicMock()

        hw_worker = MagicMock()
        hw_worker.scan.return_value = [
            {
                "studentExamId": "S1",
                "examId": "E1",
                "examName": "第一章单元测试",
                "courseId": "C1",
            }
        ]
        hw_worker.run.side_effect = [
            CaptchaRequiredError("需要滑块"),
            {"answered": 3, "submitted": False, "score": None, "needs_vision": False},
        ]
        settle = AsyncMock(return_value=True)
        cfg = _config(enableHomeworkApi=True, enableFormalExamApi=False)
        with (
            patch.object(Autovisor, "build_page_zhs_client", AsyncMock(return_value=client)),
            patch.object(
                Autovisor,
                "resolve_zhidao_course_meta",
                return_value={"recruitId": "R1", "courseId": "C1", "schoolId": "SCH"},
            ),
            patch("modules.zhidao_homework_worker.ZhidaoHomeworkWorker", return_value=hw_worker),
            patch.object(Autovisor.quiz_page.llm, "conf", return_value={"auto_submit": False}),
            patch.object(Autovisor.quiz_page.llm, "enabled", return_value=True),
            patch.object(Autovisor.homework, "_settle_captcha", settle),
        ):
            summary = await Autovisor.run_zhidao_assessment_stage(
                page,
                CATALOG_URL,
                {"kind": "course", "recruitAndCourseId": "abc"},
                cfg,
                logger,
            )
        settle.assert_awaited_once_with(page, cfg)
        client.session.cookies.set.assert_called_with(
            "CASLOGC",
            "v2",
            secure=True,
            rest={"HttpOnly": None},
            domain=".zhihuishu.com",
            path="/",
            expires=1893456000,
        )
        client.reset_exam_sso.assert_called_once()
        self.assertEqual(summary["homework"]["done"], 1)
        self.assertEqual(hw_worker.run.call_count, 2)

    async def test_assessment_stage_merges_vision_submitted_and_counts_vision_failures(self):
        logger = MagicMock()
        page = MagicMock()
        page.url = CATALOG_URL
        page.goto = AsyncMock()
        page.reload = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        client = MagicMock()
        client.session = MagicMock()

        hw_worker = MagicMock()
        hw_worker.scan.side_effect = [
            [
                {"studentExamId": "S1", "examId": "E1", "examName": "图文提交作业", "courseId": "C1"},
                {"studentExamId": "S2", "examId": "E2", "examName": "全失败图片作业", "courseId": "C1"},
            ],
            [],
        ]
        hw_worker.run.side_effect = [
            {"answered": 1, "submitted": False, "score": None, "needs_vision": True},
            {"answered": 0, "submitted": False, "score": None, "needs_vision": True},
        ]

        exam_worker = MagicMock()
        exam_worker.scan.return_value = [
            {
                "studentExamId": "ES1",
                "examId": "EE1",
                "examName": "图文期末考",
                "courseId": "C1",
                "studentStartTime": "2026-11-24 08:00:00",
            },
            {
                "studentExamId": "ES2",
                "examId": "EE2",
                "examName": "全失败期末考",
                "courseId": "C1",
                "studentStartTime": "2026-11-24 08:00:00",
            },
        ]
        exam_worker.run.side_effect = [
            {"answered": 2, "submitted": False, "skipped": False, "needs_vision": True},
            {"answered": 0, "submitted": False, "skipped": False, "needs_vision": True},
        ]

        vision_returns = [
            {"ok": 1, "fill": 0, "fail": 0, "submitted": True},
            {"ok": 0, "fill": 0, "fail": 2, "submitted": False},
            {"ok": 1, "fill": 0, "fail": 0, "submitted": True},
            {"ok": 0, "fill": 0, "fail": 1, "submitted": False},
        ]
        vision_mock = AsyncMock(side_effect=vision_returns)
        cfg = _config(enableHomeworkApi=True, enableFormalExamApi=True)
        with (
            patch.object(Autovisor, "build_page_zhs_client", AsyncMock(return_value=client)),
            patch.object(
                Autovisor,
                "resolve_zhidao_course_meta",
                return_value={"recruitId": "R1", "courseId": "C1", "schoolId": "SCH"},
            ),
            patch("modules.zhidao_homework_worker.ZhidaoHomeworkWorker", return_value=hw_worker),
            patch("modules.zhidao_exam_worker.ZhidaoExamWorker", return_value=exam_worker),
            patch.object(Autovisor.quiz_page.llm, "conf", return_value={"auto_submit": True}),
            patch.object(Autovisor.quiz_page.llm, "enabled", return_value=True),
            patch.object(Autovisor.quiz_page.llm, "vision_enabled", return_value=True),
            patch.object(Autovisor.homework, "auto_answer_homework", vision_mock),
        ):
            summary = await Autovisor.run_zhidao_assessment_stage(
                page,
                CATALOG_URL,
                {"kind": "course", "recruitAndCourseId": "abc"},
                cfg,
                logger,
            )

        self.assertEqual(summary["homework"]["done"], 1)
        self.assertEqual(summary["homework"]["failed"], 1)
        self.assertEqual(summary["homework"]["skipped"], 0)
        self.assertTrue(summary["homework"]["details"][0]["submitted"])
        self.assertEqual(summary["homework"]["details"][0]["answered"], 2)
        self.assertFalse(summary["homework"]["details"][1]["ok"])
        self.assertEqual(summary["homework"]["details"][1]["reason"], "vision_failed")

        self.assertEqual(summary["exam"]["done"], 1)
        self.assertEqual(summary["exam"]["failed"], 1)
        self.assertEqual(summary["exam"]["skipped"], 0)
        self.assertTrue(summary["exam"]["details"][0]["submitted"])
        self.assertEqual(summary["exam"]["details"][0]["answered"], 3)
        self.assertFalse(summary["exam"]["details"][1]["ok"])
        self.assertEqual(summary["exam"]["details"][1]["reason"], "vision_failed")

    async def test_main_triggers_zhidao_assessment_after_catalog_completed(self):
        from modules.course_runner import CourseOutcome

        async def _run_main_with(outcome, *, review_mode=False):
            page = MagicMock()
            page.url = CATALOG_URL
            page.goto = AsyncMock()
            page.wait_for_timeout = AsyncMock()
            locator = MagicMock()
            locator.first = locator
            locator.count = AsyncMock(return_value=0)
            page.locator = MagicMock(return_value=locator)

            context = MagicMock()
            context.on = MagicMock()
            context.cookies = AsyncMock(return_value=[])
            session = MagicMock()
            session.page = page
            session.context = context
            session.close = AsyncMock()

            pw_cm = MagicMock()
            pw_cm.__aenter__ = AsyncMock(return_value=MagicMock())
            pw_cm.__aexit__ = AsyncMock(return_value=False)

            async def _idle(*_a, **_kw):
                await asyncio.sleep(60)

            cfg = _config(
                course_urls=[CATALOG_URL],
                enableAutoCaptcha=False,
                attach_existing_chrome=False,
                enableZhidaoQuiz=False,
                enableHideWindow=False,
                enableHomeworkApi=True,
                enableFormalExamApi=True,
                reviewMode=review_mode,
                username="",
                password="",
            )
            assessment = AsyncMock(return_value={})
            notifier = MagicMock()
            notifier.notify_finish = AsyncMock()
            notifier.notify_error = AsyncMock()
            notifier.notify_verify = AsyncMock()
            catalog = SimpleNamespace(name="legacy", course_title="")
            from contextlib import ExitStack

            patches = [
                patch.object(Autovisor, "async_playwright", return_value=pw_cm),
                patch.object(Autovisor, "load_cookies", return_value=[]),
                patch.object(Autovisor, "maybe_qr_login", AsyncMock(return_value=[])),
                patch.object(Autovisor, "create_browser_session", AsyncMock(return_value=session)),
                patch.object(Autovisor, "ensure_login", AsyncMock(return_value=True)),
                patch.object(Autovisor, "persist_login_cookies", AsyncMock()),
                patch.object(Autovisor, "should_hide_window", return_value=False),
                patch.object(Autovisor, "wait_for_verify", side_effect=_idle),
                patch.object(Autovisor, "video_optimize", side_effect=_idle),
                patch.object(Autovisor, "skip_questions", side_effect=_idle),
                patch.object(Autovisor, "play_video", side_effect=_idle),
                patch.object(Autovisor, "dismiss_popups", side_effect=_idle),
                patch.object(Autovisor, "periodic_cookie_sync", side_effect=_idle),
                patch.object(Autovisor, "task_monitor", side_effect=_idle),
                patch.object(Autovisor, "detect_catalog_after_verification", AsyncMock(return_value=catalog)),
                patch.object(Autovisor, "optimize_page", AsyncMock()),
                patch.object(Autovisor, "run_course", AsyncMock(return_value=outcome)),
                patch.object(Autovisor, "run_zhidao_assessment_stage", assessment),
                patch.object(Autovisor.Notifier, "from_config", return_value=notifier),
                patch.object(Autovisor.quiz_page.llm, "enabled", return_value=False),
                patch.object(Autovisor.quiz_page.llm, "vision_enabled", return_value=False),
            ]
            with ExitStack() as stack:
                for p in patches:
                    stack.enter_context(p)
                await Autovisor.main(cfg)
            return assessment

        called = await _run_main_with(CourseOutcome.COMPLETED, review_mode=False)
        called.assert_awaited_once()

        skipped_review = await _run_main_with(CourseOutcome.COMPLETED, review_mode=True)
        skipped_review.assert_not_awaited()

        skipped_limit = await _run_main_with(CourseOutcome.TIME_LIMIT, review_mode=False)
        skipped_limit.assert_not_awaited()

        skipped_fail = await _run_main_with(CourseOutcome.FAILED, review_mode=False)
        skipped_fail.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()