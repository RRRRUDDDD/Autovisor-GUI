# encoding=utf-8
"""内置无感后端任务调度器、SSE 增量广播与 CDP 验证码求解桥接。"""
from __future__ import annotations

import asyncio
from collections import deque
import os
import queue
import sys
import threading
import time

from modules.backend.state_reducer import (
    apply_context_fields,
    apply_named_event,
    default_run_state,
)
from modules.runtime_bootstrap import log_runtime_config, log_runtime_environment
from modules.shutdown import SHUTDOWN_OUTCOMES, ShutdownManager

LOG_LIMIT = 800
SSE_COALESCE_SECS = 0.15


def _now_secs() -> float:
    return time.time()


def _load_captcha_modules():
    """加载验证码识别依赖, 返回 [numpy, cv2]; 不可用时返回 None。"""
    try:
        import numpy
        import cv2  # noqa: F401
        return [numpy, cv2]
    except ImportError:
        pass
    try:
        from modules import installer
        installer.add_runtime_search_paths(installer.get_res_dir())
        import numpy
        import cv2  # noqa: F401
        return [numpy, cv2]
    except Exception:
        return None


def _pick_captcha_page(pages, page_url: str):
    """从 CDP 已打开的页面中选出登录页。"""
    if not pages:
        return None
    target = (page_url or "").strip()
    for page in pages:
        if target and page.url == target:
            return page
    for page in pages:
        if "zhihuishu.com" in (page.url or ""):
            return page
    return pages[-1]


async def _solve_captcha_via_cdp(
    cdp_url: str, page_url: str, appear_timeout_ms: int, max_attempts: int
) -> dict:
    """连接 GUI 登录浏览器, 识别并完成易盾/腾讯滑块或腾讯点选验证。"""
    from playwright.async_api import async_playwright

    from modules.click_captcha import resolve_click_captcha_target, solve_click_captcha
    from modules.login import is_login_page
    from modules.slider import resolve_captcha_target, slider_verify
    from modules.tencent_slider import resolve_tencent_slider_target

    bs_mod = sys.modules.get("backend_server")
    loader = getattr(bs_mod, "_load_captcha_modules", _load_captcha_modules)
    modules = loader()
    if modules is None:
        return {"solved": False, "reason": "验证码识别依赖不可用"}

    async with async_playwright() as playwright:
        browser = await playwright.chromium.connect_over_cdp(cdp_url)
        try:
            pages = []
            for context in browser.contexts:
                pages.extend(context.pages)
            page = _pick_captcha_page(pages, page_url)
            if page is None:
                return {"solved": False, "reason": "未找到登录页"}

            deadline = time.monotonic() + max(1.0, appear_timeout_ms / 1000)
            kind = None
            while time.monotonic() < deadline:
                if (
                    await resolve_captcha_target(page) is not None
                    or await resolve_tencent_slider_target(page) is not None
                ):
                    kind = "slider"
                    break
                if await resolve_click_captcha_target(page) is not None:
                    kind = "click"
                    break
                if not is_login_page(page.url):
                    return {"solved": False, "reason": "登录页已跳转"}
                await asyncio.sleep(0.25)

            if kind is None:
                return {"solved": False, "reason": "未检测到验证码"}
            if kind == "slider":
                solved = await slider_verify(
                    page,
                    modules,
                    appear_timeout_ms=appear_timeout_ms,
                    max_attempts=max_attempts,
                )
            else:
                solved = await solve_click_captcha(page, modules, attempts=max_attempts)
            return {"solved": bool(solved), "type": kind}
        finally:
            try:
                await asyncio.wait_for(browser.close(), timeout=3.0)
            except Exception:
                pass


