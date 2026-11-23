import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from playwright.async_api import TimeoutError

from modules.course_runner import CourseOutcome, run_course
from modules.lesson_navigation import LEGACY_CATALOG


class CourseRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_waits_for_restored_catalog_and_skips_late_completed_lesson(self):
        ready = False
        pending_done = False
        page = Mock()
        page.is_closed = Mock(return_value=False)

        async def wait_for_selector(selector, **kwargs):
            nonlocal ready
            if selector == LEGACY_CATALOG.active:
                ready = True

        page.wait_for_selector = AsyncMock(side_effect=wait_for_selector)
        page.wait_for_timeout = AsyncMock()
        finished = Mock(key="finished", click=AsyncMock())
        pending = Mock(key="pending", click=AsyncMock())

        async def filtered(*args, **kwargs):
            self.assertTrue(ready, "Filtering must wait for restored progress")
            # A lesson can become complete after the initial catalog scan.
            return [finished, pending]

        async def progress(lesson, catalog):
            if lesson is finished or pending_done:
                return 100
            return 50

        async def learn(*args, **kwargs):
            nonlocal pending_done
            pending_done = True
            return (0, True, False)

        with (
            patch("modules.course_runner.get_filtered_class", side_effect=filtered),
            patch("modules.course_runner.lesson_progress", side_effect=progress),
            patch(
                "modules.course_runner.lesson_key",
                AsyncMock(side_effect=lambda lesson, _: lesson.key),
            ),
            patch(
                "modules.course_runner.active_lesson",
                AsyncMock(return_value=None),
            ),
            patch(
                "modules.course_runner.get_lesson_name",
                AsyncMock(return_value="Pending"),
            ),
            patch(
                "modules.course_runner.wait_for_lesson_active",
                AsyncMock(return_value=True),
            ),
            patch(
                "modules.course_runner.learn_lesson",
                AsyncMock(side_effect=learn),
            ) as learn_mock,
        ):
            result = await run_course(
                page, LEGACY_CATALOG, Mock(limitMaxTime=0), Mock(), asyncio.Event()
            )

        self.assertIs(result, CourseOutcome.COMPLETED)
        finished.click.assert_not_awaited()
        pending.click.assert_awaited_once()
        learn_mock.assert_awaited_once()

    async def test_unrestored_catalog_does_not_start_switching_lessons(self):
        page = Mock()
        page.is_closed = Mock(return_value=False)
        page.wait_for_selector = AsyncMock(
            side_effect=[None, TimeoutError("Catalog did not restore selection")]
        )
        with patch("modules.course_runner.get_filtered_class", AsyncMock()) as filtered:
            result = await run_course(
                page, LEGACY_CATALOG, Mock(limitMaxTime=0), Mock(), asyncio.Event()
            )

        self.assertIs(result, CourseOutcome.FAILED)
        filtered.assert_not_awaited()

    async def test_completed_course_is_skipped_when_review_mode_is_off(self):
        page = Mock()
        page.is_closed = Mock(return_value=False)
        page.wait_for_selector = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        lesson = Mock(key="completed", click=AsyncMock())
        with (
            patch(
                "modules.course_runner.get_filtered_class",
                AsyncMock(side_effect=[[], [lesson]]),
            ),
            patch("modules.course_runner.review_lesson", AsyncMock()) as review,
            patch("modules.course_runner.learn_lesson", AsyncMock()) as learn,
        ):
            result = await run_course(
                page,
                LEGACY_CATALOG,
                Mock(limitMaxTime=0, reviewMode=False),
                Mock(),
                asyncio.Event(),
            )

        self.assertIs(result, CourseOutcome.COMPLETED)
        lesson.click.assert_not_awaited()
        review.assert_not_awaited()
        learn.assert_not_awaited()

    async def test_review_mode_still_plays_completed_lessons(self):
        page = Mock()
        page.is_closed = Mock(return_value=False)
        page.wait_for_selector = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        lesson = Mock(key="completed", click=AsyncMock())
        with (
            patch(
                "modules.course_runner.get_filtered_class",
                AsyncMock(side_effect=[[lesson], [lesson], [lesson]]),
            ),
            patch("modules.course_runner.lesson_progress", AsyncMock(return_value=100)),
            patch(
                "modules.course_runner.lesson_key",
                AsyncMock(side_effect=lambda row, _: row.key),
            ),
            patch(
                "modules.course_runner.active_lesson",
                AsyncMock(return_value=None),
            ),
            patch(
                "modules.course_runner.get_lesson_name",
                AsyncMock(return_value="Completed"),
            ),
            patch(
                "modules.course_runner.wait_for_lesson_active",
                AsyncMock(return_value=True),
            ),
            patch(
                "modules.course_runner.review_lesson",
                AsyncMock(return_value=(0, True, False)),
            ) as review,
        ):
            result = await run_course(
                page,
                LEGACY_CATALOG,
                Mock(limitMaxTime=0, reviewMode=True),
                Mock(),
                asyncio.Event(),
            )

        self.assertIs(result, CourseOutcome.COMPLETED)
        lesson.click.assert_awaited_once()
        review.assert_awaited_once()

    async def test_review_mode_loops_until_time_limit_is_reached(self):
        page = Mock()
        page.is_closed = Mock(return_value=False)
        page.wait_for_selector = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        lesson = Mock(key="completed", click=AsyncMock())
        review_calls = 0

        async def fake_review(*args, **kwargs):
            nonlocal review_calls
            review_calls += 1
            if review_calls == 1:
                return (0, True, False)
            return (0, False, True)

        with (
            patch(
                "modules.course_runner.get_filtered_class",
                AsyncMock(return_value=[lesson]),
            ),
            patch("modules.course_runner.lesson_progress", AsyncMock(return_value=100)),
            patch(
                "modules.course_runner.lesson_key",
                AsyncMock(side_effect=lambda row, _: row.key),
            ),
            patch(
                "modules.course_runner.active_lesson",
                AsyncMock(return_value=None),
            ),
            patch(
                "modules.course_runner.get_lesson_name",
                AsyncMock(return_value="Completed"),
            ),
            patch(
                "modules.course_runner.wait_for_lesson_active",
                AsyncMock(return_value=True),
            ),
            patch(
                "modules.course_runner.elapsed_minutes",
                return_value=5.0,
            ),
            patch(
                "modules.course_runner.review_lesson",
                AsyncMock(side_effect=fake_review),
            ) as review,
        ):
            result = await run_course(
                page,
                LEGACY_CATALOG,
                Mock(limitMaxTime=30, reviewMode=True),
                Mock(),
                asyncio.Event(),
            )

        self.assertIs(result, CourseOutcome.TIME_LIMIT)
        self.assertEqual(review.await_count, 2)
