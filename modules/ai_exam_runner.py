# encoding=utf-8
"""AI 智慧课综合考试上下文、知识点循环考试运行器与复习页接口接管。"""
from __future__ import annotations

import asyncio
import random
import sys
import threading
import time

from modules.exam_api import (
    AnswerBank,
    ExamApi,
    MAX_EXAM_ATTEMPTS,
    as_number,
    is_free_exam,
    parse_review_exam_url,
)
from modules.logger import Logger
from modules.ppt_extractor import PptExtractor

logger = Logger()


def _resolve_exam_ctx_cls():
    """允许测试通过 patch('modules.exam_ctx.ExamCtx', ...) 替换运行时上下文类。"""
    mod = sys.modules.get("modules.exam_ctx")
    if mod is not None and hasattr(mod, "ExamCtx"):
        return mod.ExamCtx
    from modules.exam_ctx import ExamCtx

    return ExamCtx


def _resolve_logger():
    """兼容测试对 modules.exam_ctx.logger 的 monkeypatch。"""
    mod = sys.modules.get("modules.exam_ctx")
    if mod is not None and hasattr(mod, "logger"):
        return mod.logger
    return logger


class ExamRunner:
    """按知识点循环取卷、答题、交卷, 直到掌握度达标或达到尝试上限."""

    def __init__(
        self,
        course_id,
        class_id,
        api: ExamApi,
        target: int = 100,
        max_attempts: int = MAX_EXAM_ATTEMPTS,
        op_extra: dict | None = None,
        bank: AnswerBank | None = None,
        enable_ppt: bool = True,
        cancel_event: threading.Event | None = None,
        skip_free_exam: bool = True,
    ):
        self.course_id = course_id
        self.class_id = class_id
        self.api = api
        self.target = target
        self.max_attempts = max_attempts
        self.op_extra = op_extra or {}
        self.bank = bank or AnswerBank(course_id)
        self.enable_ppt = bool(enable_ppt)
        self.cancel_event = cancel_event
        self.skip_free_exam = bool(skip_free_exam)
        self.extractor = PptExtractor(
            proxy=getattr(api, "proxy", ""),
            session=getattr(api, "session", None),
        )
        self._materials: dict[str, list[dict]] = {}

    def _is_cancelled(self) -> bool:
        return self.cancel_event is not None and self.cancel_event.is_set()

    def run(self, nodes: list[dict], deadline: float | None = None) -> dict:
        log = _resolve_logger()
        summary = {"done": [], "skipped": [], "failed": []}
        for index, node in enumerate(nodes, 1):
            if self._is_cancelled():
                log.warn("接口考试收到取消信号, 停止剩余知识点.")
                break
            if deadline is not None and time.monotonic() >= deadline:
                log.warn("接口考试达到时限, 停止剩余知识点.")
                break
            if self.skip_free_exam and is_free_exam(node):
                summary["skipped"].append(node.get("name") or node.get("nodeUid"))
                continue
            knowledge_id = node.get("nodeUid")
            if not knowledge_id:
                continue
            name = node.get("name") or knowledge_id
            extra = {
                **self.op_extra,
                "theme": node.get("module") or "",
                "knowledgePoint": name,
            }
            log.event(
                "知识点考试",
                序号=f"{index}/{len(nodes)}",
                知识点=name,
            )
            result = self._run_node(knowledge_id, name, extra, deadline)
            if result == "done":
                summary["done"].append(name)
            elif result == "skipped":
                summary["skipped"].append(name)
            else:
                summary["failed"].append(name)
        log.info(
            f"接口考试阶段汇总: 达标 {len(summary['done'])} 个, "
            f"跳过 {len(summary['skipped'])} 个, 未完成 {len(summary['failed'])} 个."
        )
        return summary

    def _collect_materials(self, knowledge_id, name: str) -> list[dict]:
        """按知识点缓存 PPT 参考资料, 多轮重考只下载解析一次."""
        if knowledge_id in self._materials:
            return self._materials[knowledge_id]
        resources = self.api.list_knowledge_resources(
            self.course_id, self.class_id, knowledge_id
        )
        materials = self.extractor.collect(
            resources,
            course_name=str(self.op_extra.get("courseName") or ""),
        )
        self._materials[knowledge_id] = materials
        if materials:
            _resolve_logger().info(
                f"知识点「{name}」已注入 {len(materials)} 份 PPT 参考资料 "
                f"({sum(len(item['content']) for item in materials)} 字)."
            )
        return materials

    def _run_node(self, knowledge_id, name: str, extra: dict, deadline) -> str:
        log = _resolve_logger()
        exam_ctx_cls = _resolve_exam_ctx_cls()
        for attempt in range(1, self.max_attempts + 1):
            if self._is_cancelled():
                return "failed"
            if deadline is not None and time.monotonic() >= deadline:
                return "failed"
            paper = self.api.query_paper(self.course_id, self.class_id, knowledge_id)
            if not paper or not paper.get("paperId"):
                log.info(f"知识点「{name}」没有可用的考试试卷.")
                return "skipped"
            raw_mastery = paper.get("highMasteryScore")
            if raw_mastery is None:
                raw_mastery = paper.get("masteryScore")
            mastery = as_number(raw_mastery)
            if mastery >= self.target:
                log.info(f"知识点「{name}」掌握度已达 {mastery:g}, 跳过.")
                return "done"
            if mastery < 30 and attempt > 4:
                log.warn(f"知识点「{name}」掌握度始终低于 30, 放弃.")
                return "failed"
            log.info(
                f"知识点「{name}」第 {attempt}/{self.max_attempts} 轮考试, "
                f"当前掌握度 {mastery:g}."
            )
            reference_materials = (
                self._collect_materials(knowledge_id, name) if self.enable_ppt else []
            )
            ctx = exam_ctx_cls(
                api=self.api,
                course_id=self.course_id,
                knowledge_id=knowledge_id,
                exam_test_id=paper["examTestId"],
                exam_paper_id=paper["paperId"],
                op_extra=extra,
                answer_bank=self.bank,
                cancel_event=self.cancel_event,
            )
            try:
                success, correct, total = ctx.start(
                    reference_materials=reference_materials
                )
            except Exception as exc:
                log.log_exception(f"知识点「{name}」考试异常.", exc)
                return "failed"
            if self._is_cancelled():
                return "failed"
            log.info(
                f"知识点「{name}」第 {attempt}/{self.max_attempts} 轮结束: "
                f"得分 {correct}/{total}" + (" (全对)" if success else "")
            )
            log.event(
                "知识点考试结果",
                知识点=name,
                轮次=attempt,
                得分=f"{correct}/{total}",
                全对=success,
            )
            wait_sec = random.uniform(0.8, 1.6)
            if self.cancel_event is not None:
                if self.cancel_event.wait(wait_sec):
                    return "failed"
            else:
                time.sleep(wait_sec)
        return "failed"


