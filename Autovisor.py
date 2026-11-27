# encoding=utf-8
import argparse
import asyncio
import os
import sys
import threading

from modules.stdio import configure_stdio

configure_stdio()

from playwright.async_api import TimeoutError, async_playwright

from modules import homework  # noqa: F401
from modules import installer, quiz_page, updater
from modules.ai_smart import (  # noqa: F401
    complete_resources_for_node,
    is_ai_smart_url,
    run_ai_smart_course,
)
from modules.ai_smart_mastery import (  # noqa: F401
    AnswerCache,
    build_learn_url,
    enumerate_nodes,
    run_course_mastery,
)
from modules.answer_store import AiQuestionStore
from modules.auth_flow import (  # noqa: F401
    COOKIE_SYNC_INTERVAL_SECONDS,
    _cookies_signature,
    auto_login,
    ensure_credentials,
    ensure_login,
    maybe_qr_login,
    periodic_cookie_sync,
    persist_login_cookies,
    remember_login_cookies,
    report_login_error_later,
    save_credentials_to_config,
)
from modules.banner import print_banner
from modules.browser import BrowserSession, create_browser_session, get_screen_size  # noqa: F401
from modules.configs import Config, ConfigError, ensure_config_files
from modules.course_fetcher import (
    append_courses_to_config,
    fetch_courses_from_page,
    fetch_courses_via_api,
    save_fetched_courses,
)
from modules.course_runner import (
    CourseOutcome,
    detect_catalog_after_verification,
    run_course,
)
from modules.diagnostics import check_browser, check_course
from modules.hike_navigation import (
    complete_hike_document_nodes,
    is_hike_404_page,
    needs_hike_file_id_resolution,
    parse_hike_course_url,
    resolve_hike_playback_url,
)
from modules.exam_ctx import AiFormalExamCtx, ExamApi, ExamRunner  # noqa: F401
from modules.logger import Logger
from modules.notifier import Notifier
from modules.popups import dismiss_popups
from modules.quiz_dom import TargetClosedError
from modules.runtime_bootstrap import (  # noqa: F401
    RunContext,
    build_page_exam_api,
    build_page_zhs_client,
    log_runtime_config,
    log_runtime_environment,
    merge_playwright_cookies as _merge_playwright_cookies,
    resolve_cancel_event as _resolve_cancel_event,
    sync_zhs_client_cookies,
)
from modules.stages.ai_smart_stage import (  # noqa: F401
    _DIRECT_TASK_KINDS,
    _formal_exam_succeeded,
    course_route,
    run_exam_stage,
    run_formal_exam_route,
    run_formal_exam_stage,
    run_formal_exam_tasks,
    run_learn_page_route,
    run_mastery_stage,
    run_resources_stage,
    run_task_url,
)
from modules.stages.zhidao_assessment import (  # noqa: F401
    _run_vision_fallback_on_url,
    _run_zhidao_with_captcha,
    resolve_zhidao_course_meta,
    run_zhidao_assessment_stage,
)
from modules.tasks import (
    play_video,
    skip_questions,
    task_monitor,
    video_optimize,
    wait_for_verify,
)
from modules.url_router import parse_url
from modules.utils import (
    clear_cookies,
    display_window,
    get_runtime_path,
    hide_window,
    load_cookies,
    optimize_page,
    save_cookies,
    should_hide_window,
)

# 导入即就绪的默认日志器与默认 Cookie 路径（单轮运行期间由 RunContext 隔离）
logger = Logger()
COOKIE_PATH = get_runtime_path("data", "cookies.json")


