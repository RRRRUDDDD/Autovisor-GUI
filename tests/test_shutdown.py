# encoding=utf-8
import asyncio
import os
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from backend_server import TaskManager
from modules.configs import Config, CONFIG_TEMPLATE
from modules.shutdown import ShutdownManager, WINDOWS_NO_SHUTDOWN_IN_PROGRESS


class _FakeCompleted:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class ShutdownManagerTests(unittest.TestCase):
    def test_windows_schedule_and_cancel_commands(self):
        calls = []

        def runner(args, **kwargs):
            calls.append((args, kwargs))
            return _FakeCompleted(0)

        mgr = ShutdownManager(
            delay=60,
            wall_time=lambda: 1000.0,
            runner=runner,
            platform="win32",
        )
        info = mgr.schedule(1)
        self.assertEqual(info["state"], "scheduled")
        self.assertEqual(info["delaySeconds"], 60)
        self.assertEqual(info["scheduledAt"], 1000.0)
        self.assertEqual(info["shutdownAt"], 1060.0)
        self.assertEqual(calls[0][0][:4], ["shutdown", "/s", "/t", "60"])
        self.assertEqual(calls[0][0][4], "/c")
        self.assertIn("60 秒后关机", calls[0][0][5])

        cancelled = mgr.cancel(1)
        self.assertEqual(cancelled, {"state": "cancelled"})
        self.assertEqual(calls[1][0], ["shutdown", "/a"])

    def test_windows_cancel_tolerates_1116_and_raises_on_other_errors(self):
        mgr = ShutdownManager(
            runner=lambda args, **kwargs: _FakeCompleted(WINDOWS_NO_SHUTDOWN_IN_PROGRESS),
            platform="win32",
        )
        self.assertEqual(mgr.cancel(1), {"state": "cancelled"})

        mgr_err = ShutdownManager(
            runner=lambda args, **kwargs: _FakeCompleted(5, stderr="拒绝访问".encode("utf-8")),
            platform="win32",
        )
        with self.assertRaisesRegex(RuntimeError, "拒绝访问"):
            mgr_err.cancel(1)

    def test_windows_schedule_raises_on_nonzero_exit(self):
        mgr = ShutdownManager(
            runner=lambda args, **kwargs: _FakeCompleted(1, stderr="权限不足".encode("utf-8")),
            platform="win32",
        )
        with self.assertRaisesRegex(RuntimeError, "权限不足"):
            mgr.schedule(1)

    def test_posix_timer_can_be_cancelled_before_firing(self):
        calls = []
        mgr = ShutdownManager(
            delay=1,
            runner=lambda args, **kwargs: calls.append(args) or _FakeCompleted(0),
            platform="linux",
        )
        mgr.schedule(1)
        mgr.cancel(1)
        time.sleep(0.05)
        self.assertEqual(calls, [])


class ShutdownConfigTests(unittest.TestCase):
    def test_config_reads_shutdown_on_complete_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "data"), exist_ok=True)
            mirrors_path = os.path.join(tmp, "data", "mirrors.json")
            with open(mirrors_path, "w", encoding="utf-8") as fh:
                fh.write('{"Official": "https://pypi.org"}\n')
            cfg_path = os.path.join(tmp, "config.ini")
            with open(cfg_path, "w", encoding="utf-8") as fh:
                fh.write(CONFIG_TEMPLATE)
            cfg_default = Config(cfg_path, mirrors_path)
            self.assertFalse(cfg_default.shutdownOnComplete)

            with open(cfg_path, "w", encoding="utf-8") as fh:
                fh.write(CONFIG_TEMPLATE.replace("shutdownOnComplete = False", "shutdownOnComplete = True"))
            cfg_enabled = Config(cfg_path, mirrors_path)
            self.assertTrue(cfg_enabled.shutdownOnComplete)


