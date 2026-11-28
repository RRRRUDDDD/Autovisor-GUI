import unittest
from unittest.mock import AsyncMock, patch

from modules.quiz_dom import TargetClosedError

from modules import tasks


class _DummyLogger:
    def __init__(self):
        self.infos = []
        self.debugs = []
        self.throttled = []
        self.events = []
        self.exceptions = []

    def info(self, msg, shift=False):
        self.infos.append(msg)

    def debug(self, msg):
        self.debugs.append(msg)

    def debug_throttled(self, key, msg, interval=60):
        self.throttled.append((key, msg))

    def event(self, name, interval=None, **fields):
        self.events.append((name, fields))

    def log_exception(self, msg, exc=None, shift=False):
        self.exceptions.append((msg, exc))

    @staticmethod
    def summarize_exception(exc):
        return f"{type(exc).__name__}: {exc}"


class _FakePage:
    def __init__(self, url="https://studyvideoh5.zhihuishu.com/stuStudy"):
        self.url = url
        self.wait_for_load_state = AsyncMock()
        self.wait_for_selector = AsyncMock()


class PlayVideoLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def _run_one_iteration(self, state, play_result):
        page = _FakePage()
        dummy_logger = _DummyLogger()
        call_count = 0

        async def fake_sleep(_delay):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise TargetClosedError("done")

        async def fake_run_on(_page, _selector, action, purpose):
            if purpose == "读取播放器状态":
                return state
            if purpose == "恢复播放":
                self.assertIn("el.play()", action)
                self.assertIn("!el.paused", action)
                return play_result
            raise AssertionError(f"Unexpected run_on purpose: {purpose}")

        with (
            patch.object(tasks, "logger", dummy_logger),
            patch.object(tasks.asyncio, "sleep", side_effect=fake_sleep),
            patch.object(tasks, "run_on", side_effect=fake_run_on),
            patch.object(tasks, "has_blocking_overlay", AsyncMock(return_value=False)),
        ):
            await tasks.play_video(page)
        return dummy_logger

    async def test_logs_success_info_and_event_when_resume_succeeds(self):
        dummy_logger = await self._run_one_iteration(
            {"paused": True, "ended": False, "currentTime": 12.34, "duration": 120.56},
            play_result=True,
        )
        self.assertIn("检测到视频暂停,正在尝试播放.", dummy_logger.infos)
        self.assertIn("尝试播放成功,视频已恢复播放.", dummy_logger.infos)
        self.assertEqual(
            dummy_logger.events,
            [("恢复播放", {"播放器时间": 12.3, "总时长": 120.6})],
        )

    async def test_handles_missing_duration_when_resume_succeeds(self):
        dummy_logger = await self._run_one_iteration(
            {"paused": True, "ended": False, "currentTime": 0.0, "duration": float("nan")},
            play_result=True,
        )
        self.assertIn("尝试播放成功,视频已恢复播放.", dummy_logger.infos)
        self.assertEqual(
            dummy_logger.events,
            [("恢复播放", {"播放器时间": 0.0, "总时长": None})],
        )
        self.assertEqual(dummy_logger.exceptions, [])

    async def test_does_not_log_success_when_video_remains_paused(self):
        dummy_logger = await self._run_one_iteration(
            {"paused": True, "ended": False, "currentTime": 12.0, "duration": 120.0},
            play_result=False,
        )
        self.assertIn("检测到视频暂停,正在尝试播放.", dummy_logger.infos)
        self.assertNotIn("尝试播放成功,视频已恢复播放.", dummy_logger.infos)
        self.assertEqual(dummy_logger.events, [])
        self.assertEqual(len(dummy_logger.throttled), 1)
        self.assertEqual(dummy_logger.throttled[0][0], "video:play_unconfirmed")

    async def test_does_not_log_success_when_play_fails(self):
        dummy_logger = await self._run_one_iteration(
            {"paused": True, "ended": False, "currentTime": 12.0, "duration": 120.0},
            play_result=None,
        )
        self.assertIn("检测到视频暂停,正在尝试播放.", dummy_logger.infos)
        self.assertNotIn("尝试播放成功,视频已恢复播放.", dummy_logger.infos)
        self.assertEqual(dummy_logger.events, [])
        self.assertEqual(dummy_logger.throttled, [])


class SkipQuestionsLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def test_logs_default_fallback_and_summary_when_llm_unavailable(self):
        dummy_logger = _DummyLogger()
        event_loop = tasks.asyncio.Event()

        class _Choice:
            def __init__(self, text):
                self._text = text
                self.clicked = False

            async def inner_text(self):
                return self._text

            async def click(self, timeout=500):
                self.clicked = True

        class _Ques:
            async def click(self, timeout=500):
                return None

        class _Container:
            async def query_selector_all(self, selector):
                if selector == ".number":
                    return [_Ques()]
                return []

        class _Title:
            async def inner_text(self):
                return "地球绕着太阳转吗？"

        choices = [_Choice("A. 是"), _Choice("B. 否")]

        class _QuizPage(_FakePage):
            def __init__(self):
                super().__init__()
                self.wait_for_selector = AsyncMock(return_value=_Container())
                self.press = AsyncMock()
                self.wait_for_timeout = AsyncMock()

            async def query_selector(self, selector):
                if selector == ".answer":
                    return None
                if selector == ".topic-title":
                    return _Title()
                return None

            async def query_selector_all(self, selector):
                if selector == ".topic-item":
                    return choices
                return []

        page = _QuizPage()
        calls = 0

        async def fake_sleep(_delay):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise TargetClosedError("done")

        with (
            patch.object(tasks, "logger", dummy_logger),
            patch.object(tasks.asyncio, "sleep", side_effect=fake_sleep),
            patch.object(tasks, "answer_with_llm", AsyncMock(return_value=False)),
        ):
            await tasks.skip_questions(page, event_loop)

        self.assertTrue(event_loop.is_set())
        self.assertTrue(all(c.clicked for c in choices))
        self.assertTrue(any("检测到课中弹题(共 1 题)" in m for m in dummy_logger.infos))
        self.assertTrue(any("地球绕着太阳转吗" in m and "A. 是" in m for m in dummy_logger.infos))
        self.assertTrue(any("课中弹题处理完成" in m for m in dummy_logger.infos))
        quiz_events = [fields for name, fields in dummy_logger.events if name == "课中答题"]
        self.assertEqual(len(quiz_events), 2)
        self.assertEqual(quiz_events[0]["方式"], "默认选择")
        self.assertEqual(quiz_events[1]["默认选择"], 1)

    async def test_hike_logs_info_once_per_course_and_keeps_running_for_next_course(self):
        dummy_logger = _DummyLogger()
        dummy_logger.warns = []
        dummy_logger.warn = lambda msg, shift=False: dummy_logger.warns.append(msg)
        event_loop = tasks.asyncio.Event()

        class _Choice:
            def __init__(self, text):
                self._text = text
                self.clicked = False

            async def inner_text(self):
                return self._text

            async def click(self, timeout=500):
                self.clicked = True

        class _Ques:
            async def click(self, timeout=500):
                return None

        class _Container:
            async def query_selector_all(self, selector):
                return [_Ques()] if selector == ".number" else []

        choices = [_Choice("A. 对"), _Choice("B. 错")]

        class _SwitchPage(_FakePage):
            def __init__(self):
                super().__init__(
                    "https://hike.zhihuishu.com/aidedteaching/sourceLearning/sourceLearning?courseId=11559973&fileId=35338439"
                )
                self.wait_for_selector = AsyncMock(return_value=_Container())
                self.press = AsyncMock()
                self.wait_for_timeout = AsyncMock()

            async def query_selector(self, selector):
                return None

            async def query_selector_all(self, selector):
                return choices if selector == ".topic-item" else []

        page = _SwitchPage()
        calls = 0

        async def fake_sleep(_delay):
            nonlocal calls
            calls += 1
            if calls == 1:
                # 同一门 Hike 课切换下一节视频，不应重复提示
                page.url = "https://hike.zhihuishu.com/aidedteaching/sourceLearning/sourceLearning?courseId=11559973&fileId=35338440"
            elif calls == 2:
                # 队列切换到知到共享课，弹题任务应继续生效
                page.url = "https://studyvideoh5.zhihuishu.com/stuStudy?recruitAndCourseId=abc"
            elif calls > 3:
                raise TargetClosedError("done")

        with (
            patch.object(tasks, "logger", dummy_logger),
            patch.object(tasks.asyncio, "sleep", side_effect=fake_sleep),
            patch.object(tasks, "answer_with_llm", AsyncMock(return_value=False)),
        ):
            await tasks.skip_questions(page, event_loop)

        self.assertEqual(dummy_logger.warns, [])
        hike_infos = [m for m in dummy_logger.infos if "翻转/智慧课(Hike)" in m]
        self.assertEqual(len(hike_infos), 1)
        self.assertIn(("答题", {"结果": "跳过", "原因": "翻转/智慧课无课中弹题"}), dummy_logger.events)
        self.assertTrue(event_loop.is_set())
        self.assertTrue(all(c.clicked for c in choices))


