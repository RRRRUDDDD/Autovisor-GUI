# encoding=utf-8
"""Autovisor 内置无感本地后端服务（供 Tauri 桌面端托管通信）。

设计特点：
- 仅使用 Python 标准库（http.server + asyncio），零额外依赖；
- 绑定 127.0.0.1:0 自动分配空闲端口，通过 stdout 输出单行 JSON 握手；
- 所有 /api/* 接口校验 X-Autovisor-Token 令牌；
- 通过 Logger / progress 结构化事件钩子维护 RunState，并通过 SSE 实时推送；
- 监听 stdin EOF，宿主进程退出时自动取消任务、关闭浏览器并优雅退出。
"""
import argparse
import asyncio
from collections import deque
import hmac
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import PurePath
import queue
import secrets
import sqlite3
import sys
import threading
import time
import traceback
from urllib.parse import parse_qs, urlparse

from modules.shutdown import SHUTDOWN_OUTCOMES, ShutdownManager


LOG_LIMIT = 800
SSE_COALESCE_SECS = 0.15


def _now_secs() -> float:
    return time.time()


def _format_hms_local() -> str:
    return time.strftime("%H:%M:%S", time.localtime())


def _parse_ratio(value) -> tuple[int, int] | None:
    text = str(value or "").strip()
    if "/" not in text:
        return None
    left, right = text.split("/", 1)
    try:
        idx = int(left.strip())
        total = int(right.strip())
    except ValueError:
        return None
    if total <= 0:
        return None
    return idx, total


