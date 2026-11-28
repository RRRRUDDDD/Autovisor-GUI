# encoding=utf-8
"""知到共享课/融合课的作业与正式考试自动作答阶段（统一合并作业与考试循环）。"""
from __future__ import annotations

import asyncio
import sys
import threading

from playwright.async_api import Page, TimeoutError

from modules import homework, quiz_page
from modules.answer_store import AiQuestionStore
from modules.logger import Logger
from modules.runtime_bootstrap import (
    build_page_zhs_client,
    resolve_cancel_event,
    sync_zhs_client_cookies,
)
# 预导入避免测试在 patch ZhidaoHomeworkWorker 时首次触发 ZhidaoExamWorker 子类定义
import modules.zhidao_homework_worker as _zhidao_hw_mod  # noqa: F401
import modules.zhidao_exam_worker as _zhidao_exam_mod  # noqa: F401

_logger = Logger()


def _av(name: str, fallback):
    """兼容测试通过 patch.object(Autovisor, ...) 替换运行时依赖。"""
    mod = sys.modules.get("Autovisor")
    if mod is not None and hasattr(mod, name):
        return getattr(mod, name)
    return fallback


def resolve_zhidao_course_meta(client, recruit_and_course_id: str) -> dict:
    """用共享课 recruitAndCourseId 反查 recruitId / courseId / schoolId / courseName."""
    from modules.zhidao_exam_api import ZhidaoExamApi, extract_school_id
    from modules.zhidao_homework_api import ZhidaoHomeworkApi, extract_course_meta

    hw_api = ZhidaoHomeworkApi(client)
    meta = extract_course_meta(hw_api.query_course(recruit_and_course_id))
    if meta.get("recruitId") and meta.get("courseId") and not meta.get("schoolId"):
        try:
            meta["schoolId"] = extract_school_id(
                ZhidaoExamApi(client).get_login_school_info()
            )
        except Exception as exc:
            _av("logger", _logger).debug(f"补充获取 schoolId 失败, 继续使用空值: {exc}")
    return meta


async def _run_zhidao_with_captcha(
    page: Page,
    client,
    config,
    logger,
    *,
    label: str,
    captcha_url: str,
    run_fn,
    cancel_event: threading.Event | None = None,
):
    """在线程中执行知到接口调用; 若触发人机验证则在浏览器中处理后重试一次."""
    from modules.zhs_api import CaptchaRequiredError

    hw_mod = _av("homework", homework)
    last_error = None
    for attempt in range(2):
        try:
            return await asyncio.to_thread(run_fn)
        except asyncio.CancelledError:
            if cancel_event is not None:
                cancel_event.set()
            raise
        except CaptchaRequiredError as exc:
            last_error = exc
            if not getattr(exc, "retryable", True):
                logger.warn(
                    f"「{label}」交卷阶段触发人机验证, 已中止重试以防重复交卷, 请手动核对交卷状态: {exc}"
                )
                break
            logger.warn(f"「{label}」触发人机验证: {exc}")
            if attempt == 0:
                if captcha_url and getattr(page, "goto", None):
                    try:
                        await page.goto(
                            captcha_url, wait_until="domcontentloaded", timeout=30000
                        )
                    except Exception as nav_exc:
                        logger.debug(f"打开验证页失败: {nav_exc}")
                if await hw_mod._settle_captcha(page, config):
                    try:
                        sync_zhs_client_cookies(client, await page.context.cookies())
                    except Exception as sync_exc:
                        logger.debug(f"人机验证后同步 Cookie 失败: {sync_exc}")
                    logger.info(f"「{label}」人机验证已通过, 重试接口作答...")
                    continue
            break
    raise last_error


async def _run_vision_fallback_on_url(
    page: Page,
    url: str,
    config,
    logger,
    *,
    label: str,
    api_answered_text: bool = False,
) -> dict:
    """作业/考试含图片题时导航到直达页, 跳过已跑过的 API 阶段直接用视觉模型补答."""
    qp_mod = _av("quiz_page", quiz_page)
    hw_mod = _av("homework", homework)

    if not qp_mod.llm.vision_enabled():
        logger.warn(
            f"「{label}」含图片题, 但未配置视觉模型, 无法识别图片题干. "
            "请在设置的大模型卡片填写支持读图的「视觉模型」, 已跳过图片题."
        )
        return {"skipped": True, "reason": "no_vision_model"}
    logger.info(f"「{label}」含图片题, 正在打开答题页使用视觉模型补答...")
    prev_base = str(getattr(page, "url", "") or "").split("#", 1)[0]
    target_base = str(url or "").split("#", 1)[0]
    same_document = bool(prev_base and target_base and prev_base == target_base)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        if same_document and callable(getattr(page, "reload", None)):
            await page.reload(wait_until="domcontentloaded", timeout=30000)
    except TimeoutError:
        logger.warn(f"「{label}」答题页加载较慢, 继续等待题目渲染...")
    await page.wait_for_timeout(1500)
    return await hw_mod.auto_answer_homework(
        page,
        config=config,
        skip_api=True,
        api_answered_text=api_answered_text,
    )