class TaskManagerShutdownTests(unittest.TestCase):
    def _prepare_dir(self, tmp, *, shutdown_on_complete=True, course_url="https://studyvideoh5.zhihuishu.com/stuStudy?recruitAndCourseId=demo1"):
        prev = os.environ.get("AUTOVISOR_DATA_DIR")
        os.environ["AUTOVISOR_DATA_DIR"] = tmp
        self.addCleanup(
            lambda: os.environ.__setitem__("AUTOVISOR_DATA_DIR", prev)
            if prev is not None
            else os.environ.pop("AUTOVISOR_DATA_DIR", None)
        )
        os.makedirs(os.path.join(tmp, "data"), exist_ok=True)
        os.makedirs(os.path.join(tmp, "logs"), exist_ok=True)
        with open(os.path.join(tmp, "data", "mirrors.json"), "w", encoding="utf-8") as fh:
            fh.write('{"Official": "https://pypi.org"}\n')
        with open(os.path.join(tmp, "data", "llm.json"), "w", encoding="utf-8") as fh:
            fh.write('{"api_key": ""}\n')
        content = CONFIG_TEMPLATE.replace(
            "shutdownOnComplete = False",
            f"shutdownOnComplete = {'True' if shutdown_on_complete else 'False'}",
        ).replace(
            "URL1 =",
            f"URL1 = {course_url}",
        )
        with open(os.path.join(tmp, "config.ini"), "w", encoding="utf-8") as fh:
            fh.write(content)

    def _wait_terminal(self, mgr: TaskManager, timeout: float = 5.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snap = mgr.snapshot()
            if snap["status"] not in ("running", "idle"):
                return snap
            time.sleep(0.02)
        return mgr.snapshot()

    def test_study_completion_schedules_shutdown_and_can_be_cancelled(self):
        calls = []

        def runner(args, **kwargs):
            calls.append(args)
            return _FakeCompleted(0)

        with tempfile.TemporaryDirectory() as tmp:
            self._prepare_dir(tmp, shutdown_on_complete=True)
            sm = ShutdownManager(delay=60, wall_time=lambda: 2000.0, runner=runner, platform="win32")
            mgr = TaskManager(tmp, shutdown_manager=sm)
            try:
                async def fake_main(config):
                    return True

                with mock.patch("Autovisor.main", side_effect=fake_main):
                    mgr.start_task("study")
                    snap = self._wait_terminal(mgr)

                self.assertEqual(snap["status"], "completed")
                self.assertEqual(snap["shutdown"]["state"], "scheduled")
                self.assertEqual(snap["shutdown"]["delaySeconds"], 60)
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0][:4], ["shutdown", "/s", "/t", "60"])

                after_cancel = mgr.cancel_shutdown()
                self.assertFalse(after_cancel["shutdownOnComplete"])
                self.assertEqual(after_cancel["shutdown"]["state"], "cancelled")
                self.assertEqual(calls[1], ["shutdown", "/a"])
            finally:
                mgr.shutdown()

    def test_study_time_limit_also_schedules_shutdown(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            self._prepare_dir(tmp, shutdown_on_complete=True)
            sm = ShutdownManager(
                delay=60,
                wall_time=lambda: 3000.0,
                runner=lambda args, **kwargs: calls.append(args) or _FakeCompleted(0),
                platform="win32",
            )
            mgr = TaskManager(tmp, shutdown_manager=sm)
            try:
                async def fake_main(config):
                    mgr.on_logger_event("event", {"name": "达到课程时限", "fields": {"时限分钟": 30}})
                    return True

                with mock.patch("Autovisor.main", side_effect=fake_main):
                    mgr.start_task("study")
                    snap = self._wait_terminal(mgr)

                self.assertEqual(snap["status"], "time_limit")
                self.assertEqual(snap["shutdown"]["state"], "scheduled")
                self.assertEqual(len(calls), 1)
            finally:
                mgr.shutdown()

    def test_error_and_manual_stop_do_not_schedule_shutdown(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            self._prepare_dir(tmp, shutdown_on_complete=True)
            sm = ShutdownManager(
                delay=60,
                runner=lambda args, **kwargs: calls.append(args) or _FakeCompleted(0),
                platform="win32",
            )
            mgr = TaskManager(tmp, shutdown_manager=sm)
            try:
                async def fake_fail(config):
                    return False

                with mock.patch("Autovisor.main", side_effect=fake_fail):
                    mgr.start_task("study")
                    snap = self._wait_terminal(mgr)
                self.assertEqual(snap["status"], "error")
                self.assertIsNone(snap["shutdown"])
                self.assertEqual(calls, [])

                started = threading.Event()

                async def fake_hang(config):
                    started.set()
                    await asyncio.sleep(30)
                    return True

                with mock.patch("Autovisor.main", side_effect=fake_hang):
                    mgr.start_task("study")
                    self.assertTrue(started.wait(timeout=3.0))
                    snap_stopped = mgr.stop_task()
                self.assertEqual(snap_stopped["status"], "cancelled")
                self.assertIsNone(snap_stopped["shutdown"])
                self.assertEqual(calls, [])
            finally:
                mgr.shutdown()

    def test_cancel_while_running_disarms_shutdown(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            self._prepare_dir(tmp, shutdown_on_complete=True)
            sm = ShutdownManager(
                delay=60,
                runner=lambda args, **kwargs: calls.append(args) or _FakeCompleted(0),
                platform="win32",
            )
            mgr = TaskManager(tmp, shutdown_manager=sm)
            try:
                started = threading.Event()
                release = threading.Event()

                async def fake_study(config):
                    started.set()
                    while not release.is_set():
                        await asyncio.sleep(0.02)
                    return True

                with mock.patch("Autovisor.main", side_effect=fake_study):
                    mgr.start_task("study")
                    self.assertTrue(started.wait(timeout=3.0))
                    disarmed = mgr.cancel_shutdown()
                    self.assertFalse(disarmed["shutdownOnComplete"])
                    self.assertEqual(disarmed["shutdown"]["state"], "cancelled")
                    release.set()
                    snap = self._wait_terminal(mgr)

                self.assertEqual(snap["status"], "completed")
                self.assertEqual(snap["shutdown"]["state"], "cancelled")
                self.assertEqual(calls, [])
            finally:
                mgr.shutdown()

    def test_starting_new_task_cancels_previous_scheduled_shutdown(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            self._prepare_dir(tmp, shutdown_on_complete=True)
            sm = ShutdownManager(
                delay=60,
                runner=lambda args, **kwargs: calls.append(args) or _FakeCompleted(0),
                platform="win32",
            )
            mgr = TaskManager(tmp, shutdown_manager=sm)
            try:
                async def fake_ok(config):
                    return True

                with mock.patch("Autovisor.main", side_effect=fake_ok):
                    mgr.start_task("study")
                    snap1 = self._wait_terminal(mgr)
                    self.assertEqual(snap1["shutdown"]["state"], "scheduled")

                # Update config to disable shutdown and start another run
                self._prepare_dir(tmp, shutdown_on_complete=False)
                with mock.patch("Autovisor.main", side_effect=fake_ok):
                    mgr.start_task("study")
                    snap2 = self._wait_terminal(mgr)
                self.assertEqual(snap2["status"], "completed")
                self.assertIsNone(snap2["shutdown"])
                self.assertEqual(calls[0][:4], ["shutdown", "/s", "/t", "60"])
                self.assertEqual(calls[1], ["shutdown", "/a"])
            finally:
                mgr.shutdown()


if __name__ == "__main__":
    unittest.main()
