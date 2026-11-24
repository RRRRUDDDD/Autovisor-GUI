# encoding=utf-8
"""CLI 与桌面端内置后端共享的运行上下文、环境日志、API 客户端构造与 Cookie 合并工具。"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import platform
import sys
import threading
from typing import Any

from playwright.async_api import Page

from modules import llm
from modules.exam_api import ExamApi
from modules.redaction import redact_argv
from modules.utils import get_runtime_path
from modules.version import __version__


@dataclass
class RunContext:
    """单轮学习任务隔离的运行状态容器（替代模块级全局 Event/Cookie 状态）。"""

    cookie_path: str = field(default_factory=lambda: get_runtime_path("data", "cookies.json"))
    cancel_event: threading.Event = field(default_factory=threading.Event)
    verify_event: asyncio.Event = field(default_factory=asyncio.Event)
    answer_event: asyncio.Event = field(default_factory=asyncio.Event)
    playback_enabled: asyncio.Event = field(default_factory=asyncio.Event)
    popups_enabled: asyncio.Event = field(default_factory=asyncio.Event)
    saved_cookies_sig: frozenset | None = None

    def __post_init__(self) -> None:
        self.popups_enabled.set()


def resolve_cancel_event(config) -> threading.Event:
    ev = getattr(config, "_cancel_event", None) if config is not None else None
    return ev if isinstance(ev, threading.Event) else threading.Event()


def log_runtime_environment(
    logger,
    *,
    run_mode: str,
    log_file: str | None = None,
    include_argv: bool = False,
    extra_fields: dict[str, Any] | None = None,
) -> None:
    """记录统一格式的「运行环境」结构化事件（CLI 与内置后端共用）。"""
    fields: dict[str, Any] = {
        "版本": __version__,
        "Python": platform.python_version(),
        "运行方式": run_mode,
        "系统": platform.system(),
        "系统版本": platform.release(),
        "系统架构": platform.machine(),
    }
    if include_argv:
        fields["启动参数"] = redact_argv(sys.argv[1:])
    if extra_fields:
        fields.update(extra_fields)
    fields["日志文件"] = log_file if log_file is not None else getattr(logger, "filename", "")
    logger.event("运行环境", **fields)


def log_runtime_config(
    logger,
    config,
    *,
    configured_channels: str,
    llm_module=None,
    extra_fields: dict[str, Any] | None = None,
) -> None:
    """记录统一格式的「运行配置」与「课程地址」结构化事件（CLI 与内置后端共用）。"""
    llm_mod = llm_module or llm
    fields: dict[str, Any] = {
        "驱动": config.driver,
        "浏览器路径": config.exe_path or "默认",
        "代理": "已配置" if config.proxy else "直连",
        "连接现有浏览器": config.attach_existing_chrome,
        "账号": "已填写" if config.username else "未填写",
        "密码": "已填写" if config.password else "未填写",
        "二维码登录": config.qr_login,
        "自动验证码": config.enableAutoCaptcha,
        "隐藏窗口": config.enableHideWindow,
        "完成后关机": config.shutdownOnComplete,
        "大模型": llm_mod.provider_label() if llm_mod.enabled() else "未配置",
        "视觉模型": llm_mod.vision_label() if llm_mod.vision_enabled() else "未配置",
        "智慧树AI答题": config.enableZhidaoQuiz,
        "接口考试": config.enableExamApi,
        "知到作业接口": getattr(config, "enableHomeworkApi", True),
        "知到考试接口": getattr(config, "enableFormalExamApi", True),
        "已答重做": getattr(config, "forceRedoAnswered", False),
        "掌握度刷题": config.enableMastery,
        "复习考试接口": config.enableReviewExamApi,
        "目标掌握度": config.masteryTarget,
        "消息推送": configured_channels or "未配置",
        "静音": config.soundOff,
        "复习模式": config.reviewMode,
        "倍速": config.limitSpeed,
        "时限分钟": config.limitMaxTime,
        "课程数": len(config.course_urls),
        "镜像源": len(config.mirrors),
    }
    if extra_fields:
        fields.update(extra_fields)
    logger.event("运行配置", **fields)
    for index, course_url in enumerate(config.course_urls, 1):
        logger.event("课程地址", 序号=f"{index}/{len(config.course_urls)}", 地址=course_url)


async def build_page_exam_api(page: Page, config) -> ExamApi:
    cookies = await page.context.cookies()
    try:
        user_agent = await page.evaluate("navigator.userAgent")
    except Exception:
        user_agent = ""
    return ExamApi(
        cookies,
        proxy=getattr(config, "proxy", ""),
        user_agent=user_agent,
    )


async def build_page_zhs_client(page: Page, config):
    from modules.zhs_api import ZhsApiClient

    cookies = await page.context.cookies()
    try:
        user_agent = await page.evaluate("navigator.userAgent")
    except Exception:
        user_agent = ""
    return ZhsApiClient(
        cookies,
        proxy=str(getattr(config, "proxy", "") or "").strip(),
        user_agent=user_agent,
    )


def sync_zhs_client_cookies(client, cookies) -> None:
    """把浏览器最新 Cookie 同步回 ZhsApiClient (人机验证后刷新会话凭证)."""
    if client is None:
        return
    from modules.zhs_api import ZhsApiClient, set_cookie_dict

    if isinstance(client, ZhsApiClient):
        client.sync_cookies(cookies)
        return
    session = getattr(client, "session", None)
    if session is None:
        return

    for cookie in cookies or []:
        if isinstance(cookie, dict):
            set_cookie_dict(session.cookies, cookie)
    reset = getattr(client, "reset_exam_sso", None)
    if callable(reset):
        reset()


def merge_playwright_cookies(base: list[dict], updates: list[dict]) -> list[dict]:
    """合并接口刷新的 Cookie 到本地 Playwright Cookie 列表, 不降级原有安全与过期属性."""
    if not updates:
        return list(base)
    out = [dict(item) for item in base if isinstance(item, dict)]
    for upd in updates:
        if not isinstance(upd, dict) or not upd.get("name"):
            continue
        u_name = upd.get("name")
        u_domain = str(upd.get("domain") or "").strip().lower()
        u_path = str(upd.get("path") or "").strip() or "/"
        matched = None
        for slot in out:
            s_domain = str(slot.get("domain") or "").strip().lower()
            s_path = str(slot.get("path") or "").strip() or "/"
            if slot.get("name") == u_name and s_domain == u_domain and s_path == u_path:
                matched = slot
                break
        if matched is None:
            out.append(dict(upd))
            continue
        u_exp = upd.get("expires")
        u_exp_valid = (
            u_exp
            if isinstance(u_exp, (int, float))
            and not isinstance(u_exp, bool)
            and u_exp > 0
            else None
        )
        s_exp = matched.get("expires")
        s_exp_valid = (
            s_exp
            if isinstance(s_exp, (int, float))
            and not isinstance(s_exp, bool)
            and s_exp > 0
            else None
        )
        if upd.get("value") == matched.get("value"):
            if s_exp_valid is None and u_exp_valid is not None:
                matched["expires"] = u_exp_valid
            elif (
                s_exp_valid is not None
                and u_exp_valid is not None
                and u_exp_valid > s_exp_valid
            ):
                matched["expires"] = u_exp_valid
            if not matched.get("sameSite") and upd.get("sameSite"):
                matched["sameSite"] = upd["sameSite"]
            matched["secure"] = bool(matched.get("secure")) or bool(upd.get("secure"))
            matched["httpOnly"] = bool(matched.get("httpOnly")) or bool(upd.get("httpOnly"))
        else:
            matched["value"] = upd.get("value")
            if u_exp_valid is not None:
                matched["expires"] = u_exp_valid
            if upd.get("sameSite"):
                matched["sameSite"] = upd["sameSite"]
            matched["secure"] = bool(matched.get("secure")) or bool(upd.get("secure"))
            matched["httpOnly"] = bool(matched.get("httpOnly")) or bool(upd.get("httpOnly"))
    return out
