# encoding=utf-8
"""AI 智慧课阶段（资源补齐、接口考试、综合考试、掌握度刷题）与任务直达路由。"""
from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import threading
import time

from playwright.async_api import Page

from modules import homework, quiz_page
from modules.ai_smart import complete_resources_for_node, is_ai_smart_url
from modules.ai_smart_mastery import (
    AnswerCache,
    build_learn_url,
    enumerate_nodes,
    run_course_mastery,
)
from modules.answer_store import AiQuestionStore
from modules.exam_api import is_free_exam
from modules.exam_ctx import AiFormalExamCtx, ExamApi, ExamRunner
from modules.runtime_bootstrap import build_page_exam_api, resolve_cancel_event
from modules.url_router import (
    KIND_EXAM,
    KIND_HOMEWORK,
    KIND_KNOWLEDGE_STUDY,
    KIND_LEARN_PAGE,
    KIND_TEST_DETAIL,
    parse_url,
)
from modules.utils import get_runtime_path

_DIRECT_TASK_KINDS = (
    KIND_HOMEWORK,
    KIND_EXAM,
    KIND_TEST_DETAIL,
    KIND_LEARN_PAGE,
)


def _av(name: str, fallback):
    """兼容测试通过 patch.object(Autovisor, ...) 替换运行时依赖。"""
    mod = sys.modules.get("Autovisor")
    if mod is not None and hasattr(mod, name):
        return getattr(mod, name)
    return fallback


def _formal_exam_succeeded(
    ctx, submitted: bool, answered: int, total: int, auto_submit: bool
) -> bool:
    failed_saves = getattr(ctx, "_failed_saves", 0)
    if not isinstance(failed_saves, int) or isinstance(failed_saves, bool):
        failed_saves = 0
    if auto_submit:
        return bool(submitted)
    return (total == 0) or (answered >= total and failed_saves == 0)


def course_route(parsed: dict, course_url: str) -> str:
    """决定课程 URL 走精准任务路由 / ai-smart 全课流程 / 常规目录."""
    kind = (parsed or {}).get("kind", "")
    if kind in _DIRECT_TASK_KINDS:
        return "task"
    if kind == KIND_KNOWLEDGE_STUDY or is_ai_smart_url(course_url):
        return "ai_smart"
    return "catalog"


async def run_resources_stage(
    page: Page, course_url: str, config, logger, playback_enabled
) -> dict:
    """按知识点逐个核对「必学资源」, 把漏掉的视频/文档/PPT 补齐."""
    enum_fn = _av("enumerate_nodes", enumerate_nodes)
    build_url_fn = _av("build_learn_url", build_learn_url)
    complete_fn = _av("complete_resources_for_node", complete_resources_for_node)

    logger.section("必学资源补齐")
    payload = await enum_fn(page, course_url)
    nodes = list(payload.get("nodes") or [])
    logger.event("资源补齐范围", 知识点=len(nodes))
    started_at = time.monotonic()
    total_done = total_failed = 0
    for index, node in enumerate(nodes, 1):
        if getattr(config, "limitMaxTime", 0) and (
            time.monotonic() - started_at
        ) / 60 >= config.limitMaxTime:
            logger.warn("达到单课时限, 停止补齐资源.")
            break
        name = node.get("name") or node["nodeUid"]
        learn_url = build_url_fn(
            payload["courseId"], node["nodeUid"], payload["classId"]
        )
        result = await complete_fn(
            page, learn_url, config, logger, playback_enabled, started_at
        )
        if result.get("ok"):
            total_done += result.get("done", 0)
            total_failed += result.get("failed", 0)
            if result.get("done") or result.get("failed"):
                logger.event(
                    "资源补齐",
                    序号=f"{index}/{len(nodes)}",
                    名称=name,
                    补齐=result.get("done"),
                    已完成=result.get("skipped"),
                    失败=result.get("failed"),
                )
        else:
            logger.debug(f"跳过 {name}: {result.get('reason')}")
    logger.info(f"必学资源补齐完成: 补齐 {total_done} 项, 失败 {total_failed} 项.")
    logger.event("资源补齐汇总", 补齐=total_done, 失败=total_failed)
    return {"done": total_done, "failed": total_failed}