async def run_fetch_courses(config, write_config: bool = False) -> int:
    """登录并自动拉取账号下所有课程列表，导出到 data/courses.json（可选写入 config.ini）。"""
    logger.section("拉取账号课程列表")
    ctx = RunContext(cookie_path=COOKIE_PATH)
    attach = bool(getattr(config, "attach_existing_chrome", False))
    cookies = None if attach else load_cookies(ctx.cookie_path)
    remember_login_cookies(cookies, ctx=ctx)
    if not attach:
        cookies = await maybe_qr_login(config, cookies, ctx=ctx)
    proxy = str(getattr(config, "proxy", "") or "").strip()

    courses = []
    if not attach and cookies:
        logger.info("检测到本地登录凭证，优先通过智慧树课程接口直拉课程列表...")
        try:
            api_res = await asyncio.to_thread(
                fetch_courses_via_api,
                cookies,
                proxy=proxy,
                logger=logger,
            )
            if api_res.session_valid and api_res.courses:
                courses = list(api_res.courses)
                if api_res.cookies:
                    merged_cookies = _merge_playwright_cookies(cookies, api_res.cookies)
                    try:
                        save_cookies(merged_cookies, ctx.cookie_path)
                        remember_login_cookies(merged_cookies, ctx=ctx)
                    except Exception as exc:
                        logger.log_exception("写回刷新后的登录 Cookies 失败.", exc)
        except Exception as exc:
            logger.debug(f"接口直拉课程失败，回退浏览器流程: {exc}")

    if not courses:
        async with async_playwright() as p:
            session = await create_browser_session(p, config, cookies, logger)
            try:
                page, context = session.page, session.context
                await ensure_login(context, page, cookies, config, ctx=ctx)
                logger.info("正在从智慧树接口与学生主页读取课程列表...")
                courses = await fetch_courses_from_page(
                    page,
                    logger=logger,
                    proxy=proxy,
                )
                await persist_login_cookies(context, ctx=ctx)
            finally:
                await session.close()

    if not courses:
        logger.warn("未能从当前账号主页识别到可用课程链接.", shift=True)
        return 1

    output_path = get_runtime_path("data", "courses.json")
    save_fetched_courses(courses, output_path)
    logger.info(f"共拉取到 {len(courses)} 门课程，已导出至: {output_path}", shift=True)
    for idx, course in enumerate(courses, 1):
        meta = " / ".join(part for part in (course.course_type, course.teacher, course.progress) if part)
        logger.info(f"  [{idx}] {course.title} ({meta}) -> {course.url}")

    if write_config and config.config_path:
        added = append_courses_to_config(config.config_path, courses)
        logger.info(
            f"已将 {added} 条新课程链接写入配置文件: {config.config_path}",
            shift=True,
        )
    return 0


async def main(config, cancel_event: threading.Event | None = None) -> bool:
    notifier = Notifier.from_config(config, logger=logger)
    try:
        return await _main_impl(config, notifier, cancel_event=cancel_event)
    finally:
        close_fn = getattr(notifier, "close", None)
        if callable(close_fn):
            close_fn()


