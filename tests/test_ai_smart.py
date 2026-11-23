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


if __name__ == "__main__":
    unittest.main()