def run_formal_exam_tasks(
    api: ExamApi,
    course_id,
    class_id,
    auto_submit: bool,
    logger,
    *,
    cancel_event: threading.Event | None = None,
) -> dict:
    """扫描 AI 智慧课综合/单元测试任务并逐个执行 (在线程中调用)."""
    formal_cls = _av("AiFormalExamCtx", AiFormalExamCtx)
    probe = formal_cls.from_task(
        api,
        {"courseId": course_id, "classId": class_id},
        auto_submit=auto_submit,
        cancel_event=cancel_event,
    )
    try:
        tasks = probe.scan_pending_tasks()
    finally:
        close_fn = getattr(probe, "close", None)
        if callable(close_fn):
            close_fn()
    summary = {
        "found": len(tasks),
        "done": 0,
        "submitted": 0,
        "failed": 0,
        "details": [],
    }
    for task in tasks:
        if cancel_event is not None and cancel_event.is_set():
            logger.warn("综合考试收到取消信号, 停止剩余任务.")
            break
        task = dict(task)
        if not task.get("courseId"):
            task["courseId"] = course_id
        if class_id and not task.get("classId"):
            task["classId"] = class_id
        name = (
            task.get("taskName")
            or task.get("examName")
            or task.get("name")
            or str(task.get("examTestId") or "")
        )
        ctx = formal_cls.from_task(
            api, task, auto_submit=auto_submit, cancel_event=cancel_event
        )
        try:
            submitted, answered, total = ctx.start(submit=auto_submit)
        except Exception as exc:
            summary["failed"] += 1
            summary["details"].append(
                {
                    "name": name,
                    "ok": False,
                    "success": False,
                    "submitted": False,
                    "error": str(exc),
                }
            )
            logger.log_exception(f"综合考试「{name}」执行失败.", exc)
            continue
        ok = _formal_exam_succeeded(ctx, submitted, answered, total, auto_submit)
        if submitted:
            summary["submitted"] += 1
        if ok:
            summary["done"] += 1
        else:
            summary["failed"] += 1
        summary["details"].append(
            {
                "name": name,
                "ok": ok,
                "success": ok,
                "submitted": bool(submitted),
                "answered": answered,
                "total": total,
            }
        )
    return summary


async def run_formal_exam_stage(
    page: Page,
    course_url: str,
    config,
    logger,
    *,
    api: ExamApi | None = None,
    course_id: str = "",
    class_id: str = "",
) -> dict:
    """AI 智慧课: 独立扫描并执行综合/单元测试任务."""
    enum_fn = _av("enumerate_nodes", enumerate_nodes)
    build_api_fn = _av("build_page_exam_api", build_page_exam_api)
    run_tasks_fn = _av("run_formal_exam_tasks", run_formal_exam_tasks)
    qp_mod = _av("quiz_page", quiz_page)

    if not getattr(config, "enableFormalExamApi", True):
        return {}
    if not course_id or not class_id:
        parsed = parse_url(course_url)
        course_id = course_id or str(parsed.get("courseId") or "")
        class_id = class_id or str(parsed.get("classId") or "")
    if not course_id:
        try:
            payload = await enum_fn(page, course_url)
        except Exception as exc:
            logger.debug(f"综合考试解析课程 ID 失败: {exc}")
            payload = {}
        course_id = course_id or str(payload.get("courseId") or "")
        class_id = class_id or str(payload.get("classId") or "")
    own_api = api is None
    if own_api:
        api = await build_api_fn(page, config)
    auto_submit = bool(qp_mod.llm.conf().get("auto_submit", False))
    cancel_event = resolve_cancel_event(config)
    logger.section("综合考试")
    try:
        try:
            formal = await asyncio.to_thread(
                run_tasks_fn,
                api,
                course_id,
                class_id,
                auto_submit,
                logger,
                cancel_event=cancel_event,
            )
        except asyncio.CancelledError:
            cancel_event.set()
            raise
    finally:
        if own_api:
            close_fn = getattr(api, "close", None)
            if callable(close_fn):
                close_fn()
    if formal.get("found"):
        logger.event(
            "综合考试汇总",
            任务=formal["found"],
            完成=formal["done"],
            已交卷=formal.get("submitted", 0),
            失败=formal["failed"],
        )
    return formal