async def _main_impl(
    config, notifier, cancel_event: threading.Event | None = None
) -> bool:
    global COOKIE_PATH
    if cancel_event is not None:
        config._cancel_event = cancel_event
    COOKIE_PATH = get_runtime_path("data", "cookies.json")
    ctx = RunContext(
        cookie_path=COOKIE_PATH,
        cancel_event=_resolve_cancel_event(config),
    )
    quiz_page.llm.reset_cache()
    quiz_page.reset_zhidao_cooldown()
    quiz_page.llm.set_proxy(getattr(config, "proxy", ""))
    modules, tasks = [], []
    all_courses_complete = True
    run_ok = True
    if config.enableAutoCaptcha:
        logger.section("依赖检查")
        logger.info("正在检查验证码识别依赖...")
        modules = installer.start(config)
        logger.info("验证码识别依赖已就绪!")

    logger.section("登录")
    async with async_playwright() as p:
        cookies = None if getattr(config, "attach_existing_chrome", False) else load_cookies(ctx.cookie_path)
        remember_login_cookies(cookies, ctx=ctx)
        if not getattr(config, "attach_existing_chrome", False):
            cookies = await maybe_qr_login(config, cookies, ctx=ctx)
        logger.event("本地凭证", 数量=len(cookies) if cookies else 0, 文件=ctx.cookie_path)
        session = await create_browser_session(p, config, cookies, logger)
        monitor_task = None
        context = None
        quiz_tasks: set[asyncio.Task] = set()
        _on_new_page = None
        try:
            page, context = session.page, session.context

            # 捕获课程流程中新开的测验页面(如课堂测验跳转的新标签页), 自动作答
            def _on_new_page(new_page):
                task = asyncio.create_task(quiz_page.auto_answer_new_page(new_page, config))
                quiz_tasks.add(task)
                task.add_done_callback(quiz_tasks.discard)

            context.on("page", _on_new_page)
            if getattr(config, "enableZhidaoQuiz", True) and quiz_page.llm.enabled():
                quiz_capture_status = f"已启用 (智慧树AI -> {quiz_page.llm.provider_label()})"
            elif getattr(config, "enableZhidaoQuiz", True):
                quiz_capture_status = "已启用 (智慧树AI)"
            elif quiz_page.llm.enabled():
                quiz_capture_status = f"已启用 ({quiz_page.llm.provider_label()})"
            elif quiz_page.llm.vision_enabled():
                quiz_capture_status = "已启用 (仅作业/考试图片题视觉模型)"
            else:
                quiz_capture_status = "未配置答题源, 仅监听不答题"
            if quiz_page.llm.vision_enabled():
                quiz_capture_status += f" ; 图片题={quiz_page.llm.vision_label()}"
            else:
                quiz_capture_status += " ; 图片题=未配置视觉模型"
            logger.event("测验页面捕获", 状态=quiz_capture_status)
            login_by_cookie = await ensure_login(context, page, cookies, config, modules, ctx=ctx)

            logger.context(登录方式="Cookie" if login_by_cookie else "账号")
            if should_hide_window(config):
                await hide_window(page)
            elif getattr(config, "enableHideWindow", False) and getattr(
                config, "attach_existing_chrome", False
            ):
                logger.info("已连接现有 Chrome，跳过隐藏浏览器窗口以免影响正常使用.")
            tasks.extend(
                [
                    asyncio.create_task(
                        wait_for_verify(
                            page, config, ctx.verify_event, modules, notifier=notifier
                        )
                    ),
                    asyncio.create_task(video_optimize(page, config, ctx.playback_enabled)),
                    asyncio.create_task(
                        skip_questions(page, ctx.answer_event, ctx.playback_enabled, config=config)
                    ),
                    asyncio.create_task(play_video(page, ctx.playback_enabled)),
                    asyncio.create_task(dismiss_popups(page, ctx.popups_enabled)),
                    asyncio.create_task(periodic_cookie_sync(context, ctx=ctx)),
                ]
            )
            logger.event(
                "后台任务启动",
                任务=", ".join(task.get_coro().__name__ for task in tasks),
            )
            monitor_task = asyncio.create_task(task_monitor(tasks))

            course_total = len(config.course_urls)
            for index, course_url in enumerate(config.course_urls, 1):
                logger.section(f"课程 {index}/{course_total}")
                logger.context(
                    课程序号=f"{index}/{course_total}",
                    课程索引=index,
                    课程总数=course_total,
                    课程地址=course_url,
                )
                if needs_hike_file_id_resolution(course_url):
                    course_url = await resolve_hike_playback_url(
                        page,
                        course_url,
                        config=config,
                        logger=logger,
                    )
                    logger.context(课程地址=course_url)
                if "login" not in str(getattr(page, "url", "") or "").lower():
                    logger.info("正在加载播放页...")
                    try:
                        await page.goto(course_url, wait_until="domcontentloaded", timeout=30000)
                    except TimeoutError:
                        logger.warn("播放页加载较慢，继续等待目录渲染...", shift=True)
                    await page.wait_for_timeout(2000)
                if "login" in page.url:
                    logger.warn(
                        "播放页跳转到登录页, 当前登录状态已失效, 正在重新登录.",
                        shift=True,
                    )
                    logger.event("登录失效", 地址=page.url)
                    if not config.username or not config.password:
                        await notifier.notify_verify(
                            f"登录状态已失效且未配置账号密码，需要手动重新登录。\n课程：{course_url}"
                        )
                    if should_hide_window(config):
                        await display_window(page)
                    clear_cookies(ctx.cookie_path)
                    remember_login_cookies(None, ctx=ctx)
                    await ensure_login(context, page, None, config, modules, ctx=ctx)
                    if should_hide_window(config):
                        await hide_window(page)
                    if needs_hike_file_id_resolution(course_url):
                        course_url = await resolve_hike_playback_url(
                            page,
                            course_url,
                            config=config,
                            logger=logger,
                        )
                        logger.context(课程地址=course_url)
                    logger.info("重新进入播放页...")
                    try:
                        await page.goto(course_url, wait_until="domcontentloaded", timeout=30000)
                    except TimeoutError:
                        logger.warn("播放页加载较慢，继续等待目录渲染...", shift=True)
                    await page.wait_for_timeout(2000)
                if parse_hike_course_url(course_url) and await is_hike_404_page(page):
                    logger.warn(
                        "检测到翻转/智慧课播放页 404，正在重新定位有效视频地址...",
                        shift=True,
                    )
                    recovered_url = await resolve_hike_playback_url(
                        page,
                        course_url,
                        config=config,
                        logger=logger,
                        force=True,
                    )
                    if recovered_url and recovered_url != course_url:
                        course_url = recovered_url
                        logger.context(课程地址=course_url)
                        try:
                            await page.goto(
                                course_url, wait_until="domcontentloaded", timeout=30000
                            )
                        except TimeoutError:
                            logger.warn("播放页加载较慢，继续等待目录渲染...", shift=True)
                        await page.wait_for_timeout(2000)

                parsed = parse_url(course_url)
                route = course_route(parsed, course_url)
                if route == "task":
                    kind = parsed.get("kind", "")
                    logger.info(f"检测到 {kind} 任务链接, 走精准路由.")
                    logger.event("任务链接", 类型=kind, 地址=course_url)
                    await persist_login_cookies(context, ctx=ctx)
                    ctx.playback_enabled.clear()
                    try:
                        await run_task_url(
                            page, course_url, parsed, config, logger, ctx.playback_enabled
                        )
                    except Exception as exc:
                        logger.log_exception("任务链接处理失败.", exc)
                        run_ok = False
                        all_courses_complete = False
                        break
                    continue

                if route == "ai_smart":
                    logger.info("检测到 ai-smart 新形态课程.")
                    logger.context(目录类型="ai-smart")
                    logger.event("课程目录", 类型="ai-smart", 地址=page.url)
                    await persist_login_cookies(context, ctx=ctx)
                    ctx.playback_enabled.clear()
                    outcome = await run_ai_smart_course(
                        page, course_url, config, logger, ctx.playback_enabled
                    )
                    ctx.playback_enabled.clear()
                    logger.event(
                        "课程结果", 结果=outcome.value, 目录类型="ai-smart"
                    )
                    if outcome is CourseOutcome.FAILED:
                        logger.warn("课程未确认完成, 已停止本轮运行.", shift=True)
                        run_ok = False
                        all_courses_complete = False
                        break
                    if outcome is CourseOutcome.TIME_LIMIT:
                        all_courses_complete = False
                        continue
                    if getattr(config, "reviewMode", False) is True:
                        logger.info("复习模式下跳过资源补齐、接口考试与掌握度刷题阶段.")
                        continue
                    # 大纲接口会漏掉「绪论」这类 default 单元, 再按知识点补一遍资源
                    try:
                        await run_resources_stage(
                            page, course_url, config, logger, ctx.playback_enabled
                        )
                    except Exception as exc:
                        logger.log_exception("必学资源补齐失败.", exc)
                    # 视频刷完后先用接口版 ExamCtx 刷计分考试, 综合考试按开关独立调度
                    if getattr(config, "enableExamApi", True) or getattr(
                        config, "enableFormalExamApi", True
                    ):
                        try:
                            await run_exam_stage(page, course_url, config, logger)
                        except Exception as exc:
                            logger.log_exception("接口考试阶段失败.", exc)
                    # 视频刷完后继续刷知识点掌握度(题目)
                    if getattr(config, "enableMastery", True):
                        ctx.popups_enabled.clear()
                        try:
                            await run_mastery_stage(
                                page, course_url, config, logger
                            )
                        except Exception as exc:
                            logger.log_exception("掌握度刷取失败.", exc)
                            run_ok = False
                            all_courses_complete = False
                            break
                        finally:
                            ctx.popups_enabled.set()
                    continue

                catalog = await detect_catalog_after_verification(page, page.url)
                logger.info(f"检测到 {catalog.name} 课程目录.")
                logger.context(目录类型=catalog.name)
                logger.event("课程目录", 类型=catalog.name, 地址=page.url)
                await persist_login_cookies(context, ctx=ctx)
                await optimize_page(page, config, catalog)
                logger.info("页面优化完成!")
                if catalog.course_title:
                    title_element = page.locator(catalog.course_title).first
                    if await title_element.count():
                        title = " ".join(
                            (await title_element.text_content() or "").split()
                        )
                        if title:
                            logger.info(f"当前课程:<<{title}>>")
                            logger.context(课程名称=title)
                            logger.event("当前课程", 名称=title)

                ctx.playback_enabled.clear()
                outcome = await run_course(
                    page, catalog, config, logger, ctx.playback_enabled
                )
                ctx.playback_enabled.clear()
                logger.event("课程结果", 结果=outcome.value, 目录类型=catalog.name)
                if outcome is CourseOutcome.FAILED:
                    logger.warn("课程未确认完成,已停止本轮运行.", shift=True)
                    run_ok = False
                    all_courses_complete = False
                    break
                if outcome is CourseOutcome.TIME_LIMIT:
                    all_courses_complete = False
                    continue
                if getattr(config, "reviewMode", False) is True:
                    logger.info("复习模式下跳过知到作业与正式考试阶段.")
                    continue
                if (
                    catalog.name == "hike"
                    or parse_hike_course_url(course_url)
                    or parse_hike_course_url(getattr(page, "url", ""))
                ):
                    try:
                        await complete_hike_document_nodes(
                            page, course_url, config=config, logger=logger
                        )
                    except Exception as exc:
                        logger.log_exception("Hike 非视频文档标记已读失败.", exc)
                assessment_parsed = parsed
                if not assessment_parsed.get("recruitAndCourseId"):
                    assessment_parsed = parse_url(getattr(page, "url", ""))
                if assessment_parsed.get("recruitAndCourseId") and (
                    getattr(config, "enableHomeworkApi", True)
                    or getattr(config, "enableFormalExamApi", True)
                ):
                    ctx.popups_enabled.clear()
                    try:
                        await run_zhidao_assessment_stage(
                            page, course_url, assessment_parsed, config, logger
                        )
                    except Exception as exc:
                        logger.log_exception("知到作业/考试阶段失败.", exc)
                    finally:
                        ctx.popups_enabled.set()
        finally:
            if context is not None and _on_new_page is not None:
                remove_listener = getattr(context, "remove_listener", None)
                if callable(remove_listener):
                    try:
                        remove_listener("page", _on_new_page)
                    except Exception:
                        pass
            pending_quiz_tasks = list(quiz_tasks)
            for qtask in pending_quiz_tasks:
                qtask.cancel()
            for task in tasks:
                task.cancel()
            if monitor_task:
                monitor_task.cancel()
            await asyncio.gather(*tasks, *pending_quiz_tasks, return_exceptions=True)
            if monitor_task:
                await asyncio.gather(monitor_task, return_exceptions=True)
            if context is not None:
                try:
                    await persist_login_cookies(context, ctx=ctx)
                except Exception as exc:
                    logger.log_exception("刷新登录 Cookies 失败.", exc)
            await session.close()

    logger.section("任务结束")
    logger.event(
        "运行结果",
        正常退出=run_ok,
        全部完成=all_courses_complete,
    )
    logger.clear_context()
    if not run_ok:
        logger.warn("本轮因课时进度未确认而停止.", shift=True)
        await notifier.notify_error("本轮因课时进度未确认而停止，请查看运行日志。")
        return False
    is_review = getattr(config, "reviewMode", False) is True
    if all_courses_complete:
        if is_review:
            logger.info("所有课程已复习完毕!")
            await notifier.notify_finish(f"所有课程（共 {len(config.course_urls)} 门）已复习完毕！")
        else:
            logger.info("所有课程已学习完毕!")
            await notifier.notify_finish(f"所有课程（共 {len(config.course_urls)} 门）已学习完毕！")
    else:
        logger.event("达到课程时限", 时限分钟=config.limitMaxTime, 复习模式=is_review)
        if is_review:
            logger.info("本轮已按每门课程时限结束,复习任务已完成.", shift=True)
            await notifier.notify_finish(
                f"本轮复习已达到每门课程时长上限（{config.limitMaxTime:g} 分钟），习惯分任务已完成。"
            )
        else:
            logger.info("本轮已按每门课程时限结束,仍有课程未完成.", shift=True)
            await notifier.notify_finish(
                f"本轮已达到每门课程时长上限（{config.limitMaxTime:g} 分钟），仍有课程未学完。"
            )
    return True