class _QuietLogger:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


class ClickInterceptedAnswersTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = patch("modules.popup_interceptor.logger", _QuietLogger())
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _payload(*questions):
        return {
            "data": {
                "lessonTestQuestionUseInterfaceDtos": [
                    {
                        "testQuestion": {
                            "questionId": question_id,
                            "name": title,
                            "questionOptions": [
                                {"content": text, "result": result}
                                for text, result in options
                            ],
                        }
                    }
                    for question_id, title, options in questions
                ]
            }
        }

    @staticmethod
    def _choice(text):
        class _Choice:
            def __init__(self):
                self.text = text
                self.clicked = False

            async def inner_text(self):
                return self.text

            async def click(self, timeout=500):
                self.clicked = True

        return _Choice()

    async def test_title_isolates_each_popup_question(self):
        from modules.popup_interceptor import PopupAnswerInterceptor

        class _Title:
            async def inner_text(self):
                return " 地球是圆的 "

        class _Page(_FakePage):
            async def query_selector(self, selector):
                return _Title() if selector == ".topic-title" else None

        interceptor = PopupAnswerInterceptor(_Page())
        interceptor.ingest(
            self._payload(
                (101, "地球是圆的", [("正确", 1), ("错误", 0)]),
                (102, "太阳从西边升起", [("正确", 0), ("错误", 1)]),
            )
        )
        choices = [self._choice("A. 正确"), self._choice("B. 错误")]
        clicked = await tasks.click_intercepted_answers(_Page(), interceptor, choices)
        self.assertTrue(clicked)
        self.assertEqual([choice.clicked for choice in choices], [True, False])

    async def test_conflicting_answers_click_nothing(self):
        from modules.popup_interceptor import PopupAnswerInterceptor

        class _Page(_FakePage):
            async def query_selector(self, selector):
                return None

        interceptor = PopupAnswerInterceptor(_Page())
        interceptor.ingest(
            self._payload(
                (103, "判断题一", [("正确", 1), ("错误", 0)]),
                (104, "判断题二", [("正确", 0), ("错误", 1)]),
            )
        )
        choices = [self._choice("A. 正确"), self._choice("B. 错误")]
        clicked = await tasks.click_intercepted_answers(_Page(), interceptor, choices)
        self.assertFalse(clicked)
        self.assertEqual([choice.clicked for choice in choices], [False, False])


class WaitForVerifyWindowControlTests(unittest.IsolatedAsyncioTestCase):
    async def _run_verify_once(self, *, enable_hide_window: bool, attach_existing_chrome: bool):
        from types import SimpleNamespace

        page = _FakePage()
        event_loop = tasks.asyncio.Event()
        config = SimpleNamespace(
            enableAutoCaptcha=False,
            enableHideWindow=enable_hide_window,
            attach_existing_chrome=attach_existing_chrome,
        )
        dummy_logger = _DummyLogger()
        dummy_logger.warn = lambda msg, shift=False: None
        calls = 0

        async def fake_sleep(_delay):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise TargetClosedError("done")

        display_mock = AsyncMock()
        hide_mock = AsyncMock()
        with (
            patch.object(tasks, "logger", dummy_logger),
            patch.object(tasks.asyncio, "sleep", side_effect=fake_sleep),
            patch.object(tasks, "has_visible_verification", AsyncMock(return_value=True)),
            patch.object(tasks, "wait_until_verification_hidden", AsyncMock()),
            patch.object(tasks, "display_window", display_mock),
            patch.object(tasks, "hide_window", hide_mock),
        ):
            await tasks.wait_for_verify(page, config, event_loop, captcha_modules=None)
        return display_mock, hide_mock, event_loop

    async def test_displays_and_rehides_window_around_manual_verification(self):
        display_mock, hide_mock, event_loop = await self._run_verify_once(
            enable_hide_window=True, attach_existing_chrome=False
        )
        display_mock.assert_awaited_once()
        hide_mock.assert_awaited_once()
        self.assertTrue(event_loop.is_set())

    async def test_skips_window_hide_and_display_when_attached_to_existing_chrome(self):
        display_mock, hide_mock, event_loop = await self._run_verify_once(
            enable_hide_window=True, attach_existing_chrome=True
        )
        display_mock.assert_not_awaited()
        hide_mock.assert_not_awaited()
        self.assertTrue(event_loop.is_set())


if __name__ == "__main__":
    unittest.main()
