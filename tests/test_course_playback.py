import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from modules.course_playback import learn_lesson, review_lesson
from modules.lesson_navigation import HIKE_CATALOG, WISDOM_CATALOG, lesson_progress
from modules.quiz_dom import TargetClosedError
from modules.tasks import video_optimize


class _Config:
    limitMaxTime = 0


class _Logger:
    def __init__(self):
        self.warnings = []
        self.infos = []

    def info(self, msg, *_args, **_kwargs):
        self.infos.append(msg)

    def warn(self, msg, *_args, **_kwargs):
        self.warnings.append(msg)

    def debug(self, *_args, **_kwargs):
        pass

    def event(self, *_args, **_kwargs):
        pass

    def debug_throttled(self, *_args, **_kwargs):
        pass


class _FakeLocator:
    def __init__(self, *, count_val=0, text_val=""):
        self._count = count_val
        self._text = text_val
        self.first = self

    async def count(self):
        return self._count

    async def text_content(self):
        return self._text


class _FakeHikeLesson:
    def __init__(self, *, finished=False, rate_text=""):
        self._finished = finished
        self._rate_text = rate_text

    def locator(self, selector):
        if selector == HIKE_CATALOG.finish:
            return _FakeLocator(count_val=1 if self._finished else 0)
        if selector == HIKE_CATALOG.progress:
            return _FakeLocator(
                count_val=1 if self._rate_text else 0,
                text_val=self._rate_text,
            )
        return _FakeLocator()


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

    async def test_hike_lesson_progress_reads_rate_and_finish_icon(self):
        self.assertEqual(
            await lesson_progress(_FakeHikeLesson(rate_text="57%"), HIKE_CATALOG), 57
        )
        self.assertEqual(
            await lesson_progress(_FakeHikeLesson(finished=True, rate_text="57%"), HIKE_CATALOG),
            100,
        )
        self.assertEqual(
            await lesson_progress(_FakeHikeLesson(), HIKE_CATALOG), 0
        )

    async def test_hike_learn_lesson_aligns_from_end_to_rate_percent_and_completes(self):
        page = SimpleNamespace(
            evaluate=AsyncMock(),
            wait_for_timeout=AsyncMock(),
            is_closed=lambda: False,
        )
        logger = _Logger()
        with (
            patch(
                "modules.course_playback._wait_for_duration",
                new_callable=AsyncMock,
                return_value=(924.0, 0.0),
            ),
            patch(
                "modules.course_playback.lesson_key",
                new_callable=AsyncMock,
                return_value="k1",
            ),
            patch(
                "modules.course_playback.ensure_lesson_unchanged",
                new_callable=AsyncMock,
            ),
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
                "modules.course_playback.lesson_progress",
                new_callable=AsyncMock,
                side_effect=[57, 100],
            ),
            patch(
                "modules.course_playback.wait_for_lesson_completion",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "modules.course_playback.get_video_attr",
                new_callable=AsyncMock,
                side_effect=[924.0],
            ),
            patch("modules.course_playback.asyncio.sleep", new_callable=AsyncMock),
            patch("modules.course_playback.show_course_progress") as show_progress,
        ):
            result = await learn_lesson(
                page, 0, 0, object(), HIKE_CATALOG, _Config(), logger
            )

        self.assertEqual(result, (0.0, True, False))
        # 首帧位于 924s 但平台仅记录 57% -> 应回退到 924 * 0.57 = 526.68s 而非末尾前 5 秒
        page.evaluate.assert_awaited_once()
        seek_args = page.evaluate.await_args.args[1]
        self.assertAlmostEqual(seek_args[1], 924.0 * 0.57, places=2)
        show_progress.assert_called_once_with(desc="平台记录进度:", cur_time="57%")

    async def test_video_optimize_uses_native_speed_tabs_on_hike_and_does_not_force_raw_rate(self):
        class _FakeHikePage:
            url = "https://hike.zhihuishu.com/aidedteaching/sourceLearning/sourceLearning?courseId=11559973&fileId=35338440"

            def __init__(self):
                self.wait_calls = 0

            async def wait_for_load_state(self, *_args, **_kwargs):
                return None

            async def wait_for_selector(self, *_args, **_kwargs):
                self.wait_calls += 1
                if self.wait_calls > 2:
                    raise TargetClosedError("done")
                return None

        page = _FakeHikePage()
        cfg = SimpleNamespace(soundOff=False, limitSpeed=1.8)
        enabled = asyncio.Event()
        enabled.set()
        run_on_mock = AsyncMock()
        hike_speed_mock = AsyncMock(
            side_effect=[
                {
                    "ready": False,
                    "changed": False,
                    "appliedRate": None,
                    "chosenRate": None,
                    "maxRate": None,
                    "clamped": False,
                    "cappedToMax": False,
                },
                {
                    "ready": True,
                    "changed": True,
                    "appliedRate": 1.5,
                    "chosenRate": 1.5,
                    "maxRate": 1.5,
                    "clamped": True,
                    "cappedToMax": True,
                },
            ]
        )

        with (
            patch("modules.tasks.asyncio.sleep", new_callable=AsyncMock),
            patch(
                "modules.tasks.get_video_attr",
                new_callable=AsyncMock,
                side_effect=[0, 1.0, 0, 1.0],
            ),
            patch("modules.tasks.run_on", run_on_mock),
            patch("modules.tasks._apply_hike_playback_rate", hike_speed_mock),
            patch("modules.tasks.logger") as mock_logger,
        ):
            await video_optimize(page, cfg, enabled)

        self.assertEqual(hike_speed_mock.await_count, 2)
        # 绝不直接改写 <video>.playbackRate
        run_on_mock.assert_not_awaited()
        info_messages = [call.args[0] for call in mock_logger.info.call_args_list]
        self.assertTrue(any("1->1.5(翻转/智慧课原生最高 1.5x)" in m for m in info_messages))


if __name__ == "__main__":
    unittest.main()