def parse_args():
    parser = argparse.ArgumentParser(description="Autovisor")
    parser.add_argument("--config", default=None, help="配置文件路径(默认使用程序目录下的 config.ini)")
    parser.add_argument(
        "-c",
        "--course",
        nargs="+",
        metavar="URL",
        help="临时指定本轮要学习的课程播放页链接(覆盖 config.ini 中的 [course-url])",
    )
    parser.add_argument(
        "-s",
        "--speed",
        type=float,
        default=None,
        help="临时覆盖视频播放倍速(0.5–1.8)",
    )
    parser.add_argument(
        "-l",
        "--limit",
        type=float,
        default=None,
        help="临时覆盖每门课程刷课时长限制/分钟(0 表示不限制)",
    )
    parser.add_argument(
        "-r",
        "--review",
        action="store_true",
        help="启用复习模式(刷习惯分)：忽略已完成状态重播视频，设置时长上限时循环刷满",
    )
    parser.add_argument(
        "--proxy",
        default=None,
        metavar="URL",
        help="临时覆盖代理服务器地址(如 http://127.0.0.1:7890 或 socks5://127.0.0.1:1080)",
    )
    parser.add_argument(
        "--hide-window",
        action="store_true",
        help="临时开启隐藏浏览器窗口后台刷课",
    )
    parser.add_argument(
        "-q",
        "--qr-login",
        action="store_true",
        help="无有效 Cookie 时使用终端二维码扫码登录",
    )
    parser.add_argument(
        "--qr-terminal",
        action="store_true",
        help="在终端打印登录二维码(自动启用 --qr-login)",
    )
    parser.add_argument(
        "--qr-unicode",
        action="store_true",
        help="使用 Unicode 半块字符打印二维码(兼容无 ANSI 颜色终端)",
    )
    parser.add_argument(
        "--qr-save-dir",
        default=None,
        metavar="DIR",
        help="将登录二维码 PNG 保存到指定目录",
    )
    parser.add_argument(
        "-f",
        "--fetch-courses",
        action="store_true",
        help="登录后自动拉取账号下所有课程清单并导出至 data/courses.json",
    )
    parser.add_argument(
        "--write-config",
        action="store_true",
        help="配合 --fetch-courses 使用，将拉取到的新课程链接自动追加到 config.ini",
    )
    parser.add_argument(
        "--test-notify",
        action="store_true",
        help="测试消息推送通知配置是否可用（Windows / ServerChan / Qmsg / Bark / Telegram）",
    )
    parser.add_argument(
        "-d",
        "--debug",
        action="store_true",
        help="在终端同步输出 DEBUG 级调试日志",
    )
    parser.add_argument(
        "--check-browser",
        action="store_true",
        help="只检查 Chrome 启动和智慧树登录状态",
    )
    parser.add_argument(
        "--check-course",
        metavar="URL",
        help="阻止进度上报和自动播放,只检查课程目录选择器",
    )
    parser.add_argument(
        "--import-cookies",
        metavar="PATH",
        help="从 Requests CookieJar JSON 安全导入未过期的智慧树 Cookie",
    )
    parser.add_argument(
        "--clear-cache",
        action="store_true",
        help="清理本地题库缓存（SQLite 与兼容 JSON 快照）",
    )
    return parser.parse_args()