async def run_exam_stage(
    page: Page, course_url: str, config, logger, *, run_formal: bool = True
) -> dict:
    """AI 智慧课: 用接口版 ExamCtx 按知识点刷计分考试，并独立调度综合考试扫描."""
    enum_fn = _av("enumerate_nodes", enumerate_nodes)
    build_api_fn = _av("build_page_exam_api", build_page_exam_api)
    runner_cls = _av("ExamRunner", ExamRunner)
    formal_stage_fn = _av("run_formal_exam_stage", run_formal_exam_stage)

    summary: dict = {}
    api = None
    course_id = ""
    class_id = ""
    try:
        if getattr(config, "enableExamApi", True):
            logger.section("接口考试")
            try:
                payload = await enum_fn(page, course_url)
                course_id = str(payload.get("courseId") or "")
                class_id = str(payload.get("classId") or "")
                nodes = [
                    node for node in payload.get("nodes") or [] if node.get("nodeUid")
                ]
                if not nodes:
                    logger.warn("没有枚举到知识点, 跳过知识点接口考试.")
                else:
                    api = await build_api_fn(page, config)
                    limit = getattr(config, "limitMaxTime", 0) or 0
                    deadline = time.monotonic() + limit * 60 if limit > 0 else None
                    cancel_event = resolve_cancel_event(config)
                    runner = runner_cls(
                        course_id,
                        class_id,
                        api,
                        target=int(config.masteryTarget),
                        enable_ppt=getattr(config, "enablePptReference", True),
                        cancel_event=cancel_event,
                    )
                    logger.info(
                        f"开始接口考试: 知识点 {len(nodes)} 个, 目标掌握度 {int(config.masteryTarget)}%"
                    )
                    logger.event(
                        "接口考试范围",
                        知识点=len(nodes),
                        目标掌握度=int(config.masteryTarget),
                    )
                    try:
                        summary = await asyncio.to_thread(runner.run, nodes, deadline)
                    except asyncio.CancelledError:
                        cancel_event.set()
                        raise
                    logger.event(
                        "接口考试汇总",
                        达标=len(summary.get("done") or []),
                        跳过=len(summary.get("skipped") or []),
                        未完成=len(summary.get("failed") or []),
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.log_exception("接口考试阶段失败.", exc)

        if run_formal and getattr(config, "enableFormalExamApi", True):
            try:
                formal = await formal_stage_fn(
                    page,
                    course_url,
                    config,
                    logger,
                    api=api,
                    course_id=course_id,
                    class_id=class_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.log_exception("综合考试扫描失败.", exc)
            else:
                summary["formal_exams"] = formal
        return summary
    finally:
        if api is not None:
            close_fn = getattr(api, "close", None)
            if callable(close_fn):
                close_fn()


async def run_formal_exam_route(page: Page, parsed: dict, config, logger) -> dict:
    """testDetail 链接: 直接按综合考试接口作答."""
    build_api_fn = _av("build_page_exam_api", build_page_exam_api)
    formal_cls = _av("AiFormalExamCtx", AiFormalExamCtx)
    qp_mod = _av("quiz_page", quiz_page)

    api = await build_api_fn(page, config)
    try:
        auto_submit = bool(qp_mod.llm.conf().get("auto_submit", False))
        task = {
            "courseId": parsed.get("courseId", ""),
            "classId": parsed.get("classId", ""),
            "examTestId": parsed.get("examTestId", ""),
            "examPaperId": parsed.get("examPaperId", ""),
        }
        logger.info(f"检测到综合考试链接, 开始接口作答 (自动交卷={auto_submit}) ...")
        logger.event("综合考试", 状态="开始", 自动交卷=auto_submit)
        cancel_event = resolve_cancel_event(config)
        ctx = formal_cls.from_task(
            api, task, auto_submit=auto_submit, cancel_event=cancel_event
        )
        try:
            submitted, answered, total = await asyncio.to_thread(ctx.start, submit=auto_submit)
        except asyncio.CancelledError:
            cancel_event.set()
            raise
        ok = _formal_exam_succeeded(ctx, submitted, answered, total, auto_submit)
        logger.event(
            "综合考试",
            状态="结束",
            成功=ok,
            已交卷=bool(submitted),
            作答=answered,
            总数=total,
        )
        return {"success": ok, "answered": answered, "total": total}
    finally:
        close_fn = getattr(api, "close", None)
        if callable(close_fn):
            close_fn()


async def run_learn_page_route(
    page: Page, parsed: dict, config, logger, playback_enabled
) -> dict:
    """learnPage 单知识点链接: 先补资源, 再只对该节点跑接口考试."""
    build_url_fn = _av("build_learn_url", build_learn_url)
    complete_fn = _av("complete_resources_for_node", complete_resources_for_node)
    build_api_fn = _av("build_page_exam_api", build_page_exam_api)
    runner_cls = _av("ExamRunner", ExamRunner)

    course_id = str(parsed.get("courseId", ""))
    node_uid = str(parsed.get("nodeUid", ""))
    class_id = str(parsed.get("classId", ""))
    if not course_id or not node_uid or not class_id:
        logger.warn("learnPage 链接缺少 courseId/nodeUid/classId, 已跳过.")
        return {}
    learn_url = build_url_fn(course_id, node_uid, class_id)
    if not getattr(config, "reviewMode", False):
        result = await complete_fn(
            page, learn_url, config, logger, playback_enabled, time.monotonic()
        )
        logger.event(
            "资源补齐",
            知识点=node_uid,
            补齐=result.get("done"),
            已完成=result.get("skipped"),
            失败=result.get("failed"),
        )
    if not getattr(config, "enableExamApi", True):
        return {}
    api = await build_api_fn(page, config)
    try:
        limit = getattr(config, "limitMaxTime", 0) or 0
        deadline = time.monotonic() + limit * 60 if limit > 0 else None
        cancel_event = resolve_cancel_event(config)
        runner = runner_cls(
            course_id,
            class_id,
            api,
            target=int(config.masteryTarget),
            enable_ppt=getattr(config, "enablePptReference", True),
            cancel_event=cancel_event,
        )
        node = {"nodeUid": node_uid, "name": f"知识点 {node_uid}"}
        try:
            summary = await asyncio.to_thread(runner.run, [node], deadline)
        except asyncio.CancelledError:
            cancel_event.set()
            raise
        logger.event(
            "接口考试汇总",
            达标=len(summary.get("done") or []),
            跳过=len(summary.get("skipped") or []),
            未完成=len(summary.get("failed") or []),
        )
        return summary
    finally:
        close_fn = getattr(api, "close", None)
        if callable(close_fn):
            close_fn()


async def run_task_url(
    page: Page, course_url: str, parsed: dict, config, logger, playback_enabled
) -> dict:
    """执行 dohomework/doexam/testDetail/learnPage 链接对应的任务."""
    hw_mod = _av("homework", homework)
    formal_route_fn = _av("run_formal_exam_route", run_formal_exam_route)
    learn_route_fn = _av("run_learn_page_route", run_learn_page_route)

    kind = parsed.get("kind", "")
    if kind in (KIND_HOMEWORK, KIND_EXAM):
        stats = await hw_mod.auto_answer_homework(page, config=config)
        logger.event(
            "任务链接",
            类型=kind,
            作答=stats.get("answered"),
            已交卷=stats.get("submitted"),
        )
        return stats
    if kind == KIND_TEST_DETAIL:
        return await formal_route_fn(page, parsed, config, logger)
    return await learn_route_fn(
        page, parsed, config, logger, playback_enabled
    )


async def run_mastery_stage(page: Page, course_url: str, config, logger) -> dict:
    """AI 智慧课: 视频刷完后, 把每个知识点的掌握度刷到目标值."""
    enum_fn = _av("enumerate_nodes", enumerate_nodes)
    mastery_fn = _av("run_course_mastery", run_course_mastery)

    logger.section("掌握度提升")
    payload = await enum_fn(page, course_url)
    nodes = payload.get("nodes") or []
    free_count = sum(1 for node in nodes if is_free_exam(node))
    logger.event("知识点枚举", 总数=len(nodes), 免考=free_count)
    if not nodes:
        logger.warn("没有枚举到知识点, 跳过掌握度阶段.")
        return {}
    cache = AnswerCache(
        Path(get_runtime_path("data", "mastery_answers.json")),
        store=AiQuestionStore(),
    )
    target = int(config.masteryTarget)
    logger.info(f"开始刷掌握度: 知识点 {len(nodes)} 个, 目标 {target}%")
    summary = await mastery_fn(
        page,
        nodes,
        payload["courseId"],
        payload["classId"],
        cache,
        logger,
        target=target,
        max_attempts=3,
        force_redo=bool(getattr(config, "forceRedoAnswered", False)),
    )
    return summary
