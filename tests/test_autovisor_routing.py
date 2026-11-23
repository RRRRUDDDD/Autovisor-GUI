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


if __name__ == "__main__":
    unittest.main()