def cli() -> int:
    global logger
    args = parse_args()
    print_banner()
    logger = Logger()
    if args.debug:
        logger.console_debug = True
    exit_code = 0
    notifier = None
    try:
        logger.section("初始化")
        logger.info("程序启动中...")
        log_runtime_environment(
            logger,
            run_mode="打包" if getattr(sys, "frozen", False) else "源码",
            include_argv=True,
        )
        installer.validate_python_version()
        base_dir = (
            os.path.dirname(sys.executable)
            if getattr(sys, "frozen", False)
            else os.path.dirname(os.path.abspath(__file__))
        )
        default_config_name = "config.macos.ini" if sys.platform == "darwin" else "config.ini"
        config_path = args.config or os.path.join(base_dir, default_config_name)
        mirrors_path = os.path.join(base_dir, "data", "mirrors.json")
        # 启动即补齐缺失的配置文件(只创建不覆盖), 个人配置不会被提交到仓库
        created_files = ensure_config_files(config_path)
        for path in created_files:
            logger.info(f"已生成默认配置文件: {path}", shift=True)
        if config_path in created_files and not (
            args.course
            or args.fetch_courses
            or args.test_notify
            or args.import_cookies
            or args.clear_cache
        ):
            logger.warn("配置文件为默认模板, 请填写课程链接([course-url])后重新运行.", shift=True)
            return 0
        config = Config(config_path, mirrors_path)
        logger.configure_from_config(config)
        config.apply_cli_overrides(args)
        notifier = Notifier.from_config(config, logger=logger)
        updater.check_for_update(logger, proxy=config.proxy)
        logger.context(配置文件=config_path)
        quiz_page.llm.reset_cache()
        quiz_page.llm.set_proxy(getattr(config, "proxy", ""))
        log_runtime_config(
            logger,
            config,
            configured_channels=",".join(notifier.configured_channels()) or "未配置",
            llm_module=quiz_page.llm,
        )
        if args.test_notify:
            if not notifier.enabled:
                logger.warn("未配置可用的通知服务，请先在 [notification] 段填写 provider 及对应参数。", shift=True)
                return 2
            results = notifier.send(
                "Autovisor GUI 推送测试",
                "这是一条来自 Autovisor GUI 的测试通知，推送渠道工作正常。",
            )
            all_ok = all(ok for ok, _ in results.values())
            for channel, (ok, detail) in results.items():
                if ok:
                    logger.info(f"推送测试成功 ({channel}): {detail}", shift=True)
                else:
                    logger.error(f"推送测试失败 ({channel}): {detail}", shift=True)
            return 0 if all_ok else 1
        if args.import_cookies:
            from modules.utils import import_zhihuishu_cookies

            count = import_zhihuishu_cookies(args.import_cookies, COOKIE_PATH)
            logger.info(f"已安全导入 {count} 条智慧树 Cookie.", shift=True)
            return 0
        if args.clear_cache:
            counts = AiQuestionStore().clear()
            logger.info(
                f"已清理本地题库缓存: 知到题 {counts['zhidao_questions']} 条, "
                f"AI 题 {counts['ai_questions']} 条, 快照文件 {counts['json_files']} 个.",
                shift=True,
            )
            return 0
        if args.fetch_courses:
            return asyncio.run(run_fetch_courses(config, write_config=args.write_config))
        if args.check_browser:
            return asyncio.run(check_browser(config, logger, COOKIE_PATH))
        if args.check_course:
            return asyncio.run(
                check_course(args.check_course, config, logger, COOKIE_PATH)
            )
        if not config.course_urls:
            logger.error("未检测到有效网址或不支持此类网页,请检查配置文件! (可用 --fetch-courses 自动拉取课程列表)")
            return 2
        if not config.qr_login:
            ensure_credentials(config)
        if not asyncio.run(main(config)):
            exit_code = 1
        elif config.shutdownOnComplete:
            from modules.shutdown import ShutdownManager

            mgr = ShutdownManager()
            try:
                mgr.schedule(1)
                logger.warn(
                    f"学习任务已结束，系统将在 {mgr.delay} 秒后自动关机（Windows 可运行 shutdown /a 取消）。",
                    shift=True,
                )
            except Exception as exc:
                logger.error(f"自动关机启动失败: {exc}", shift=True)
    except TargetClosedError as exc:
        if "BrowserType.launch" in repr(exc):
            logger.log_exception("浏览器相关流程异常结束.", exc)
            logger.error("浏览器启动失败,请检查 Chrome 或 CDP 配置!")
        else:
            logger.debug(f"浏览器关闭结束运行: {logger.summarize_exception(exc)}")
        exit_code = 1
    except ConfigError as exc:
        logger.error(f"配置文件无效: {exc}", shift=True)
        logger.info("请完整解压发行包，并确保 config.ini 与 Autovisor.exe 位于同一目录。")
        exit_code = 1
    except Exception as exc:
        logger.log_exception("程序运行时出现未处理异常.", exc, shift=True)
        if isinstance(exc, KeyError):
            logger.error("配置文件错误!")
        elif isinstance(exc, FileNotFoundError):
            logger.error(f"依赖文件缺失: {exc.filename},请重新安装程序!")
        elif isinstance(exc, UnicodeDecodeError):
            logger.error("配置文件编码错误,保存时请选择UTF-8或GBK编码!")
        else:
            logger.error("系统出错,请检查后重新启动!")
        if notifier is not None:
            notifier.notify_error_sync(f"程序运行时出现异常：{type(exc).__name__}: {exc}")
        exit_code = 1
    finally:
        if notifier is not None:
            close_fn = getattr(notifier, "close", None)
            if callable(close_fn):
                close_fn()
        logger.save()
        if getattr(sys, "frozen", False) and sys.stdin.isatty():
            try:
                input("程序已结束,按Enter退出...")
            except EOFError:
                # 非交互式运行(重定向/自动化)时 stdin 可能已关闭, 不应视作异常
                pass
    return exit_code


if __name__ == "__main__":
    sys.exit(cli())
