# encoding=utf-8
"""任务完成后的系统自动关机调度与取消。"""

from __future__ import annotations

import locale
import subprocess
import sys
import threading
import time
from typing import Callable


DEFAULT_SHUTDOWN_DELAY = 60
SHUTDOWN_OUTCOMES = frozenset({"completed", "time_limit"})
WINDOWS_NO_SHUTDOWN_IN_PROGRESS = 1116
_COMMAND_TIMEOUT = 5


def _decode_output(raw: bytes | str | None) -> str:
    if not raw:
        return ""
    if isinstance(raw, str):
        return raw.strip()
    try:
        return raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        encoding = locale.getpreferredencoding(False) or "utf-8"
        return raw.decode(encoding, errors="replace").strip()


def _creation_flags(platform: str) -> int:
    if platform == "win32":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


def _run_command(args: list[str], *, runner: Callable, platform: str):
    kwargs = {"capture_output": True, "timeout": _COMMAND_TIMEOUT}
    flags = _creation_flags(platform)
    if flags:
        kwargs["creationflags"] = flags
    return runner(args, **kwargs)


def _execute_posix_shutdown(runner: Callable, platform: str) -> None:
    try:
        if platform == "darwin":
            _run_command(
                ["osascript", "-e", 'tell application "System Events" to shut down'],
                runner=runner,
                platform=platform,
            )
            return

        for cmd in (["shutdown", "-h", "now"], ["systemctl", "poweroff"]):
            try:
                result = _run_command(cmd, runner=runner, platform=platform)
            except FileNotFoundError:
                continue
            if result.returncode == 0:
                return
    except Exception:
        return


class ShutdownManager:
    """协调延时系统关机调度与用户取消。"""

    def __init__(
        self,
        *,
        delay: int = DEFAULT_SHUTDOWN_DELAY,
        wall_time: Callable[[], float] = time.time,
        runner: Callable = subprocess.run,
        platform: str | None = None,
    ):
        self._delay = max(1, int(delay))
        self._wall_time = wall_time
        self._runner = runner
        self._platform = platform
        self._lock = threading.RLock()
        self._active_run_id: int | None = None
        self._timer: threading.Timer | None = None

    @property
    def delay(self) -> int:
        return self._delay

    def _current_platform(self) -> str:
        return self._platform if self._platform is not None else sys.platform

    def schedule(self, run_id: int) -> dict:
        """向操作系统提交延时关机计划，并返回前端可展示的状态字典。"""
        with self._lock:
            platform = self._current_platform()
            if platform == "win32":
                comment = f"Autovisor GUI：学习任务已完成，系统将在 {self._delay} 秒后关机"
                result = _run_command(
                    ["shutdown", "/s", "/t", str(self._delay), "/c", comment],
                    runner=self._runner,
                    platform=platform,
                )
                if result.returncode != 0:
                    detail = _decode_output(result.stderr) or _decode_output(result.stdout)
                    raise RuntimeError(detail or f"shutdown 命令返回退出码 {result.returncode}")
                self._active_run_id = run_id
            else:
                if self._timer is not None:
                    self._timer.cancel()
                timer = threading.Timer(
                    self._delay, self._on_posix_timer, args=(run_id, platform)
                )
                timer.daemon = True
                self._timer = timer
                self._active_run_id = run_id
                timer.start()

            now = self._wall_time()
            return {
                "state": "scheduled",
                "delaySeconds": self._delay,
                "scheduledAt": now,
                "shutdownAt": now + self._delay,
            }

    def _on_posix_timer(self, run_id: int, platform: str) -> None:
        with self._lock:
            if self._active_run_id != run_id:
                return
            self._active_run_id = None
            self._timer = None
        _execute_posix_shutdown(self._runner, platform)

    def cancel(self, run_id: int | None = None) -> dict:
        """取消当前操作系统已计划的延时关机。"""
        with self._lock:
            platform = self._current_platform()
            if platform == "win32":
                result = _run_command(
                    ["shutdown", "/a"],
                    runner=self._runner,
                    platform=platform,
                )
                if result.returncode not in (0, WINDOWS_NO_SHUTDOWN_IN_PROGRESS):
                    detail = _decode_output(result.stderr) or _decode_output(result.stdout)
                    raise RuntimeError(detail or f"shutdown /a 返回退出码 {result.returncode}")
                if run_id is None or self._active_run_id == run_id:
                    self._active_run_id = None
            else:
                if self._timer is not None:
                    self._timer.cancel()
                    self._timer = None
                if run_id is None or self._active_run_id == run_id:
                    self._active_run_id = None
            return {"state": "cancelled"}

    def reset(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self._active_run_id = None