def _parse_cli_args(argv=None):
    parser = argparse.ArgumentParser(description="Autovisor Embedded Backend Service")
    parser.add_argument("--data-dir", default="", help="可写用户数据目录（config.ini / data / logs）")
    parser.add_argument("--resources-dir", default="", help="只读内置资源目录（stealth.min.js）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，固定仅允许回环地址")
    parser.add_argument("--port", type=int, default=0, help="监听端口，0 表示系统自动分配")
    parser.add_argument("--auth-stdin", action="store_true", help="从 stdin 首行 JSON 握手读取认证令牌")
    parser.add_argument("--allow-insecure-dev", action="store_true", help="仅限本地手动调试：允许无令牌免鉴权访问")
    parser.add_argument("--verify-deps", action="store_true", help="校验内置运行时依赖是否可直接导入并退出")
    args = parser.parse_args(argv)
    if not args.verify_deps and not args.data_dir:
        parser.error("the following arguments are required: --data-dir")
    return args


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

    modules = _load_captcha_modules()
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
        return {
            "runId": 0,
            "revision": 0,
            "status": "idle",  # idle | running | stopping | completed | time_limit | error | cancelled
            "mode": "study",
            "reviewMode": False,
            "shutdownOnComplete": False,
            "shutdown": None,
            "startedAt": None,
            "finishedAt": None,
            "exitCode": None,
            "stage": "就绪",
            "courseIndex": 0,
            "courseTotal": 0,
            "currentCourse": "",
            "catalogType": "",
            "currentLesson": "",
            "lessonProgress": 0.0,
            "progressDesc": "",
            "remainingMinutes": None,
            "courseElapsedMin": 0.0,
            "completedLessons": [],
            "needsAttention": "",
            "errorMessage": "",
            "logFilePath": "",
            "logs": [],
            "logsTruncated": False,
        }

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
                fields = payload.get("fields") or {}
                course_idx = fields.get("课程索引")
                course_total = fields.get("课程总数")
                if isinstance(course_idx, int) and isinstance(course_total, int):
                    self._state["courseIndex"] = course_idx
                    self._state["courseTotal"] = course_total
                    self._state["lessonProgress"] = 0.0
                    self._state["remainingMinutes"] = None
                    self._state["courseElapsedMin"] = 0.0
                    changed = True
                elif fields.get("课程序号") and "/" in str(fields["课程序号"]):
                    parts = str(fields["课程序号"]).split("/", 1)
                    try:
                        self._state["courseIndex"] = int(parts[0].strip())
                        self._state["courseTotal"] = int(parts[1].strip())
                        self._state["lessonProgress"] = 0.0
                        self._state["remainingMinutes"] = None
                        self._state["courseElapsedMin"] = 0.0
                        changed = True
                    except ValueError:
                        pass
                if fields.get("目录类型"):
                    self._state["catalogType"] = str(fields["目录类型"])
                    changed = True
                if fields.get("课程名称"):
                    self._state["currentCourse"] = str(fields["课程名称"])
                    changed = True
                if fields.get("课时名称"):
                    self._state["currentLesson"] = str(fields["课时名称"])
                    self._state["lessonProgress"] = 0.0
                    self._state["needsAttention"] = ""
                    changed = True
                if fields.get("子项目名称"):
                    self._state["currentLesson"] = str(fields["子项目名称"])
                    self._state["lessonProgress"] = 0.0
                    self._state["needsAttention"] = ""
                    if fields.get("子项目序号"):
                        mode_prefix = (
                            "AI 智慧课复习子项目"
                            if fields.get("模式") == "复习"
                            else "AI 智慧课子项目"
                        )
                        self._state["progressDesc"] = f"{mode_prefix} {fields['子项目序号']}"
                    changed = True
            elif kind == "event":
                name = str(payload.get("name") or "")
                fields = payload.get("fields") or {}
                if name == "当前课程" and fields.get("名称"):
                    self._state["currentCourse"] = str(fields["名称"])
                    changed = True
                elif name == "课程目录" and fields.get("类型"):
                    self._state["catalogType"] = str(fields["类型"])
                    changed = True
                elif name == "课时开始" and fields.get("标题"):
                    self._state["currentLesson"] = str(fields["标题"])
                    self._state["lessonProgress"] = 0.0
                    self._state["needsAttention"] = ""
                    changed = True
                elif name == "课时已完成" and fields.get("标题"):
                    title = str(fields["标题"]).strip()
                    if title:
                        self._state["completedLessons"].append(
                            {"title": title, "time": _format_hms_local()}
                        )
                        self._state["lessonProgress"] = 100.0
                        self._state["needsAttention"] = ""
                    elapsed = fields.get("已学分钟")
                    if isinstance(elapsed, (int, float)):
                        self._state["courseElapsedMin"] = float(elapsed)
                    changed = True
                elif name == "资源开始" and fields.get("标题"):
                    title = str(fields["标题"]).strip()
                    kind_label = str(fields.get("类型") or "").strip()
                    stage = str(fields.get("阶段") or "").strip()
                    self._state["currentLesson"] = (
                        f"{title} ({kind_label})" if kind_label else title
                    )
                    self._state["lessonProgress"] = 0.0
                    self._state["needsAttention"] = ""
                    if stage == "复习":
                        self._state["progressDesc"] = "AI 智慧课视频复习"
                    elif stage == "补齐":
                        self._state["progressDesc"] = "AI 智慧课资源补齐"
                    else:
                        self._state["progressDesc"] = "AI 智慧课必学资源"
                    changed = True
                elif name == "ai-smart 资源完成" and fields.get("标题"):
                    title = str(fields["标题"]).strip()
                    if title:
                        self._state["completedLessons"].append(
                            {"title": title, "time": _format_hms_local()}
                        )
                        self._state["lessonProgress"] = 100.0
                        changed = True
                elif name == "ai-smart 资源复习完成" and fields.get("标题"):
                    title = str(fields["标题"]).strip()
                    if title:
                        self._state["completedLessons"].append(
                            {"title": f"{title}（复习）", "time": _format_hms_local()}
                        )
                        self._state["lessonProgress"] = 100.0
                        changed = True
                elif name == "ai-smart 课程完成":
                    self._state["lessonProgress"] = 100.0
                    self._state["needsAttention"] = ""
                    changed = True
                elif name == "知识点状态" and fields.get("名称"):
                    kp_name = str(fields["名称"])
                    self._state["currentLesson"] = kp_name
                    self._state["needsAttention"] = ""
                    mastery = fields.get("掌握度")
                    if isinstance(mastery, (int, float)):
                        pct = max(0.0, min(100.0, float(mastery)))
                        self._state["lessonProgress"] = pct
                        self._state["progressDesc"] = f"知识点掌握度 {pct:g}%"
                    else:
                        self._state["progressDesc"] = "知识点掌握度检测"
                    changed = True
                elif name == "掌握度结果":
                    mastery = fields.get("掌握度")
                    attempt = fields.get("轮次")
                    if isinstance(mastery, (int, float)):
                        pct = max(0.0, min(100.0, float(mastery)))
                        self._state["lessonProgress"] = pct
                        suffix = f"（第 {attempt} 轮）" if attempt is not None else ""
                        self._state["progressDesc"] = f"知识点掌握度 {pct:g}%{suffix}"
                        changed = True
                elif name == "知识点完成" and fields.get("名称"):
                    kp_name = str(fields["名称"])
                    after = fields.get("后")
                    if isinstance(after, (int, float)):
                        pct = max(0.0, min(100.0, float(after)))
                        self._state["lessonProgress"] = pct
                        self._state["completedLessons"].append(
                            {"title": f"{kp_name}（掌握度 {pct:g}%）", "time": _format_hms_local()}
                        )
                        changed = True
                elif name == "接口考试范围":
                    kp_count = fields.get("知识点")
                    self._state["progressDesc"] = (
                        f"接口考试（共 {kp_count} 个知识点）"
                        if kp_count is not None
                        else "接口考试"
                    )
                    self._state["lessonProgress"] = 0.0
                    self._state["needsAttention"] = ""
                    changed = True
                elif name == "知识点考试":
                    kp_name = str(fields.get("知识点") or "").strip()
                    if kp_name:
                        self._state["currentLesson"] = kp_name
                    seq_text = str(fields.get("序号") or "").strip()
                    if seq_text:
                        self._state["progressDesc"] = f"接口考试 {seq_text}"
                        ratio = _parse_ratio(seq_text)
                        if ratio is not None:
                            idx, total = ratio
                            self._state["lessonProgress"] = max(
                                0.0, min(100.0, (idx - 1) / total * 100.0)
                            )
                    self._state["needsAttention"] = ""
                    changed = True
                elif name == "接口考试开始":
                    q_total = fields.get("题目")
                    self._state["progressDesc"] = (
                        f"接口考试作答（共 {q_total} 题）"
                        if q_total is not None
                        else "接口考试作答"
                    )
                    self._state["lessonProgress"] = 0.0
                    self._state["needsAttention"] = ""
                    changed = True
                elif name == "接口答题":
                    seq_text = str(fields.get("序号") or "").strip()
                    ratio = _parse_ratio(seq_text)
                    if ratio is not None:
                        idx, total = ratio
                        self._state["lessonProgress"] = max(
                            0.0, min(100.0, idx / total * 100.0)
                        )
                        self._state["progressDesc"] = f"接口考试作答 {seq_text}"
                        changed = True
                    elif seq_text:
                        self._state["progressDesc"] = f"接口考试作答 {seq_text}"
                        changed = True
                elif name == "知识点考试结果":
                    kp_name = str(
                        fields.get("知识点") or self._state.get("currentLesson") or ""
                    ).strip()
                    score = str(fields.get("得分") or "").strip()
                    score_ratio = _parse_ratio(score)
                    all_correct = bool(fields.get("全对")) or (
                        score_ratio is not None and score_ratio[0] >= score_ratio[1]
                    )
                    if all_correct:
                        if kp_name:
                            label = (
                                f"{kp_name}（接口考试 {score}）"
                                if score
                                else f"{kp_name}（接口考试）"
                            )
                            self._state["completedLessons"].append(
                                {"title": label, "time": _format_hms_local()}
                            )
                        self._state["lessonProgress"] = 100.0
                        self._state["progressDesc"] = (
                            f"接口考试完成 {score}" if score else "接口考试完成"
                        )
                        changed = True
                    elif score:
                        if score_ratio is not None:
                            self._state["lessonProgress"] = max(
                                0.0, min(100.0, score_ratio[0] / score_ratio[1] * 100.0)
                            )
                        self._state["progressDesc"] = f"接口考试得分 {score}"
                        changed = True
                elif name == "资源补齐":
                    done_count = fields.get("补齐")
                    if isinstance(done_count, (int, float)) and done_count > 0:
                        res_name = str(fields.get("名称") or "").strip()
                        if res_name:
                            self._state["currentLesson"] = res_name
                        seq_text = str(fields.get("序号") or "").strip()
                        self._state["progressDesc"] = (
                            f"必学资源补齐 {seq_text}" if seq_text else "必学资源补齐"
                        )
                        ratio = _parse_ratio(seq_text)
                        if ratio is not None:
                            self._state["lessonProgress"] = max(
                                0.0, min(100.0, ratio[0] / ratio[1] * 100.0)
                            )
                        changed = True
                elif name in ("知到作业", "知到考试"):
                    item_name = str(fields.get("名称") or "").strip()
                    status_val = str(fields.get("状态") or "").strip()
                    seq_text = str(fields.get("序号") or "").strip()
                    if item_name:
                        self._state["currentLesson"] = item_name
                    if status_val == "开始":
                        self._state["needsAttention"] = ""
                        self._state["progressDesc"] = (
                            f"{name} {seq_text}" if seq_text else name
                        )
                        ratio = _parse_ratio(seq_text)
                        if ratio is not None:
                            idx, total = ratio
                            self._state["lessonProgress"] = max(
                                0.0, min(100.0, (idx - 1) / total * 100.0)
                            )
                        else:
                            self._state["lessonProgress"] = 0.0
                        changed = True
                    elif status_val == "完成":
                        if item_name:
                            score = fields.get("得分")
                            submitted = fields.get("已交卷")
                            if score is not None:
                                suffix = f"得分 {score}"
                            elif submitted:
                                suffix = "已提交"
                            else:
                                suffix = "已保存"
                            self._state["completedLessons"].append(
                                {
                                    "title": f"{item_name}（{name}·{suffix}）",
                                    "time": _format_hms_local(),
                                }
                            )
                        ratio = _parse_ratio(seq_text)
                        if ratio is not None:
                            idx, total = ratio
                            self._state["lessonProgress"] = max(
                                0.0, min(100.0, idx / total * 100.0)
                            )
                        else:
                            self._state["lessonProgress"] = 100.0
                        self._state["progressDesc"] = (
                            f"{name}完成 {seq_text}" if seq_text else f"{name}完成"
                        )
                        changed = True
                elif name == "达到课程时限":
                    if self._state["status"] == "running":
                        self._state["status"] = "time_limit"
                        changed = True
                elif name == "人工介入":
                    status_val = str(fields.get("状态") or "").strip()
                    if status_val == "需要":
                        self._state["needsAttention"] = str(
                            fields.get("原因") or "需要人工处理"
                        )
                        changed = True
                    elif status_val == "已解除":
                        self._state["needsAttention"] = ""
                        changed = True
                elif name == "安全验证":
                    status_val = str(fields.get("状态") or "").strip()
                    if status_val == "开始":
                        self._state["needsAttention"] = "检测到安全验证,请手动完成验证..."
                        changed = True
                    elif status_val in ("自动完成", "完成"):
                        self._state["needsAttention"] = ""
                        changed = True
                elif name in ("恢复播放", "登录完成"):
                    if self._state["needsAttention"]:
                        self._state["needsAttention"] = ""
                        changed = True
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
        fut = asyncio.run_coroutine_threadsafe(
            asyncio.wait_for(
                _solve_captcha_via_cdp(cdp_url, page_url, appear_ms, attempts),
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
        import platform
        import Autovisor
        from modules import installer, llm, progress
        from modules.configs import Config, ConfigError, ensure_config_files
        from modules.diagnostics import check_browser, check_course
        from modules.logger import Logger
        from modules.notifier import Notifier
        from modules.utils import get_runtime_path, import_zhihuishu_cookies
        from modules.version import __version__

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
            logger.event(
                "运行环境",
                版本=__version__,
                Python=platform.python_version(),
                运行方式="桌面端内置后端(打包)" if getattr(sys, "frozen", False) else "桌面端内置后端(源码)",
                系统=platform.system(),
                系统版本=platform.release(),
                系统架构=platform.machine(),
                任务模式=mode,
                数据目录=self.data_dir,
                日志文件=log_file,
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
            logger.event(
                "运行配置",
                驱动=config.driver,
                浏览器路径=config.exe_path or "默认",
                代理="已配置" if config.proxy else "直连",
                连接现有浏览器=config.attach_existing_chrome,
                账号="已填写" if config.username else "未填写",
                密码="已填写" if config.password else "未填写",
                二维码登录=config.qr_login,
                自动验证码=config.enableAutoCaptcha,
                隐藏窗口=config.enableHideWindow,
                完成后关机=config.shutdownOnComplete,
                大模型=llm.provider_label() if llm.enabled() else "未配置",
                视觉模型=llm.vision_label() if llm.vision_enabled() else "未配置",
                智慧树AI答题=config.enableZhidaoQuiz,
                接口考试=config.enableExamApi,
                知到作业接口=config.enableHomeworkApi,
                知到考试接口=config.enableFormalExamApi,
                已答重做=getattr(config, "forceRedoAnswered", False),
                掌握度刷题=config.enableMastery,
                复习考试接口=config.enableReviewExamApi,
                目标掌握度=config.masteryTarget,
                消息推送=configured_channels,
                静音=config.soundOff,
                复习模式=config.reviewMode,
                倍速=config.limitSpeed,
                时限分钟=config.limitMaxTime,
                课程数=len(config.course_urls),
                镜像源=len(config.mirrors),
            )
            for index, course_url in enumerate(config.course_urls, 1):
                logger.event("课程地址", 序号=f"{index}/{len(config.course_urls)}", 地址=course_url)

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
                                # 若运行以非 0 退出但未记录 error 级别日志（如课时进度未确认以 warn 退出），
                                # 提取最近一条告警/错误日志作为可见原因，避免仅提示“退出码 1”。
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


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_ALLOWED_ORIGIN_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "tauri.localhost"})


def _is_valid_host_header(host: str | None) -> bool:
    raw = str(host or "").strip()
    if not raw or any(ch.isspace() or ord(ch) < 32 for ch in raw):
        return False
    try:
        parsed = urlparse(f"http://{raw}")
    except ValueError:
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    if parsed.path or parsed.params or parsed.query or parsed.fragment:
        return False
    try:
        _ = parsed.port
    except ValueError:
        return False
    return (parsed.hostname or "").lower() in _LOOPBACK_HOSTS


def _is_valid_origin_header(origin: str | None) -> bool:
    raw = str(origin or "").strip()
    if not raw:
        return True
    if any(ch.isspace() or ord(ch) < 32 for ch in raw):
        return False
    try:
        parsed = urlparse(raw)
    except ValueError:
        return False
    scheme = (parsed.scheme or "").lower()
    hostname = (parsed.hostname or "").lower()
    if scheme == "tauri":
        return hostname == "localhost"
    if scheme in ("http", "https"):
        return hostname in _ALLOWED_ORIGIN_HOSTS
    return False


def _is_loopback_cdp_url(cdp_url: str) -> bool:
    raw = str(cdp_url or "").strip()
    if not raw or any(ch.isspace() or ord(ch) < 32 for ch in raw):
        return False
    try:
        parsed = urlparse(raw)
    except ValueError:
        return False
    if parsed.scheme not in ("ws", "wss", "http", "https"):
        return False
    return (parsed.hostname or "").lower() in _LOOPBACK_HOSTS


def _validate_user_path(raw: str, *, expect_dir: bool) -> str:
    text = str(raw or "").strip()
    if not text:
        raise ValueError("路径不能为空")
    if any(ord(ch) < 32 for ch in text):
        raise ValueError("路径包含非法控制字符")
    if text.startswith("\\\\") or text.startswith("//"):
        raise ValueError("禁止使用网络 UNC 路径")
    if ".." in PurePath(text).parts:
        raise ValueError("路径禁止包含目录遍历片段 (..)")
    resolved = os.path.realpath(os.path.abspath(text))
    if expect_dir:
        if not os.path.isdir(resolved):
            raise ValueError(f"目录不存在或无效: {text}")
    else:
        if not os.path.isfile(resolved):
            raise ValueError(f"文件不存在或不是普通文件: {text}")
    return resolved


class BackendHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        addr,
        handler_cls,
        manager: TaskManager,
        token: str,
        app_version: str,
        *,
        allow_insecure_dev: bool = False,
    ):
        self.manager = manager
        self.token = token
        self.allow_insecure_dev = bool(allow_insecure_dev)
        self.app_version = app_version
        self.shutdown_event = threading.Event()
        self._shutdown_once_lock = threading.Lock()
        self._shutdown_done = False
        self._shutdown_complete = threading.Event()
        self._qr_manager_lock = threading.Lock()
        self._qr_manager = None
        self._question_store_lock = threading.Lock()
        self._question_store = None
        self._question_store_db_path = ""
        super().__init__(addr, handler_cls)

    def get_question_store(self):
        data_root = os.path.join(self.manager.data_dir, "data")
        db_path = os.path.join(data_root, "questions_bank.db")
        with self._question_store_lock:
            if (
                self._question_store is None
                or self._question_store_db_path != db_path
            ):
                from modules.answer_store import AiQuestionStore

                self._question_store = AiQuestionStore(
                    db_path=db_path,
                    data_root=data_root,
                )
                self._question_store_db_path = db_path
            return self._question_store

    def get_qr_manager(self):
        with self._qr_manager_lock:
            if self._qr_manager is None:
                from modules.qr_login import QrLoginManager

                self._qr_manager = QrLoginManager()
            return self._qr_manager

    def close_qr_manager(self):
        with self._qr_manager_lock:
            mgr = self._qr_manager
            self._qr_manager = None
        if mgr is not None:
            try:
                mgr.close_all()
            except Exception:
                pass

    def graceful_shutdown(self, *, stop_http: bool = True):
        """幂等统一关停：标记退出、清理二维码会话、关闭任务管理器与事件循环，并按需停止 HTTP 监听。"""
        with self._shutdown_once_lock:
            if self._shutdown_done:
                already_started = True
            else:
                self._shutdown_done = True
                already_started = False
        if already_started:
            # 等待首个关停流程完成清理，避免并发调用者（如 main() finally）提前退出进程打断清理
            self._shutdown_complete.wait(timeout=8.0)
            return
        try:
            self.shutdown_event.set()
            self.close_qr_manager()
            try:
                self.manager.shutdown()
            except Exception:
                pass
            if stop_http:
                try:
                    self.shutdown()
                except Exception:
                    pass
        finally:
            self._shutdown_complete.set()


class BackendRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):  # noqa: A002
        # 无感后端不向控制台输出 HTTP 访问日志
        return

    def _drain_request_body(self, max_bytes: int = 64 * 1024) -> None:
        """提前拒绝 POST 请求前读取小体积剩余请求体，避免 Windows loopback 关闭时触发 TCP RST (WinError 10053)。"""
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except (TypeError, ValueError):
            return
        if 0 < length <= max_bytes:
            try:
                self.rfile.read(length)
            except Exception:
                pass

    def _validate_request_origin(self) -> bool:
        if not _is_valid_host_header(self.headers.get("Host")):
            self._drain_request_body()
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden host"})
            return False
        if not _is_valid_origin_header(self.headers.get("Origin")):
            self._drain_request_body()
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden origin"})
            return False
        return True

    def _authorized(self) -> bool:
        expected = getattr(self.server, "token", "") or ""
        if not expected:
            return bool(getattr(self.server, "allow_insecure_dev", False))
        provided = self.headers.get("X-Autovisor-Token", "")
        if not provided:
            return False
        return hmac.compare_digest(provided, expected)

    def _send_json(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self, max_bytes: int = 2 * 1024 * 1024) -> dict:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return {}
        if length > max_bytes:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length)
        if not raw:
            return {}
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data

    def _question_store(self):
        return self.server.get_question_store()

    def do_GET(self):
        if not self._validate_request_origin():
            return
        if not self._authorized():
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return

        path = urlparse(self.path).path
        if path == "/api/health":
            snap = self.server.manager.snapshot()
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "version": self.server.app_version,
                    "pid": os.getpid(),
                    "taskStatus": snap["status"],
                },
            )
            return

        if path == "/api/state":
            self._send_json(HTTPStatus.OK, self.server.manager.snapshot())
            return

        if path == "/api/cache/stats":
            query = parse_qs(urlparse(self.path).query)
            course_id = (query.get("courseId") or query.get("course_id") or [""])[0]
            try:
                stats = self._question_store().stats(course_id.strip() or None)
                self._send_json(HTTPStatus.OK, {"ok": True, **stats})
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except (sqlite3.Error, Exception) as exc:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return

        if path == "/api/events":
            self._handle_sse()
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": f"not found: {path}"})

    def do_POST(self):
        if not self._validate_request_origin():
            return
        if not self._authorized():
            self._drain_request_body()
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return

        path = urlparse(self.path).path
        try:
            content_length = int(self.headers.get("Content-Length") or "0")
        except (TypeError, ValueError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "无效的 Content-Length"})
            return
        content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            if not (path == "/api/shutdown" and content_length <= 0):
                self._drain_request_body()
                self._send_json(
                    HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                    {"error": "Content-Type 必须为 application/json"},
                )
                return

        try:
            # 题库缓存导入包含全部历史题目, 单独放宽上限; 其他接口保持 2MB 保护
            body_limit = 20 * 1024 * 1024 if path == "/api/cache/import" else 2 * 1024 * 1024
            body = self._read_json_body(max_bytes=body_limit)
        except Exception as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": f"无效请求体: {exc}"})
            return

        if path == "/api/tasks/start":
            mode = str(body.get("mode") or "study").strip()
            target = str(body.get("target") or "").strip()
            if mode == "import_cookies":
                try:
                    target = _validate_user_path(target, expect_dir=False)
                except ValueError as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
            try:
                state = self.server.manager.start_task(mode, target)
                self._send_json(HTTPStatus.OK, state)
            except (ValueError, RuntimeError) as exc:
                self._send_json(HTTPStatus.CONFLICT, {"error": str(exc)})
            return

        if path == "/api/tasks/stop":
            state = self.server.manager.stop_task()
            self._send_json(HTTPStatus.OK, state)
            return

        if path == "/api/tasks/cancel-shutdown":
            try:
                state = self.server.manager.cancel_shutdown()
                self._send_json(HTTPStatus.OK, state)
            except Exception as exc:
                self._send_json(
                    HTTPStatus.BAD_GATEWAY,
                    {"error": str(exc).strip() or "取消关机失败，请重试"},
                )
            return

        if path == "/api/notify/test":
            from modules.notifier import test_notification_config

            provider = str(body.get("provider") or "").strip()
            url = str(body.get("url") or "").strip()
            tg_chat_id = str(body.get("tgChatId") or body.get("tg_chat_id") or "").strip()
            proxy = str(body.get("proxy") or "").strip()
            try:
                msg = test_notification_config(
                    provider=provider,
                    url=url,
                    tg_chat_id=tg_chat_id,
                    proxy=proxy,
                )
                self._send_json(HTTPStatus.OK, {"ok": True, "message": msg})
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
            return

        if path == "/api/cache/export":
            course_id = str(body.get("courseId") or body.get("course_id") or "").strip()
            try:
                payload = self._question_store().export_data(course_id or None)
                self._send_json(HTTPStatus.OK, {"ok": True, "data": payload})
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except (sqlite3.Error, Exception) as exc:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return

        if path == "/api/cache/import":
            if self.server.manager.snapshot().get("status") in ("running", "stopping"):
                self._send_json(
                    HTTPStatus.CONFLICT,
                    {"error": "任务运行中，请先停止当前学习任务后再导入题库缓存"},
                )
                return
            payload = body.get("data") if isinstance(body.get("data"), dict) else body
            try:
                counts = self._question_store().import_data(payload)
                self._send_json(HTTPStatus.OK, {"ok": True, **counts})
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except (sqlite3.Error, Exception) as exc:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return

        if path == "/api/cache/clear":
            if self.server.manager.snapshot().get("status") in ("running", "stopping"):
                self._send_json(
                    HTTPStatus.CONFLICT,
                    {"error": "任务运行中，请先停止当前学习任务后再清理题库缓存"},
                )
                return
            course_id = str(body.get("courseId") or body.get("course_id") or "").strip()
            try:
                counts = self._question_store().clear(course_id or None)
                self._send_json(HTTPStatus.OK, {"ok": True, **counts})
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except (sqlite3.Error, Exception) as exc:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return

        if path == "/api/question-bank/test":
            from modules.question_bank import DEFAULT_QUERY_URL, QuestionBankClient
            from modules.llm import question_bank_conf

            try:
                conf = question_bank_conf()
                token = str(body.get("token") or conf.get("token") or "").strip()
                query_url = str(body.get("url") or conf.get("url") or DEFAULT_QUERY_URL).strip()
                proxy = str(body.get("proxy") or "").strip()
                client = QuestionBankClient(token=token, query_url=query_url, proxy=proxy)
                try:
                    result = client.probe()
                finally:
                    close_fn = getattr(client, "close", None)
                    if callable(close_fn):
                        close_fn()
                self._send_json(HTTPStatus.OK, result)
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
            return

        if path == "/api/llm/test":
            from modules.llm import test_connection

            llm_cfg = body.get("llm") if isinstance(body.get("llm"), dict) else body
            proxy = str(body.get("proxy") or "").strip()
            try:
                msg = test_connection(llm_cfg, proxy=proxy)
                self._send_json(HTTPStatus.OK, {"ok": True, "message": msg})
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
            return

        if path == "/api/llm/models":
            from modules.llm import fetch_models

            llm_cfg = body.get("llm") if isinstance(body.get("llm"), dict) else body
            proxy = str(body.get("proxy") or "").strip()
            try:
                models = fetch_models(llm_cfg, proxy=proxy)
                self._send_json(HTTPStatus.OK, {"ok": True, "models": models})
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
            return

        if path == "/api/tasks/debug":
            enabled = bool(body.get("enabled"))
            effective = self.server.manager.set_debug_log(enabled)
            self._send_json(HTTPStatus.OK, {"ok": True, "enabled": effective})
            return

        if path == "/api/qr-login/start":
            proxy = str(body.get("proxy") or "").strip()
            save_dir = str(body.get("saveDir") or "").strip()
            if save_dir:
                try:
                    save_dir = _validate_user_path(save_dir, expect_dir=True)
                except ValueError as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
            try:
                result = self.server.get_qr_manager().start(proxy=proxy, save_dir=save_dir)
                self._send_json(HTTPStatus.OK, {"ok": True, **result})
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
            return

        if path == "/api/qr-login/poll":
            session_id = str(body.get("sessionId") or "").strip()
            cookie_path = os.path.join(self.server.manager.data_dir, "data", "cookies.json")
            try:
                result = self.server.get_qr_manager().poll(
                    session_id, cookie_path=cookie_path
                )
                self._send_json(HTTPStatus.OK, {"ok": True, **result})
            except (KeyError, ValueError) as exc:
                msg = exc.args[0] if exc.args else str(exc)
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(msg)})
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
            return

        if path == "/api/qr-login/save":
            session_id = str(body.get("sessionId") or "").strip()
            save_dir = str(body.get("saveDir") or "").strip()
            try:
                save_dir = _validate_user_path(save_dir, expect_dir=True)
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            try:
                saved_path = self.server.get_qr_manager().save_current_image(
                    session_id, save_dir
                )
                self._send_json(HTTPStatus.OK, {"ok": True, "savedPath": saved_path})
            except (KeyError, ValueError) as exc:
                msg = exc.args[0] if exc.args else str(exc)
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(msg)})
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
            return

        if path == "/api/qr-login/cancel":
            session_id = str(body.get("sessionId") or "").strip()
            cancelled = self.server.get_qr_manager().cancel(session_id)
            self._send_json(HTTPStatus.OK, {"ok": True, "cancelled": cancelled})
            return

        if path == "/api/courses/fetch":
            from modules.course_fetcher import fetch_courses_via_api

            proxy = str(body.get("proxy") or "").strip()
            raw_cookies = body.get("cookies")
            if raw_cookies is None:
                cookie_path = os.path.join(
                    self.server.manager.data_dir, "data", "cookies.json"
                )
                try:
                    with open(cookie_path, "r", encoding="utf-8") as file:
                        raw_cookies = json.load(file)
                except OSError:
                    raw_cookies = []
                except ValueError as exc:
                    self._send_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": f"本地 Cookie 文件格式无效: {exc}"},
                    )
                    return
            if not isinstance(raw_cookies, list):
                self._send_json(
                    HTTPStatus.BAD_REQUEST, {"error": "cookies 参数格式无效"}
                )
                return
            try:
                fetched = fetch_courses_via_api(raw_cookies, proxy=proxy)
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "sessionValid": fetched.session_valid,
                        "courses": [
                            {
                                "title": c.title,
                                "url": c.url,
                                "courseType": c.course_type,
                                "teacher": c.teacher,
                                "school": c.school,
                                "progress": c.progress,
                            }
                            for c in fetched.courses
                        ],
                        "displayName": fetched.display_name,
                        "schoolName": fetched.school_name,
                        "sources": fetched.sources,
                        "cookies": fetched.cookies,
                    },
                )
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
            return

        if path == "/api/update/check":
            from modules import updater

            proxy = str(body.get("proxy") or "").strip()
            current_version = str(body.get("currentVersion") or "").strip()
            try:
                kwargs = {"proxy": proxy}
                if current_version:
                    kwargs["current_version"] = current_version
                info = updater.check_update_info(**kwargs)
                self._send_json(HTTPStatus.OK, {"ok": True, **info})
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
            return

        if path == "/api/captcha/solve":
            cdp_url = str(body.get("cdpUrl") or "").strip()
            page_url = str(body.get("pageUrl") or "").strip()
            if not _is_loopback_cdp_url(cdp_url):
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "cdpUrl 无效（仅允许本机回环地址）"},
                )
                return
            try:
                appear_ms = int(body.get("appearMs") or 6000)
            except (TypeError, ValueError):
                appear_ms = 6000
            try:
                attempts = int(body.get("maxAttempts") or 2)
            except (TypeError, ValueError):
                attempts = 2
            appear_ms = max(1000, min(appear_ms, 20000))
            attempts = max(1, min(attempts, 4))
            try:
                result = self.server.manager.solve_captcha(
                    cdp_url, page_url, appear_ms, attempts
                )
            except Exception as exc:
                self._send_json(
                    HTTPStatus.BAD_GATEWAY,
                    {"error": f"自动验证失败: {type(exc).__name__}: {exc}"},
                )
                return
            self._send_json(HTTPStatus.OK, {"ok": True, **result})
            return

        if path == "/api/shutdown":
            self._send_json(HTTPStatus.OK, {"ok": True})
            threading.Thread(
                target=self._trigger_shutdown, daemon=True
            ).start()
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": f"not found: {path}"})

    def _trigger_shutdown(self):
        self.server.graceful_shutdown(stop_http=True)

    def _handle_sse(self):
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        q = self.server.manager.subscribe()
        try:
            while not self.server.shutdown_event.is_set():
                try:
                    item = q.get(timeout=15.0)
                    if isinstance(item, tuple) and len(item) == 2:
                        event_name, data = item
                    else:
                        event_name, data = "state", item
                    payload = json.dumps(data, ensure_ascii=False)
                    chunk = f"event: {event_name}\ndata: {payload}\n\n".encode("utf-8")
                except queue.Empty:
                    chunk = b": keepalive\n\n"
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            self.server.manager.unsubscribe(q)