class TaskManager:
    """单任务执行器与结构化 RunState 状态中心。"""

    def __init__(self, data_dir: str, shutdown_manager: ShutdownManager | None = None):
        self.data_dir = os.path.abspath(data_dir)
        self._lock = threading.Lock()
        self._shutdown_lock = threading.RLock()
        self._shutdown_manager = shutdown_manager or ShutdownManager()
        self._subscribers: set[queue.Queue] = set()
        self._state = self._default_state()
        self._log_ring: deque[dict] = deque(maxlen=LOG_LIMIT)
        self._next_seq = 1
        self._run_id = 0
        self._revision = 0
        self._cancel_requested = False
        self._cancel_event: threading.Event | None = None
        self._shutdown_cancelled_for_run = False
        self._active_task: asyncio.Task | None = None
        self._active_future = None
        self._active_logger = None
        self._debug_log_enabled = False
        self._closed = False
        self._pending_state_dirty = False
        self._pending_snapshot_reset = False
        self._pending_logs: list[dict] = []
        self._broadcast_timer: threading.Timer | None = None
        self._last_flush_at = 0.0

        # 独立 asyncio 事件循环线程，避免阻塞 HTTP 服务线程
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._run_loop, name="autovisor-asyncio", daemon=True
        )
        self._loop_thread.start()

    @staticmethod
    def _default_state() -> dict:
        return default_run_state()

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def snapshot(self) -> dict:
        with self._lock:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> dict:
        snap = dict(self._state)
        snap["runId"] = self._run_id
        snap["revision"] = self._revision
        snap["completedLessons"] = list(self._state["completedLessons"])
        snap["logs"] = list(self._log_ring)
        shutdown = self._state.get("shutdown")
        snap["shutdown"] = dict(shutdown) if isinstance(shutdown, dict) else None
        return snap

    def _state_delta_locked(self) -> dict:
        delta = dict(self._state)
        delta["runId"] = self._run_id
        delta["revision"] = self._revision
        delta.pop("logs", None)
        delta["completedLessons"] = list(self._state["completedLessons"])
        shutdown = self._state.get("shutdown")
        delta["shutdown"] = dict(shutdown) if isinstance(shutdown, dict) else None
        return delta

    def _enqueue_subscriber_locked(
        self, q: queue.Queue, frames: list[tuple[str, dict]]
    ) -> None:
        for frame in frames:
            try:
                q.put_nowait(frame)
            except queue.Full:
                # 慢消费者队列溢出：清空旧增量并下发完整快照自愈，避免丢日志或状态漂移
                while True:
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        break
                try:
                    q.put_nowait(("snapshot", self._snapshot_locked()))
                except queue.Full:
                    pass
                return

    def _flush_pending_locked(self) -> None:
        if self._broadcast_timer is not None:
            self._broadcast_timer.cancel()
            self._broadcast_timer = None
        if not self._subscribers:
            self._pending_snapshot_reset = False
            self._pending_state_dirty = False
            self._pending_logs.clear()
            return
        frames: list[tuple[str, dict]] = []
        if self._pending_snapshot_reset:
            frames.append(("snapshot", self._snapshot_locked()))
            self._pending_snapshot_reset = False
            self._pending_state_dirty = False
            self._pending_logs.clear()
        else:
            if self._pending_state_dirty:
                frames.append(("state", self._state_delta_locked()))
                self._pending_state_dirty = False
            if self._pending_logs:
                frames.append(
                    (
                        "log",
                        {
                            "runId": self._run_id,
                            "revision": self._revision,
                            "entries": list(self._pending_logs),
                            "logsTruncated": bool(self._state.get("logsTruncated")),
                        },
                    )
                )
                self._pending_logs.clear()
        if not frames:
            return
        self._last_flush_at = time.monotonic()
        for q in list(self._subscribers):
            self._enqueue_subscriber_locked(q, frames)

    def _flush_coalesced(self, timer: threading.Timer) -> None:
        with self._lock:
            if self._broadcast_timer is not timer:
                return
            self._broadcast_timer = None
            if self._closed:
                return
            self._flush_pending_locked()

    def _broadcast_locked(
        self,
        *,
        state_dirty: bool = True,
        urgent: bool = False,
        reset_snapshot: bool = False,
    ):
        self._revision += 1
        self._state["revision"] = self._revision
        if reset_snapshot:
            self._pending_snapshot_reset = True
            self._pending_state_dirty = False
            self._pending_logs.clear()
        elif state_dirty:
            self._pending_state_dirty = True

        if not self._subscribers:
            self._pending_snapshot_reset = False
            self._pending_state_dirty = False
            self._pending_logs.clear()
            return

        now = time.monotonic()
        elapsed = now - self._last_flush_at
        if urgent or reset_snapshot or elapsed >= SSE_COALESCE_SECS:
            self._flush_pending_locked()
            return

        if self._broadcast_timer is None and not self._closed:
            delay = max(0.01, SSE_COALESCE_SECS - elapsed)
            timer_holder: list[threading.Timer] = []
            timer = threading.Timer(delay, lambda: self._flush_coalesced(timer_holder[0]))
            timer_holder.append(timer)
            timer.daemon = True
            self._broadcast_timer = timer
            timer.start()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=64)
        with self._lock:
            self._subscribers.add(q)
            q.put_nowait(("snapshot", self._snapshot_locked()))
        return q

    def unsubscribe(self, q: queue.Queue):
        with self._lock:
            self._subscribers.discard(q)

    def _push_log_locked(self, level: str, message: str):
        text = str(message or "").strip()
        if not text:
            return
        if len(self._log_ring) >= LOG_LIMIT:
            self._state["logsTruncated"] = True
        entry = {
            "seq": self._next_seq,
            "timestamp": _now_secs(),
            "level": level,
            "message": text,
        }
        self._log_ring.append(entry)
        if self._subscribers:
            self._pending_logs.append(entry)
        self._next_seq += 1

    # ---- Logger & Progress 结构化事件回调 ----

    def on_progress(
        self,
        desc: str,
        percent: float,
        remaining: float | None,
        *,
        run_id: int | None = None,
    ):
        pct = max(0.0, min(100.0, float(percent)))
        with self._lock:
            if run_id is not None and self._run_id != run_id:
                return
            if self._state["status"] != "running":
                return
            changed = (
                abs(self._state["lessonProgress"] - pct) >= 0.5
                or self._state["remainingMinutes"] != remaining
                or (desc and self._state["progressDesc"] != desc)
            )
            self._state["lessonProgress"] = pct
            self._state["remainingMinutes"] = remaining
            if desc:
                self._state["progressDesc"] = desc
            if changed:
                self._broadcast_locked()

    def on_logger_event(self, kind: str, payload: dict, *, run_id: int | None = None):
        with self._lock:
            if run_id is not None and self._run_id != run_id:
                return
            if self._state["status"] not in ("running", "stopping") and kind != "log_file":
                return
            changed = False
            if kind == "section":
                title = str(payload.get("title") or "").strip()
                if title:
                    self._state["stage"] = title
                    self._push_log_locked("info", f"── {title} ──")
                    changed = True
            elif kind == "context":
                changed = apply_context_fields(self._state, payload.get("fields") or {})
            elif kind == "event":
                name = str(payload.get("name") or "")
                fields = payload.get("fields") or {}
                changed = apply_named_event(self._state, name, fields)
            elif kind == "log_file":
                path = str(payload.get("path") or "").strip()
                if path:
                    self._state["logFilePath"] = path
                    changed = True
            elif kind == "log":
                level = str(payload.get("level") or "info")
                msg = str(payload.get("message") or "").strip()
                if not msg:
                    return
                state_dirty = False
                if level == "error":
                    self._state["errorMessage"] = msg
                    state_dirty = True
                self._push_log_locked(level, msg)
                self._broadcast_locked(state_dirty=state_dirty)
                return

            if changed:
                self._broadcast_locked(state_dirty=True)

    # ---- 任务启动 / 停止 / 关闭 ----

    def start_task(self, mode: str, target: str = "") -> dict:
        valid_modes = {
            "study",
            "check_browser",
            "check_course",
            "import_cookies",
        }
        if mode not in valid_modes:
            raise ValueError(f"不支持的任务模式: {mode}")

        with self._shutdown_lock:
            prev_scheduled = False
            with self._lock:
                if self._closed:
                    raise RuntimeError("内置服务正在关闭")
                if self._state["status"] in ("running", "stopping"):
                    raise RuntimeError("当前已有任务正在运行或正在停止，请先等待停止后再启动新任务")
                if self._active_future is not None and not self._active_future.done():
                    raise RuntimeError("上一轮任务仍在收尾中，请稍后再启动新任务")
                prev_shutdown = self._state.get("shutdown")
                prev_scheduled = (
                    isinstance(prev_shutdown, dict)
                    and prev_shutdown.get("state") == "scheduled"
                )
            if prev_scheduled:
                try:
                    self._shutdown_manager.cancel()
                except Exception:
                    pass
            with self._lock:
                if self._closed:
                    raise RuntimeError("内置服务正在关闭")
                if self._state["status"] in ("running", "stopping") or (
                    self._active_future is not None and not self._active_future.done()
                ):
                    raise RuntimeError("当前已有任务正在运行或正在停止，请先等待停止后再启动新任务")
                self._log_ring.clear()
                self._next_seq = 1
                self._run_id += 1
                run_id = self._run_id
                self._cancel_requested = False
                cancel_event = threading.Event()
                self._cancel_event = cancel_event
                self._shutdown_cancelled_for_run = False
                self._state = self._default_state()
                self._state["runId"] = run_id
                self._state["status"] = "running"
                self._state["mode"] = mode
                self._state["startedAt"] = _now_secs()
                self._state["stage"] = "初始化"
                if prev_scheduled:
                    self._push_log_locked("info", "已自动取消上一轮任务的系统关机倒计时")
                self._push_log_locked("info", "内置学习引擎已启动，正在初始化任务...")
                self._broadcast_locked(reset_snapshot=True, urgent=True)
                snap = self._snapshot_locked()
                self._active_future = asyncio.run_coroutine_threadsafe(
                    self._execute_task(mode, target, run_id, cancel_event), self._loop
                )

        return snap

    def set_debug_log(self, enabled: bool) -> bool:
        flag = bool(enabled)
        with self._lock:
            self._debug_log_enabled = flag
            active_logger = self._active_logger
        if active_logger is not None:
            active_logger.emit_debug = flag
        return flag

    def cancel_shutdown(self) -> dict:
        with self._shutdown_lock:
            with self._lock:
                shutdown = self._state.get("shutdown")
                armed = bool(self._state.get("shutdownOnComplete"))
                run_id = self._run_id

            if isinstance(shutdown, dict) and shutdown.get("state") == "scheduled":
                self._shutdown_manager.cancel(run_id)
                with self._lock:
                    self._shutdown_cancelled_for_run = True
                    self._state["shutdownOnComplete"] = False
                    self._state["shutdown"] = {"state": "cancelled"}
                    self._push_log_locked("info", "已取消自动关机计划，系统不会自动关闭")
                    self._broadcast_locked(urgent=True)
                    return self._snapshot_locked()

            if armed:
                with self._lock:
                    self._shutdown_cancelled_for_run = True
                    self._state["shutdownOnComplete"] = False
                    self._state["shutdown"] = {"state": "cancelled"}
                    self._push_log_locked("info", "已取消当前任务的完成后自动关机")
                    self._broadcast_locked(urgent=True)
                    return self._snapshot_locked()

            return self.snapshot()

    def stop_task(self, wait_timeout: float = 4.0) -> dict:
        with self._lock:
            self._cancel_requested = True
            if self._cancel_event is not None:
                self._cancel_event.set()
            task = self._active_task
            fut = self._active_future
            run_id = self._run_id
            if self._state["status"] == "running":
                self._state["status"] = "stopping"
                self._state["stage"] = "正在停止..."
                self._state["needsAttention"] = ""
                self._push_log_locked("warning", "收到停止指令，正在优雅关闭浏览器并保存进度...")
                self._broadcast_locked(urgent=True)

        if task is not None and not task.done():
            try:
                self._loop.call_soon_threadsafe(task.cancel)
            except Exception:
                pass
        elif fut is not None and not fut.done():
            fut.cancel()

        if fut is not None:
            try:
                fut.result(timeout=wait_timeout)
            except Exception:
                pass

        with self._lock:
            finished = fut is None or fut.done()
            if finished and self._run_id == run_id and self._state["status"] in ("running", "stopping"):
                self._state["status"] = "cancelled"
                self._state["stage"] = "已停止"
                self._state["finishedAt"] = _now_secs()
                self._state["needsAttention"] = ""
                if self._active_future is fut:
                    self._active_future = None
                self._broadcast_locked(urgent=True)
            return self._snapshot_locked()

    def shutdown(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._cancel_requested = True
            if self._cancel_event is not None:
                self._cancel_event.set()
            if self._broadcast_timer is not None:
                self._broadcast_timer.cancel()
                self._broadcast_timer = None
        try:
            self.stop_task(wait_timeout=3.0)
        except Exception:
            pass
        try:
            self._shutdown_manager.reset()
        except Exception:
            pass
        if (
            self._loop_thread.is_alive()
            and threading.current_thread() is not self._loop_thread
            and not self._loop.is_closed()
        ):
            async def _cancel_remaining():
                current = asyncio.current_task()
                pending = [
                    t for t in asyncio.all_tasks() if t is not current and not t.done()
                ]
                for t in pending:
                    t.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)

            try:
                cancel_fut = asyncio.run_coroutine_threadsafe(_cancel_remaining(), self._loop)
                cancel_fut.result(timeout=2.0)
            except Exception:
                pass
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception:
                pass
            self._loop_thread.join(timeout=3.0)
        if not self._loop_thread.is_alive() and not self._loop.is_closed():
            try:
                self._loop.close()
            except Exception:
                pass
        try:
            from modules import llm

            llm.close_all_http_sessions()
        except Exception:
            pass

    def solve_captcha(
        self,
        cdp_url: str,
        page_url: str,
        appear_ms: int,
        attempts: int,
        timeout: float = 65.0,
    ) -> dict:
        with self._lock:
            if self._closed:
                raise RuntimeError("后端正在关闭")
        inner_timeout = max(0.5, float(timeout))
        bs_mod = sys.modules.get("backend_server")
        solve_fn = getattr(bs_mod, "_solve_captcha_via_cdp", _solve_captcha_via_cdp)
        fut = asyncio.run_coroutine_threadsafe(
            asyncio.wait_for(
                solve_fn(cdp_url, page_url, appear_ms, attempts),
                timeout=inner_timeout,
            ),
            self._loop,
        )
        try:
            return fut.result(timeout=inner_timeout + 5.0)
        except Exception:
            fut.cancel()
            raise

    async def _execute_task(
        self,
        mode: str,
        target: str,
        run_id: int = 0,
        cancel_event: threading.Event | None = None,
    ):
        import Autovisor
        from modules import installer, llm, progress
        from modules.configs import Config, ConfigError, ensure_config_files
        from modules.diagnostics import check_browser, check_course
        from modules.logger import Logger
        from modules.notifier import Notifier
        from modules.utils import get_runtime_path, import_zhihuishu_cookies

        logger = Logger()
        with self._lock:
            if self._run_id != run_id:
                return
            logger.emit_debug = self._debug_log_enabled
            already_cancelled = self._cancel_requested or (
                cancel_event is not None and cancel_event.is_set()
            )
            self._active_task = asyncio.current_task()
            self._active_logger = logger
        Autovisor.logger = logger

        exit_code = 0
        error_msg = ""
        log_file = ""
        try:
            if already_cancelled:
                raise asyncio.CancelledError()
            config_path = os.path.join(self.data_dir, "config.ini")
            mirrors_path = os.path.join(self.data_dir, "data", "mirrors.json")
            created_files = ensure_config_files(config_path)
            config = Config(config_path, mirrors_path)
            if cancel_event is not None:
                setattr(config, "_cancel_event", cancel_event)
            # 先按用户配置维护旧日志, 再切换本轮日志, 避免用默认保留期误删
            logger.configure_from_config(config)
            log_file = logger.start_new_run()
            logger.set_event_sink(
                lambda kind, payload: self.on_logger_event(kind, payload, run_id=run_id)
            )
            progress.set_progress_sink(
                lambda desc, percent, remaining: self.on_progress(
                    desc, percent, remaining, run_id=run_id
                )
            )

            with self._lock:
                if self._run_id != run_id:
                    return
                self._state["logFilePath"] = log_file
                self._broadcast_locked()

            logger.section("初始化")
            log_runtime_environment(
                logger,
                run_mode="桌面端内置后端(打包)" if getattr(sys, "frozen", False) else "桌面端内置后端(源码)",
                log_file=log_file,
                extra_fields={"任务模式": mode, "数据目录": self.data_dir},
            )
            installer.validate_python_version()
            for path in created_files:
                logger.info(f"已生成默认配置文件: {path}", shift=True)
            cfg_debug = bool(getattr(config, "enableDebugLog", False))
            with self._lock:
                debug_enabled = cfg_debug or self._debug_log_enabled
                self._debug_log_enabled = debug_enabled
            logger.emit_debug = debug_enabled
            if mode == "study":
                with self._lock:
                    self._state["reviewMode"] = bool(getattr(config, "reviewMode", False))
                    if not self._shutdown_cancelled_for_run:
                        self._state["shutdownOnComplete"] = bool(
                            getattr(config, "shutdownOnComplete", False)
                        )
                    self._broadcast_locked()
            cookie_path = get_runtime_path("data", "cookies.json")
            Autovisor.COOKIE_PATH = cookie_path
            llm.reset_cache()
            llm.set_proxy(getattr(config, "proxy", ""))
            with Notifier.from_config(config, logger=logger) as notifier_preview:
                configured_channels = ",".join(notifier_preview.configured_channels()) or "未配置"
            logger.context(配置文件=config_path, 任务模式=mode)
            log_runtime_config(
                logger,
                config,
                configured_channels=configured_channels,
                llm_module=llm,
            )

            if mode == "import_cookies":
                count = import_zhihuishu_cookies(target, cookie_path)
                logger.info(f"已安全导入 {count} 条智慧树 Cookie.", shift=True)
                logger.event("导入Cookies", 条数=count, 目标=cookie_path)
                exit_code = 0
            elif mode == "check_browser":
                exit_code = await check_browser(config, logger, cookie_path)
            elif mode == "check_course":
                exit_code = await check_course(target, config, logger, cookie_path)
            elif mode == "study":
                if not config.course_urls:
                    logger.error("未检测到有效课程链接，请先勾选或添加课程！")
                    exit_code = 2
                else:
                    ok = await Autovisor.main(config)
                    exit_code = 0 if ok else 1
            if exit_code == 0:
                logger.info("任务已正常完成。")
        except asyncio.CancelledError:
            exit_code = -1
            logger.warn("学习任务已由用户取消。", shift=True)
        except ConfigError as exc:
            exit_code = 1
            error_msg = f"配置无效：{exc}"
            logger.error(error_msg, shift=True)
        except Exception as exc:
            exit_code = 1
            error_msg = f"{type(exc).__name__}: {exc}"
            logger.log_exception("任务执行过程中出现异常.", exc, shift=True)
        finally:
            with self._lock:
                is_current_run = self._run_id == run_id
                if is_current_run and self._active_logger is logger:
                    self._active_logger = None
            if is_current_run:
                logger.emit_debug = False
                try:
                    logger.save(inform=False)
                except Exception:
                    pass
                logger.clear_context()
                logger.set_event_sink(None)
                progress.set_progress_sink(None)

                with self._shutdown_lock:
                    with self._lock:
                        if self._run_id != run_id:
                            return
                        cancelled = self._cancel_requested or exit_code == -1
                        if cancelled:
                            terminal_status = "cancelled"
                        elif exit_code == 0:
                            terminal_status = (
                                "time_limit"
                                if self._state["status"] == "time_limit"
                                else "completed"
                            )
                        else:
                            terminal_status = "error"
                        should_schedule = (
                            mode == "study"
                            and terminal_status in SHUTDOWN_OUTCOMES
                            and bool(self._state.get("shutdownOnComplete"))
                            and not self._shutdown_cancelled_for_run
                            and not self._closed
                        )

                    shutdown_info = None
                    if should_schedule:
                        try:
                            shutdown_info = self._shutdown_manager.schedule(run_id)
                        except Exception as exc:
                            shutdown_info = {
                                "state": "failed",
                                "error": str(exc).strip() or "系统拒绝了关机请求",
                            }

                    with self._lock:
                        if self._run_id != run_id:
                            return
                        self._active_task = None
                        self._cancel_event = None
                        self._state["finishedAt"] = _now_secs()
                        self._state["needsAttention"] = ""
                        if self._cancel_requested or exit_code == -1:
                            self._state["status"] = "cancelled"
                            self._state["stage"] = "已停止"
                            self._state["exitCode"] = None
                        elif exit_code == 0:
                            if self._state["status"] != "time_limit":
                                self._state["status"] = "completed"
                            self._state["stage"] = "已结束"
                            self._state["exitCode"] = 0
                        else:
                            self._state["status"] = "error"
                            self._state["stage"] = "异常结束"
                            self._state["exitCode"] = exit_code
                            if error_msg:
                                self._state["errorMessage"] = error_msg
                            elif not self._state["errorMessage"]:
                                fallback_reason = ""
                                for entry in reversed(self._log_ring):
                                    if entry.get("level") in ("error", "warning"):
                                        msg_text = str(entry.get("message") or "").strip()
                                        if msg_text:
                                            fallback_reason = msg_text
                                            break
                                self._state["errorMessage"] = fallback_reason or (
                                    f"任务异常结束（退出码 {exit_code}），请查看日志"
                                )

                        if shutdown_info is not None:
                            if (
                                shutdown_info.get("state") == "scheduled"
                                and (
                                    self._state["status"] == "cancelled"
                                    or self._shutdown_cancelled_for_run
                                    or self._closed
                                )
                            ):
                                try:
                                    self._shutdown_manager.cancel(run_id)
                                except Exception:
                                    pass
                                self._state["shutdownOnComplete"] = False
                                self._state["shutdown"] = {"state": "cancelled"}
                            else:
                                self._state["shutdown"] = shutdown_info
                                if shutdown_info.get("state") == "scheduled":
                                    self._push_log_locked(
                                        "warning",
                                        f"学习任务已结束，系统将在 {shutdown_info['delaySeconds']} 秒后自动关机，可在运行监控页取消关机",
                                    )
                                elif shutdown_info.get("state") == "failed":
                                    self._push_log_locked(
                                        "error",
                                        f"自动关机启动失败：{shutdown_info.get('error') or '系统拒绝了关机请求'}",
                                    )
                        self._broadcast_locked(urgent=True)
