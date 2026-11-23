import unittest
from unittest.mock import AsyncMock, patch

from modules.course_playback import learn_lesson, review_lesson
from modules.lesson_navigation import WISDOM_CATALOG


class _Config:
    limitMaxTime = 0


class _Logger:
    def warn(self, *_args, **_kwargs):
        pass

    def debug(self, *_args, **_kwargs):
        pass

    def event(self, *_args, **_kwargs):
        pass

    def debug_throttled(self, *_args, **_kwargs):
        pass


class CoursePlaybackTests(unittest.IsolatedAsyncioTestCase):
    @patch(
        "modules.course_playback._wait_for_duration",
        new_callable=AsyncMock,
        return_value=(None, 7.5),
    )
    async def test_invalid_metadata_fails_learning_with_three_part_result(self, _wait):
        result = await learn_lesson(
            object(), 0, 0, object(), WISDOM_CATALOG, _Config(), _Logger()
        )
        self.assertEqual(result, (7.5, False, False))

    @patch(
        "modules.course_playback._wait_for_duration",
        new_callable=AsyncMock,
        return_value=(None, 7.5),
    )
    async def test_invalid_metadata_fails_review_with_three_part_result(self, _wait):
        result = await review_lesson(object(), 0, 0, _Config(), _Logger())
        self.assertEqual(result, (7.5, False, False))

    async def test_review_lesson_reports_video_percentage_when_time_limit_is_zero(self):
        with (
            patch(
                "modules.course_playback._wait_for_duration",
                new_callable=AsyncMock,
                return_value=(200.0, 0.0),
            ),
            patch("modules.course_playback.run_on", new_callable=AsyncMock),
            patch(
                "modules.course_playback.has_visible_verification",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "modules.course_playback.has_visible_element",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "modules.course_playback.get_video_attr",
                new_callable=AsyncMock,
                side_effect=[100.0, 200.0],
            ),
            patch("modules.course_playback.asyncio.sleep", new_callable=AsyncMock),
            patch("modules.course_playback.show_course_progress") as show_progress,
        ):
            result = await review_lesson(object(), 0, 0, _Config(), _Logger())

        self.assertEqual(result, (0.0, True, False))
        show_progress.assert_called_once_with(desc="复习进度:", cur_time="50%")


if __name__ == "__main__":
    unittest.main()