def _watch_parent_stdin_windows(server: BackendHTTPServer) -> bool:
    """Windows 下通过 Win32 PeekNamedPipe/ReadFile 监听父进程 stdin 管道 EOF。

    不能使用 sys.stdin.buffer.read(1) 或 os.read(0, 1)：阻塞态 UCRT _read(0)
    会长期持有 C 运行时 stdin 文件锁，导致工作线程首次加载 numpy/OpenBLAS DLL
    时与 Windows Loader Lock / GIL 发生永久死锁。
    """
    import ctypes
    from ctypes import wintypes

    STD_INPUT_HANDLE = -10
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    ERROR_BROKEN_PIPE = 109
    ERROR_INVALID_HANDLE = 6

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel32.GetStdHandle.restype = wintypes.HANDLE
    kernel32.PeekNamedPipe.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.PeekNamedPipe.restype = wintypes.BOOL
    kernel32.ReadFile.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    kernel32.ReadFile.restype = wintypes.BOOL

    handle = kernel32.GetStdHandle(wintypes.DWORD(STD_INPUT_HANDLE))
    if not handle or handle == INVALID_HANDLE_VALUE:
        return False

    buf = ctypes.create_string_buffer(64)
    bytes_read = wintypes.DWORD(0)
    total_avail = wintypes.DWORD(0)
    bytes_left = wintypes.DWORD(0)

    while not server.shutdown_event.is_set():
        ok = kernel32.PeekNamedPipe(
            handle,
            None,
            0,
            None,
            ctypes.byref(total_avail),
            ctypes.byref(bytes_left),
        )
        if not ok:
            err = ctypes.get_last_error()
            if err in (ERROR_BROKEN_PIPE, ERROR_INVALID_HANDLE):
                return True
            # 非管道句柄（如 NUL 或普通文件重定向）不触发自动退出
            return False
        if total_avail.value > 0:
            to_read = min(total_avail.value, len(buf))
            read_ok = kernel32.ReadFile(
                handle, buf, to_read, ctypes.byref(bytes_read), None
            )
            if not read_ok or bytes_read.value == 0:
                return True
        else:
            server.shutdown_event.wait(0.25)
    return False