async def _execute_assessment_item(
    *,
    page: Page,
    client,
    config,
    logger,
    cancel_event: threading.Event,
    summary_bucket: dict,
    idx: int,
    total_count: int,
    item_kind: str,
    event_name: str,
    item_name: str,
    item_url: str,
    start_log: str,
    run_fn,
    check_locked: bool = False,
    include_score: bool = False,
) -> None:
    """统一执行单份知到作业或正式考试的接口作答、视觉兜底与统计归档。"""
    logger.info(start_log)
    logger.event(
        event_name,
        状态="开始",
        名称=item_name,
        序号=f"{idx}/{total_count}",
    )
    try:
        stats = await _run_zhidao_with_captcha(
            page,
            client,
            config,
            logger,
            label=item_name,
            captcha_url=item_url,
            run_fn=run_fn,
            cancel_event=cancel_event,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        summary_bucket["failed"] += 1
        summary_bucket["details"].append(
            {"name": item_name, "ok": False, "error": str(exc)}
        )
        logger.warn(f"[{idx}/{total_count}] {item_kind}「{item_name}」作答失败: {exc}")
        logger.event(
            event_name,
            状态="失败",
            名称=item_name,
            序号=f"{idx}/{total_count}",
            错误=logger.summarize_exception(exc),
        )
        return

    if check_locked and stats.get("skipped"):
        summary_bucket["skipped"] += 1
        summary_bucket["details"].append(
            {"name": item_name, "skipped": True, "reason": "locked"}
        )
        return

    vision_stats = None
    if stats.get("needs_vision"):
        vision_stats = await _run_vision_fallback_on_url(
            page,
            item_url,
            config,
            logger,
            label=item_name,
            api_answered_text=int(stats.get("answered", 0) or 0) > 0,
        )
        stats["vision"] = vision_stats

    answered = int(stats.get("answered", 0) or 0)
    if isinstance(vision_stats, dict) and not vision_stats.get("skipped"):
        answered += int(vision_stats.get("ok", 0) or 0) + int(
            vision_stats.get("fill", 0) or 0
        )

    if answered == 0:
        if isinstance(vision_stats, dict) and vision_stats.get("skipped"):
            summary_bucket["skipped"] += 1
            summary_bucket["details"].append(
                {
                    "name": item_name,
                    "skipped": True,
                    "reason": vision_stats.get("reason", "no_vision_model"),
                }
            )
        elif int((vision_stats or {}).get("fail", 0) or 0) > 0:
            fail_cnt = int((vision_stats or {}).get("fail", 0) or 0)
            summary_bucket["failed"] += 1
            summary_bucket["details"].append(
                {
                    "name": item_name,
                    "ok": False,
                    "reason": "vision_failed",
                }
            )
            logger.warn(
                f"[{idx}/{total_count}] {item_kind}「{item_name}」视觉补答失败 "
                f"(失败 {fail_cnt} 题)."
            )
            logger.event(
                event_name,
                状态="失败",
                名称=item_name,
                序号=f"{idx}/{total_count}",
                原因="vision_failed",
            )
        else:
            summary_bucket["skipped"] += 1
            summary_bucket["details"].append(
                {"name": item_name, "skipped": True, "reason": "no_questions"}
            )
        return

    submitted = bool(stats.get("submitted")) or bool(
        isinstance(vision_stats, dict) and vision_stats.get("submitted")
    )
    summary_bucket["done"] += 1
    detail: dict = {
        "name": item_name,
        "ok": True,
        "answered": answered,
        "submitted": submitted,
        "needs_vision": bool(stats.get("needs_vision")),
    }
    if include_score:
        detail["score"] = stats.get("score")
    summary_bucket["details"].append(detail)

    if include_score:
        logger.info(
            f"[{idx}/{total_count}] {item_kind}「{item_name}」完成: "
            f"作答 {answered} 题, 已提交={submitted}, 得分={stats.get('score')}"
        )
        logger.event(
            event_name,
            状态="完成",
            名称=item_name,
            序号=f"{idx}/{total_count}",
            作答=answered,
            已交卷=submitted,
            得分=stats.get("score"),
        )
    else:
        logger.info(
            f"[{idx}/{total_count}] {item_kind}「{item_name}」完成: "
            f"作答 {answered} 题, 已交卷={submitted}"
        )
        logger.event(
            event_name,
            状态="完成",
            名称=item_name,
            序号=f"{idx}/{total_count}",
            作答=answered,
            已交卷=submitted,
        )


async def run_zhidao_assessment_stage(
    page: Page, course_url: str, parsed: dict, config, logger
) -> dict:
    """共享课/融合课视频完成后: 自动扫描并完成知到作业与进行中的正式考试."""
    build_client_fn = _av("build_page_zhs_client", build_page_zhs_client)

    recruit_secret = str((parsed or {}).get("recruitAndCourseId") or "").strip()
    do_hw = bool(getattr(config, "enableHomeworkApi", True))
    do_exam = bool(getattr(config, "enableFormalExamApi", True))
    if not recruit_secret or (not do_hw and not do_exam):
        return {}

    client = await build_client_fn(page, config)
    resources = [client]
    try:
        return await _run_zhidao_assessment_with_client(
            page,
            course_url,
            recruit_secret,
            config,
            logger,
            client=client,
            do_hw=do_hw,
            do_exam=do_exam,
            resources=resources,
        )
    finally:
        for res in reversed(resources):
            close_fn = getattr(res, "close", None)
            if callable(close_fn):
                try:
                    close_fn()
                except Exception:
                    pass


async def _run_zhidao_assessment_with_client(
    page: Page,
    course_url: str,
    recruit_secret: str,
    config,
    logger,
    *,
    client,
    do_hw: bool,
    do_exam: bool,
    resources: list,
) -> dict:
    from modules.ai_analysis_api import AiAnalysisApi
    from modules.yidun_fp import get_session_fp, probe_fp_from_page
    from modules.zhidao_exam_api import ZhidaoExamApi, is_exam_started
    from modules.zhidao_exam_worker import ZhidaoExamWorker
    from modules.zhidao_homework_api import (
        ZhidaoHomeworkApi,
        build_exam_page_url,
        check_time_window,
    )
    from modules.zhidao_homework_worker import (
        ZhidaoHomeworkWorker,
        should_redo_submitted,
    )

    resolve_meta_fn = _av("resolve_zhidao_course_meta", resolve_zhidao_course_meta)
    qp_mod = _av("quiz_page", quiz_page)

    try:
        meta = await _run_zhidao_with_captcha(
            page,
            client,
            config,
            logger,
            label="课程信息查询",
            captcha_url=course_url,
            run_fn=lambda: resolve_meta_fn(client, recruit_secret),
        )
    except Exception as exc:
        logger.warn(f"查询共享课课程信息失败, 跳过知到作业与考试扫描: {exc}")
        logger.event(
            "知到课程扫描",
            结果="查询失败",
            错误=logger.summarize_exception(exc),
        )
        return {}

    recruit_id = meta.get("recruitId", "")
    course_id = meta.get("courseId", "")
    school_id = meta.get("schoolId", "")
    course_name = meta.get("courseName", "")
    if not recruit_id or not course_id:
        logger.warn("未能从当前课程解析出 recruitId/courseId, 跳过知到作业与考试扫描.")
        return {}

    proxy = str(getattr(config, "proxy", "") or "").strip()
    auto_submit = bool(qp_mod.llm.conf().get("auto_submit", False))
    enable_http_captcha = bool(getattr(config, "enableAutoCaptcha", True))
    page_context = getattr(page, "context", None)
    if enable_http_captcha and not get_session_fp(page_context):
        await probe_fp_from_page(page, context=page_context)
    def fp_provider(ctx=page_context):
        return get_session_fp(ctx)
    cancel_event = resolve_cancel_event(config)
    store = AiQuestionStore()
    if not qp_mod.llm.enabled():
        logger.warn(
            "未配置自定义大模型 API Key, 知到作业/考试仅在本地题库命中时生效; "
            "如需自动作答新题请在设置的大模型卡片配置 API Key."
        )

    summary = {
        "course_id": course_id,
        "recruit_id": recruit_id,
        "homework": {"found": 0, "done": 0, "skipped": 0, "failed": 0, "details": []},
        "exam": {"found": 0, "done": 0, "skipped": 0, "failed": 0, "details": []},
    }

    if do_hw:
        logger.section("知到作业")
        threshold = float(getattr(config, "homeworkThreshold", 100) or 0)
        max_redo = int(getattr(config, "homeworkMaxRedo", 3) or 0)
        hw_api = ZhidaoHomeworkApi(client)
        clone_fn = getattr(client, "clone_session", None)
        analysis_session = (
            clone_fn() if callable(clone_fn) else getattr(client, "session", None)
        )
        analysis_api = AiAnalysisApi(
            session=analysis_session,
            proxy=proxy,
            owns_session=callable(clone_fn)
            and analysis_session is not getattr(client, "session", None),
        )
        resources.append(analysis_api)
        hw_worker = ZhidaoHomeworkWorker(
            hw_api,
            course_id=course_id,
            course_name=course_name,
            recruit_id=recruit_id,
            school_id=school_id,
            store=store,
            analysis_api=analysis_api,
            auto_submit=auto_submit,
            target_score=threshold,
            max_rounds=max_redo,
            cancel_event=cancel_event,
            fp_provider=fp_provider,
            enable_http_captcha=enable_http_captcha,
        )
        resources.append(hw_worker)
        try:
            pending_items = await asyncio.to_thread(
                hw_worker.scan, 1, recruit_id=recruit_id, course_id=course_id
            )
        except Exception as exc:
            logger.warn(f"扫描知到待完成作业失败: {exc}")
            pending_items = []

        candidates = list(pending_items)
        seen_keys = {
            (item.get("studentExamId", ""), item.get("examId", ""))
            for item in candidates
        }
        if auto_submit and threshold > 0:
            try:
                submitted_items = await asyncio.to_thread(
                    hw_worker.scan, 2, recruit_id=recruit_id, course_id=course_id
                )
            except Exception as exc:
                logger.debug(f"扫描已提交未达标作业失败: {exc}")
                submitted_items = []
            for item in submitted_items:
                key = (item.get("studentExamId", ""), item.get("examId", ""))
                if key in seen_keys:
                    continue
                if should_redo_submitted(item, target_score=threshold):
                    seen_keys.add(key)
                    candidates.append(item)

        hw_summary = summary["homework"]
        hw_summary["found"] = len(candidates)
        if not candidates:
            logger.info("当前课程暂无待处理的知到作业.")
        else:
            logger.info(
                f"扫描到 {len(candidates)} 份待处理知到作业 "
                f"(自动提交={auto_submit}, 达标线={threshold:g}%)..."
            )
            total_hw = len(candidates)
            for idx, item in enumerate(candidates, 1):
                exam_id = item.get("examId", "")
                stu_exam_id = item.get("studentExamId", "")
                exam_name = item.get("examName") or exam_id or f"作业 {idx}"
                item_recruit = item.get("recruitId") or recruit_id
                item_course = item.get("courseId") or course_id
                item_school = (
                    item.get("schoolId")
                    or (item.get("raw") or {}).get("schoolId")
                    or school_id
                )
                raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
                in_window, reason = check_time_window(
                    item.get("startTime") or raw.get("startTime"),
                    item.get("endDate") or raw.get("endDate"),
                )
                if not in_window:
                    label = "尚未开放" if reason == "not_started" else "已截止"
                    logger.info(f"[{idx}/{total_hw}] 作业「{exam_name}」{label}, 跳过.")
                    hw_summary["skipped"] += 1
                    hw_summary["details"].append(
                        {"name": exam_name, "skipped": True, "reason": reason}
                    )
                    continue

                item_url = build_exam_page_url(
                    "dohomework",
                    recruit_id=item_recruit,
                    stu_exam_id=stu_exam_id,
                    exam_id=exam_id,
                    course_id=item_course,
                    school_id=item_school,
                )
                await _execute_assessment_item(
                    page=page,
                    client=client,
                    config=config,
                    logger=logger,
                    cancel_event=cancel_event,
                    summary_bucket=hw_summary,
                    idx=idx,
                    total_count=total_hw,
                    item_kind="作业",
                    event_name="知到作业",
                    item_name=exam_name,
                    item_url=item_url,
                    start_log=f"[{idx}/{total_hw}] 开始作答知到作业: {exam_name}",
                    run_fn=lambda r=item_recruit, c=item_course, e=exam_id, s=stu_exam_id, sc=item_school: (
                        hw_worker.run(
                            recruit_id=r,
                            course_id=c,
                            homework_id=e,
                            stu_exam_id=s,
                            exam_id=e,
                            school_id=str(sc or ""),
                        )
                    ),
                    check_locked=False,
                    include_score=True,
                )
        logger.event(
            "知到作业汇总",
            发现=hw_summary["found"],
            完成=hw_summary["done"],
            跳过=hw_summary["skipped"],
            失败=hw_summary["failed"],
        )

    if do_exam:
        logger.section("知到考试")
        exam_api = ZhidaoExamApi(client)
        exam_worker = ZhidaoExamWorker(
            exam_api,
            course_id=course_id,
            course_name=course_name,
            recruit_id=recruit_id,
            school_id=school_id,
            store=store,
            auto_submit=auto_submit,
            cancel_event=cancel_event,
            fp_provider=fp_provider,
            enable_http_captcha=enable_http_captcha,
        )
        resources.append(exam_worker)
        try:
            exams = await asyncio.to_thread(
                exam_worker.scan, 1, recruit_id=recruit_id, course_id=course_id
            )
        except Exception as exc:
            logger.warn(f"扫描知到正式考试失败: {exc}")
            exams = []

        exam_summary = summary["exam"]
        exam_summary["found"] = len(exams)
        if not exams:
            logger.info("当前课程暂无未完成的正式考试.")
        else:
            total_ex = len(exams)
            for idx, exam in enumerate(exams, 1):
                exam_id = exam.get("examId", "")
                stu_exam_id = exam.get("studentExamId", "")
                exam_name = exam.get("examName") or exam_id or f"考试 {idx}"
                item_recruit = exam.get("recruitId") or recruit_id
                item_course = exam.get("courseId") or course_id
                item_school = exam.get("schoolId") or school_id
                raw = exam.get("raw") if isinstance(exam.get("raw"), dict) else {}
                in_window, reason = check_time_window(
                    None, exam.get("endDate") or raw.get("endDate")
                )
                if not in_window:
                    logger.info(f"[{idx}/{total_ex}] 考试「{exam_name}」已截止, 跳过.")
                    exam_summary["skipped"] += 1
                    exam_summary["details"].append(
                        {"name": exam_name, "skipped": True, "reason": reason}
                    )
                    continue
                if not is_exam_started(exam):
                    start_hint = exam.get("startTime") or raw.get("startTime") or ""
                    time_note = f" (开放时间: {start_hint})" if start_hint else ""
                    logger.info(
                        f"[{idx}/{total_ex}] 正式考试「{exam_name}」尚未开始{time_note}, "
                        "跳过 (请在开放后手动进入考试再运行)."
                    )
                    exam_summary["skipped"] += 1
                    exam_summary["details"].append(
                        {"name": exam_name, "skipped": True, "reason": "not_started"}
                    )
                    continue

                exam_url = build_exam_page_url(
                    "doexam",
                    recruit_id=item_recruit,
                    stu_exam_id=stu_exam_id,
                    exam_id=exam_id,
                    course_id=item_course,
                    school_id=item_school,
                )
                def _run_exam(
                    r=item_recruit,
                    c=item_course,
                    e=exam_id,
                    s=stu_exam_id,
                    sc=item_school,
                    n=exam_name,
                ):
                    return exam_worker.run(
                        recruit_id=r,
                        course_id=c,
                        exam_id=e,
                        student_exam_id=s,
                        school_id=str(sc or ""),
                        exam_name=n,
                    )

                await _execute_assessment_item(
                    page=page,
                    client=client,
                    config=config,
                    logger=logger,
                    cancel_event=cancel_event,
                    summary_bucket=exam_summary,
                    idx=idx,
                    total_count=total_ex,
                    item_kind="考试",
                    event_name="知到考试",
                    item_name=exam_name,
                    item_url=exam_url,
                    start_log=f"[{idx}/{total_ex}] 接管进行中的正式考试: {exam_name}",
                    run_fn=_run_exam,
                    check_locked=True,
                    include_score=False,
                )
        logger.event(
            "知到考试汇总",
            发现=exam_summary["found"],
            完成=exam_summary["done"],
            跳过=exam_summary["skipped"],
            失败=exam_summary["failed"],
        )

    return summary