async def answer_review_exam_page(page, config=None) -> bool:
    """把当前复习/模拟考试页接管为接口作答.

    返回 True 表示已接管(无论接口是否成功, 避免 UI 流程再交一次卷);
    返回 False 表示页面参数不足, 交给原有 UI 流程处理.
    """
    log = _resolve_logger()
    exam_ctx_cls = _resolve_exam_ctx_cls()
    info = parse_review_exam_url(getattr(page, "url", "") or "")
    if not info.get("examTestId") or not info.get("paperId"):
        log.warn("复习考试页缺少 examTestId/paperId, 无法接口作答.")
        return False
    try:
        cookies = await page.context.cookies()
        user_agent = await page.evaluate("navigator.userAgent")
    except Exception as exc:
        log.log_exception("读取浏览器 Cookie 失败, 无法接口作答.", exc)
        return False
    api = ExamApi(
        cookies,
        proxy=getattr(config, "proxy", "") if config is not None else "",
        user_agent=user_agent,
    )
    cancel_event = getattr(config, "_cancel_event", None) if config is not None else None
    if cancel_event is None:
        cancel_event = threading.Event()
    ctx = exam_ctx_cls(
        api,
        info.get("courseId") or "review",
        0,
        info["examTestId"],
        info["paperId"],
        op_extra={"knowledgePoint": info.get("nodeName") or "", "来源": "复习考试"},
        cancel_event=cancel_event,
    )
    log.event("复习考试接口作答", 试卷=info["paperId"], 考试=info["examTestId"])
    try:
        try:
            success, correct, total = await asyncio.to_thread(ctx.start)
        except asyncio.CancelledError:
            cancel_event.set()
            raise
        except Exception as exc:
            log.log_exception("复习考试接口作答失败.", exc)
            log.warn("复习考试未能通过接口完成, 请手动检查并交卷.")
            return True
    finally:
        ctx.close()
        api.close()
    log.info(
        f"复习/模拟考试接口作答完成: 得分 {correct}/{total}"
        + (" (全对)" if success else "")
    )
    log.event("复习考试结果", 得分=f"{correct}/{total}", 全对=success)
    return True