def _watch_parent_stdin(server: BackendHTTPServer):
    """宿主关闭 stdin 管道（正常退出或崩溃）时自动优雅退出，防止僵尸进程。"""
    should_shutdown = True
    try:
        if os.name == "nt":
            should_shutdown = _watch_parent_stdin_windows(server)
        else:
            while not server.shutdown_event.is_set():
                data = sys.stdin.buffer.read(1)
                if not data:
                    break
    except Exception:
        pass
    finally:
        if should_shutdown and not server.shutdown_event.is_set():
            server.graceful_shutdown(stop_http=True)


def _preload_builtin_deps():
    """在主线程（启动子线程与管道监听前）预热内置 numpy / cv2，消除首次任务延迟与 DLL 锁竞争。"""
    for mod_name in ("numpy", "cv2"):
        try:
            __import__(mod_name)
        except Exception:
            pass


def main(argv=None) -> int:
    # 保留原始 stdout 专门用于输出干净的单行握手 JSON
    handshake_stdout = sys.stdout
    args = _parse_cli_args(argv)

    if args.verify_deps:
        sys.stdout = sys.stderr
        import Autovisor  # noqa: F401
        from modules import installer

        core_mods = ["requests", "PIL", "playwright.async_api"]
        for mod_name in core_mods:
            __import__(mod_name)

        installer.validate_python_version()
        loaded = installer.load_preinstalled_modules()
        if not loaded or len(loaded) < 2:
            sys.stderr.write("内置验证码依赖校验失败：未能直接导入 numpy / cv2\n")
            return 1
        np_mod, cv2_mod = loaded[0], loaded[1]
        arr = np_mod.zeros((8, 8, 3), dtype=np_mod.uint8)
        gray = cv2_mod.cvtColor(arr, cv2_mod.COLOR_BGR2GRAY)
        if gray.shape != (8, 8):
            sys.stderr.write(f"内置 OpenCV 基础运算校验失败：{gray.shape}\n")
            return 1
        handshake_stdout.write(
            json.dumps(
                {
                    "ok": True,
                    "numpy": getattr(np_mod, "__version__", ""),
                    "cv2": getattr(cv2_mod, "__version__", ""),
                    "core": core_mods,
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        handshake_stdout.flush()
        return 0

    data_dir = os.path.abspath(args.data_dir)
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(os.path.join(data_dir, "data"), exist_ok=True)
    os.makedirs(os.path.join(data_dir, "logs"), exist_ok=True)
    os.environ["AUTOVISOR_DATA_DIR"] = data_dir
    if args.resources_dir:
        os.environ["AUTOVISOR_RESOURCES_DIR"] = os.path.abspath(args.resources_dir)

    if args.host not in ("127.0.0.1", "localhost"):
        raise SystemExit("安全限制：内置后端仅允许绑定 127.0.0.1")

    token = ""
    if args.auth_stdin:
        if sys.stdin is None or sys.stdin.isatty():
            raise SystemExit("安全限制：--auth-stdin 需要通过管道传入首行 JSON 握手令牌")
        raw_auth = sys.stdin.buffer.readline(4096)
        if not raw_auth:
            raise SystemExit("安全限制：未从 stdin 收到认证令牌握手")
        try:
            auth_payload = json.loads(raw_auth.decode("utf-8"))
        except Exception as exc:
            raise SystemExit(f"安全限制：stdin 认证握手 JSON 无效: {exc}")
        if not isinstance(auth_payload, dict) or not str(auth_payload.get("authToken") or "").strip():
            raise SystemExit("安全限制：stdin 认证握手缺少非空 authToken")
        token = str(auth_payload["authToken"]).strip()
    else:
        token = os.environ.get("AUTOVISOR_BACKEND_TOKEN", "").strip()
        if not token and not args.allow_insecure_dev:
            token = secrets.token_urlsafe(32)

    # 将模块运行时的 print 输出重定向到 stderr，确保 stdout 只输出握手消息
    sys.stdout = sys.stderr

    # 在主线程预先加载内置 numpy / cv2，确保任务启动时 0 延迟且绝不与子线程发生 DLL 死锁
    _preload_builtin_deps()

    from modules.version import __version__

    manager = TaskManager(data_dir)
    server = BackendHTTPServer(
        (args.host, args.port),
        BackendRequestHandler,
        manager=manager,
        token=token,
        app_version=__version__,
        allow_insecure_dev=bool(args.allow_insecure_dev),
    )
    actual_port = server.server_address[1]

    # 启动宿主 stdin 监听线程（仅在非 TTY 管道托管模式下启用）
    if sys.stdin is not None and not sys.stdin.isatty():
        threading.Thread(
            target=_watch_parent_stdin, args=(server,), name="stdin-watchdog", daemon=True
        ).start()

    handshake = {
        "event": "autovisor-backend-ready",
        "version": 1,
        "port": actual_port,
        "pid": os.getpid(),
        "appVersion": __version__,
    }
    handshake_stdout.write(json.dumps(handshake, ensure_ascii=False) + "\n")
    handshake_stdout.flush()

    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.graceful_shutdown(stop_http=False)
        server.server_close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
