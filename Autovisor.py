# encoding=utf-8
import argparse
import asyncio
import ctypes
import getpass
import os
import platform
import re
import sys
import time
from pathlib import Path

from modules.stdio import configure_stdio

configure_stdio()

from playwright.async_api import BrowserContext, Page, Playwright, TimeoutError, async_playwright
from playwright._impl._errors import TargetClosedError

from modules import homework, installer, quiz_page, updater
from modules.ai_smart import (
    complete_resources_for_node,
    is_ai_smart_url,
    run_ai_smart_course,
)
from modules.ai_smart_mastery import (
    AnswerCache,
    build_learn_url,
    enumerate_nodes,
    run_course_mastery,
)
from modules.answer_store import AiQuestionStore
from modules.banner import print_banner
from modules.browser import BrowserSession, create_browser_session, get_screen_size
from modules.configs import Config, ConfigError, ensure_config_files
from modules.course_fetcher import (
    append_courses_to_config,
    fetch_courses_from_page,
    save_fetched_courses,
)
from modules.course_runner import (
    CourseOutcome,
    detect_catalog_after_verification,
    run_course,
)
from modules.diagnostics import check_browser, check_course
from modules.exam_ctx import AiFormalExamCtx, ExamApi, ExamRunner
from modules.http_utils import build_playwright_proxy
from modules.login import (
    describe_login_error,
    is_login_page,
    perform_password_login,
    wait_for_login_complete,
)
from modules.logger import Logger
from modules.notifier import Notifier
from modules.popups import dismiss_popups
from modules.qr_login import qr_login_for_cookies
from modules.slider import captcha_visible, slider_verify
from modules.tasks import (
    play_video,
    skip_questions,
    task_monitor,
    video_optimize,
    wait_for_verify,
)
from modules.url_router import (
    KIND_EXAM,
    KIND_HOMEWORK,
    KIND_KNOWLEDGE_STUDY,
    KIND_LEARN_PAGE,
    KIND_TEST_DETAIL,
    parse_url,
)
from modules.utils import (
    clear_cookies,
    display_window,
    filter_zhihuishu_cookies,
    get_resource_path,
    get_runtime_path,
    hide_window,
    load_cookies,
    optimize_page,
    save_cookies,
    should_hide_window,
)
from modules.version import __version__

# 获取全局事件循环与单例日志器（导入即就绪，兼容内置后端直接调用）
logger = Logger()
event_loop_verify = asyncio.Event()
event_loop_answer = asyncio.Event()
COOKIE_PATH = get_runtime_path("data", "cookies.json")
ZHS_COOKIE_URLS = [
    "https://www.zhihuishu.com",
    "https://passport.zhihuishu.com",
    "https://onlineweb.zhihuishu.com",
    "https://studyvideoh5.zhihuishu.com",
    "https://studywisdomh5.zhihuishu.com",
    "https://fusioncourseh5.zhihuishu.com",
    "https://wisdom-mooc.zhihuishu.com",
    "https://hike.zhihuishu.com",
    "https://ai-smart-course-student-pro.zhihuishu.com",
]
# 运行期间周期性落盘 Cookies 的间隔(秒): 服务器中途续期后, 崩溃/断电也不丢凭证
COOKIE_SYNC_INTERVAL_SECONDS = 300


_saved_cookies = None


def _cookies_signature(cookies):
    if not cookies:
        return None
    return frozenset(
        (
            cookie.get("name"),
            cookie.get("domain"),
            cookie.get("path"),
            cookie.get("value"),
        )
        for cookie in cookies
    )


def remember_login_cookies(cookies) -> None:
    """记录已经落盘的凭证, 用于跳过重复写入。"""
    global _saved_cookies
    _saved_cookies = _cookies_signature(cookies)


async def persist_login_cookies(context: BrowserContext) -> None:
    """凭证有变化时才写盘: 登录完成、Cookie 续期后立即保存, 中断也不丢。"""
    global _saved_cookies
    # 按域名筛选而非固定 URL 列表, 新子域下发 cookies 也能收进来
    cookies = filter_zhihuishu_cookies(await context.cookies())
    signature = _cookies_signature(cookies)
    if signature is None or signature == _saved_cookies:
        return
    try:
        save_cookies(cookies, COOKIE_PATH)
    except Exception as exc:
        logger.log_exception("保存登录 Cookies 失败.", exc)
        return
    _saved_cookies = signature
    logger.event("保存登录凭证", 条数=len(cookies), 文件=COOKIE_PATH)


async def periodic_cookie_sync(context: BrowserContext) -> None:
    """运行期间周期性落盘 Cookies, 与 persist_login_cookies 共用变更检测, 无变化时不写盘。"""
    while True:
        await asyncio.sleep(COOKIE_SYNC_INTERVAL_SECONDS)
        try:
            await persist_login_cookies(context)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.log_exception("周期保存登录 Cookies 失败.", exc)


async def init_page(p: Playwright, config, cookies) -> BrowserSession:
    return await create_browser_session(p, config, cookies, logger)


async def report_login_error_later(page: Page) -> None:
    """提交账号密码后若仍停留在登录页, 收集并输出页面错误提示。"""
    await page.wait_for_timeout(6000)
    if not is_login_page(page.url):
        return
    if await captcha_visible(page):
        # 验证码还开着时提示"登录未完成"会误导, 交给滑块任务继续处理。
        return
    detail = await describe_login_error(page)
    if detail:
        logger.warn(f"登录未完成, 页面提示: {detail}", shift=True)
    else:
        logger.warn("登录未完成, 请检查账号密码是否正确, 或在浏览器中手动登录.", shift=True)


async def auto_login(context: BrowserContext, page: Page, config, modules=None) -> None:
    wait_start = time.time()
    if not is_login_page(page.url):
        await page.goto(config.login_url, wait_until="domcontentloaded")
    if not is_login_page(page.url):
        logger.info("检测到已登录,跳过登录步骤.")
        return

    error_task = None
    if config.username and config.password:
        try:
            logger.event("自动登录", 方式="账号密码", 账号="已填写")
            await perform_password_login(page, config.username, config.password)
            logger.info("已自动填写账号密码并提交登录表单.")
            error_task = asyncio.create_task(report_login_error_later(page))
        except TimeoutError:
            if is_login_page(page.url):
                logger.warn("未找到自动登录控件,请在浏览器中手动完成登录.", shift=True)
                logger.event("自动登录", 方式="手动", 原因="未找到登录控件")
                logger.event("人工介入", 状态="需要", 原因="未找到自动登录控件,请在浏览器中手动完成登录.")
        except Exception as exc:
            if is_login_page(page.url):
                logger.log_exception("自动填写登录表单失败, 请在浏览器中手动完成登录.", exc)
                logger.event("自动登录", 方式="手动", 原因=type(exc).__name__)
                logger.event("人工介入", 状态="需要", 原因="自动填写登录表单失败, 请在浏览器中手动完成登录.")
    else:
        logger.info("配置文件未填写账号密码,请在浏览器中手动登录.", shift=True)
        logger.event("自动登录", 方式="手动", 原因="未配置账号密码")
        logger.event("人工介入", 状态="需要", 原因="配置文件未填写账号密码,请在浏览器中手动登录.")

    captcha_task = None
    if config.enableAutoCaptcha and modules:
        logger.event("滑块任务", 状态="启动")
        captcha_task = asyncio.create_task(slider_verify(page, modules))

    try:
        await wait_for_login_complete(page)
    finally:
        for task in (error_task, captcha_task):
            if task and not task.done():
                task.cancel()
        pending = [task for task in (error_task, captcha_task) if task]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    logger.event("登录完成", 耗时=f"{time.time() - wait_start:.1f}s", 地址=page.url)
    logger.event("人工介入", 状态="已解除", 原因="登录完成")
    await persist_login_cookies(context)
    logger.info(f"已保存登录凭证到: {COOKIE_PATH},下次可免密登录.")


async def ensure_login(
    context: BrowserContext, page: Page, cookies, config, modules=None
) -> bool:
    if cookies:
        logger.info("正在校验 Cookies 登录状态...")
        await page.goto(config.login_url, wait_until="domcontentloaded")
        try:
            await wait_for_login_complete(page, timeout=10000)
        except TimeoutError:
            pass
        if not is_login_page(page.url):
            logger.info("使用Cookies登录成功!")
            logger.event("登录状态", 结果="Cookies 有效", 地址=page.url)
            await persist_login_cookies(context)
            return True
        logger.warn("检测到 Cookies 已失效, 将重新登录.", shift=True)
        logger.event("登录状态", 结果="Cookies 失效", 地址=page.url)
        clear_cookies(COOKIE_PATH)
        remember_login_cookies(None)
        cookies = None

    if not config.username or not config.password:
        logger.info("请手动填写账号密码...")
    logger.info("正在等待登录完成...")
    await auto_login(context, page, config, modules)
    logger.info("登录成功!")
    return False


def save_credentials_to_config(config_path: str, username: str, password: str) -> bool:
    """把账号密码写回 config.ini, 只替换目标行, 保留文件中的其余内容和注释。"""
    raw = None
    used_encoding = "utf-8"
    for encoding in ("utf-8-sig", "gbk"):
        try:
            with open(config_path, "r", encoding=encoding) as file:
                raw = file.read()
            # utf-8-sig 仅用于剥离 BOM, 回写统一用 utf-8, 避免给原文件追加 BOM
            used_encoding = "utf-8" if encoding == "utf-8-sig" else encoding
            break
        except UnicodeDecodeError:
            continue
    if raw is None:
        logger.warn("配置文件读取失败,账号密码仅本次运行生效.", shift=True)
        return False

    def replace_field(text: str, key: str, value: str) -> str:
        # 只匹配行内空白: \s 会吞掉换行, 导致下一行被整体替换掉
        pattern = re.compile(rf"(?m)^([ \t]*{key}[ \t]*=[ \t]*).*$")
        if pattern.search(text):
            return pattern.sub(lambda match: match.group(1) + value, text, count=1)
        if "[user-account]" in text:
            return text.replace("[user-account]", f"[user-account]\n{key} = {value}", 1)
        return text.rstrip("\r\n") + f"\n[user-account]\n{key} = {value}\n"

    raw = replace_field(raw, "username", username)
    raw = replace_field(raw, "password", password)
    try:
        with open(config_path, "w", encoding=used_encoding) as file:
            file.write(raw)
    except OSError as exc:
        logger.log_exception("保存账号密码到配置文件失败.", exc)
        return False
    return True


def ensure_credentials(config) -> None:
    """配置文件未填账号密码时, 在终端引导输入并回存, 之后即可全自动登录。"""
    if config.username and config.password:
        return
    if not config.config_path or not sys.stdin.isatty() or not sys.stdout.isatty():
        return
    try:
        logger.info("配置文件未填写账号密码,现在输入一次即可永久自动登录(直接回车可跳过).", shift=True)
        username = input("请输入账号(手机号): ").strip()
        if not username:
            return
        password = getpass.getpass("请输入密码: ").strip()
        if not password:
            return
    except (EOFError, KeyboardInterrupt):
        logger.info("", shift=True)
        return
    if save_credentials_to_config(config.config_path, username, password):
        logger.event("自动登录", 方式="账号密码", 状态="已保存到配置文件", 文件=config.config_path)
    config.username = username
    config.password = password
    logger.info("已记录本次输入的账号密码,正在自动登录...")


async def run_resources_stage(
    page: Page, course_url: str, config, logger, playback_enabled
) -> dict:
    """按知识点逐个核对「必学资源」, 把漏掉的视频/文档/PPT 补齐."""
    logger.section("必学资源补齐")
    payload = await enumerate_nodes(page, course_url)
    nodes = [node for node in payload["nodes"] if not node.get("freeExam")]
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
        learn_url = build_learn_url(
            payload["courseId"], node["nodeUid"], payload["classId"]
        )
        result = await complete_resources_for_node(
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


def _formal_exam_succeeded(
    ctx, submitted: bool, answered: int, total: int, auto_submit: bool
) -> bool:
    failed_saves = getattr(ctx, "_failed_saves", 0)
    if not isinstance(failed_saves, int) or isinstance(failed_saves, bool):
        failed_saves = 0
    if auto_submit:
        return bool(submitted)
    return (total == 0) or (answered >= total and failed_saves == 0)


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
    if not getattr(config, "enableFormalExamApi", True):
        return {}
    if not course_id or not class_id:
        parsed = parse_url(course_url)
        course_id = course_id or str(parsed.get("courseId") or "")
        class_id = class_id or str(parsed.get("classId") or "")
    if not course_id:
        try:
            payload = await enumerate_nodes(page, course_url)
        except Exception as exc:
            logger.debug(f"综合考试解析课程 ID 失败: {exc}")
            payload = {}
        course_id = course_id or str(payload.get("courseId") or "")
        class_id = class_id or str(payload.get("classId") or "")
    if api is None:
        api = await build_page_exam_api(page, config)
    auto_submit = bool(quiz_page.llm.conf().get("auto_submit", False))
    logger.section("综合考试")
    formal = await asyncio.to_thread(
        run_formal_exam_tasks,
        api,
        course_id,
        class_id,
        auto_submit,
        logger,
    )
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
    summary: dict = {}
    api = None
    course_id = ""
    class_id = ""
    if getattr(config, "enableExamApi", True):
        logger.section("接口考试")
        try:
            payload = await enumerate_nodes(page, course_url)
            course_id = str(payload.get("courseId") or "")
            class_id = str(payload.get("classId") or "")
            nodes = [
                node for node in payload.get("nodes") or [] if node.get("nodeUid")
            ]
            if not nodes:
                logger.warn("没有枚举到知识点, 跳过知识点接口考试.")
            else:
                api = await build_page_exam_api(page, config)
                limit = getattr(config, "limitMaxTime", 0) or 0
                deadline = time.monotonic() + limit * 60 if limit > 0 else None
                runner = ExamRunner(
                    course_id,
                    class_id,
                    api,
                    target=int(config.masteryTarget),
                    enable_ppt=getattr(config, "enablePptReference", True),
                )
                logger.info(
                    f"开始接口考试: 知识点 {len(nodes)} 个, 目标掌握度 {int(config.masteryTarget)}%"
                )
                logger.event(
                    "接口考试范围",
                    知识点=len(nodes),
                    目标掌握度=int(config.masteryTarget),
                )
                summary = await asyncio.to_thread(runner.run, nodes, deadline)
                logger.event(
                    "接口考试汇总",
                    达标=len(summary.get("done") or []),
                    跳过=len(summary.get("skipped") or []),
                    未完成=len(summary.get("failed") or []),
                )
        except Exception as exc:
            logger.log_exception("接口考试阶段失败.", exc)

    if run_formal and getattr(config, "enableFormalExamApi", True):
        try:
            formal = await run_formal_exam_stage(
                page,
                course_url,
                config,
                logger,
                api=api,
                course_id=course_id,
                class_id=class_id,
            )
        except Exception as exc:
            logger.log_exception("综合考试扫描失败.", exc)
        else:
            summary["formal_exams"] = formal
    return summary


def run_formal_exam_tasks(
    api: ExamApi,
    course_id,
    class_id,
    auto_submit: bool,
    logger,
) -> dict:
    """扫描 AI 智慧课综合/单元测试任务并逐个执行 (在线程中调用)."""
    probe = AiFormalExamCtx.from_task(
        api,
        {"courseId": course_id, "classId": class_id},
        auto_submit=auto_submit,
    )
    tasks = probe.scan_pending_tasks()
    summary = {
        "found": len(tasks),
        "done": 0,
        "submitted": 0,
        "failed": 0,
        "details": [],
    }
    for task in tasks:
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
        ctx = AiFormalExamCtx.from_task(api, task, auto_submit=auto_submit)
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
    session = getattr(client, "session", None)
    if session is None:
        return
    for cookie in cookies or []:
        name = cookie.get("name")
        value = cookie.get("value")
        if name and value is not None:
            kwargs = {}
            domain = str(cookie.get("domain") or "").strip()
            if domain:
                kwargs["domain"] = domain
            path = str(cookie.get("path") or "").strip()
            if path:
                kwargs["path"] = path
            session.cookies.set(name, value, **kwargs)
    reset = getattr(client, "reset_exam_sso", None)
    if callable(reset):
        reset()


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
            logger.debug(f"补充获取 schoolId 失败, 继续使用空值: {exc}")
    return meta


async def _run_zhidao_with_captcha(
    page: Page, client, config, logger, *, label: str, captcha_url: str, run_fn
):
    """在线程中执行知到接口调用; 若触发人机验证则在浏览器中处理后重试一次."""
    from modules.zhs_api import CaptchaRequiredError

    last_error = None
    for attempt in range(2):
        try:
            return await asyncio.to_thread(run_fn)
        except CaptchaRequiredError as exc:
            last_error = exc
            logger.warn(f"「{label}」触发人机验证: {exc}")
            if attempt == 0:
                if captcha_url and getattr(page, "goto", None):
                    try:
                        await page.goto(
                            captcha_url, wait_until="domcontentloaded", timeout=30000
                        )
                    except Exception as nav_exc:
                        logger.debug(f"打开验证页失败: {nav_exc}")
                if await homework._settle_captcha(page, config):
                    try:
                        sync_zhs_client_cookies(client, await page.context.cookies())
                    except Exception as sync_exc:
                        logger.debug(f"人机验证后同步 Cookie 失败: {sync_exc}")
                    logger.info(f"「{label}」人机验证已通过, 重试接口作答...")
                    continue
            break
    raise last_error


async def _run_vision_fallback_on_url(
    page: Page, url: str, config, logger, *, label: str
) -> dict:
    """作业/考试含图片题时导航到直达页, 跳过已跑过的 API 阶段直接用视觉模型补答."""
    if not quiz_page.llm.vision_enabled():
        logger.warn(
            f"「{label}」含图片题, 但未配置视觉模型, 无法识别图片题干. "
            "请在设置的大模型卡片填写支持读图的「视觉模型」, 已跳过图片题."
        )
        return {"skipped": True, "reason": "no_vision_model"}
    logger.info(f"「{label}」含图片题, 正在打开答题页使用视觉模型补答...")
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except TimeoutError:
        logger.warn(f"「{label}」答题页加载较慢, 继续等待题目渲染...")
    await page.wait_for_timeout(1500)
    return await homework.auto_answer_homework(page, config=config, skip_api=True)


async def run_zhidao_assessment_stage(
    page: Page, course_url: str, parsed: dict, config, logger
) -> dict:
    """共享课/融合课视频完成后: 自动扫描并完成知到作业与进行中的正式考试."""
    from modules.ai_analysis_api import AiAnalysisApi
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

    recruit_secret = str((parsed or {}).get("recruitAndCourseId") or "").strip()
    do_hw = bool(getattr(config, "enableHomeworkApi", True))
    do_exam = bool(getattr(config, "enableFormalExamApi", True))
    if not recruit_secret or (not do_hw and not do_exam):
        return {}

    client = await build_page_zhs_client(page, config)
    try:
        meta = await _run_zhidao_with_captcha(
            page,
            client,
            config,
            logger,
            label="课程信息查询",
            captcha_url=course_url,
            run_fn=lambda: resolve_zhidao_course_meta(client, recruit_secret),
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
    auto_submit = bool(quiz_page.llm.conf().get("auto_submit", False))
    store = AiQuestionStore()
    if not quiz_page.llm.enabled():
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
        hw_worker = ZhidaoHomeworkWorker(
            hw_api,
            course_id=course_id,
            course_name=course_name,
            recruit_id=recruit_id,
            school_id=school_id,
            store=store,
            analysis_api=AiAnalysisApi(session=client.session, proxy=proxy),
            auto_submit=auto_submit,
            target_score=threshold,
            max_rounds=max_redo,
        )
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
                logger.info(f"[{idx}/{total_hw}] 开始作答知到作业: {exam_name}")
                logger.event(
                    "知到作业",
                    状态="开始",
                    名称=exam_name,
                    序号=f"{idx}/{total_hw}",
                )
                try:
                    stats = await _run_zhidao_with_captcha(
                        page,
                        client,
                        config,
                        logger,
                        label=exam_name,
                        captcha_url=item_url,
                        run_fn=lambda r=item_recruit, c=item_course, e=exam_id, s=stu_exam_id, sc=item_school: hw_worker.run(
                            recruit_id=r,
                            course_id=c,
                            homework_id=e,
                            stu_exam_id=s,
                            exam_id=e,
                            school_id=str(sc or ""),
                        ),
                    )
                except Exception as exc:
                    hw_summary["failed"] += 1
                    hw_summary["details"].append(
                        {"name": exam_name, "ok": False, "error": str(exc)}
                    )
                    logger.warn(f"[{idx}/{total_hw}] 作业「{exam_name}」作答失败: {exc}")
                    logger.event(
                        "知到作业",
                        状态="失败",
                        名称=exam_name,
                        序号=f"{idx}/{total_hw}",
                        错误=logger.summarize_exception(exc),
                    )
                    continue

                vision_stats = None
                if stats.get("needs_vision"):
                    vision_stats = await _run_vision_fallback_on_url(
                        page, item_url, config, logger, label=exam_name
                    )
                    stats["vision"] = vision_stats

                answered = int(stats.get("answered", 0) or 0)
                if isinstance(vision_stats, dict) and not vision_stats.get("skipped"):
                    answered += int(vision_stats.get("ok", 0) or 0) + int(
                        vision_stats.get("fill", 0) or 0
                    )
                if answered == 0 and isinstance(vision_stats, dict) and vision_stats.get("skipped"):
                    hw_summary["skipped"] += 1
                    hw_summary["details"].append(
                        {
                            "name": exam_name,
                            "skipped": True,
                            "reason": vision_stats.get("reason", "no_vision_model"),
                        }
                    )
                    continue
                if answered == 0 and not stats.get("needs_vision"):
                    hw_summary["skipped"] += 1
                    hw_summary["details"].append(
                        {"name": exam_name, "skipped": True, "reason": "no_questions"}
                    )
                    continue
                hw_summary["done"] += 1
                hw_summary["details"].append(
                    {
                        "name": exam_name,
                        "ok": True,
                        "answered": answered,
                        "submitted": bool(stats.get("submitted")),
                        "score": stats.get("score"),
                        "needs_vision": bool(stats.get("needs_vision")),
                    }
                )
                logger.info(
                    f"[{idx}/{total_hw}] 作业「{exam_name}」完成: "
                    f"作答 {answered} 题, 已提交={bool(stats.get('submitted'))}, "
                    f"得分={stats.get('score')}"
                )
                logger.event(
                    "知到作业",
                    状态="完成",
                    名称=exam_name,
                    序号=f"{idx}/{total_hw}",
                    作答=answered,
                    已交卷=bool(stats.get("submitted")),
                    得分=stats.get("score"),
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
        )
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
                logger.info(f"[{idx}/{total_ex}] 接管进行中的正式考试: {exam_name}")
                logger.event(
                    "知到考试",
                    状态="开始",
                    名称=exam_name,
                    序号=f"{idx}/{total_ex}",
                )
                try:
                    stats = await _run_zhidao_with_captcha(
                        page,
                        client,
                        config,
                        logger,
                        label=exam_name,
                        captcha_url=exam_url,
                        run_fn=lambda r=item_recruit, c=item_course, e=exam_id, s=stu_exam_id, sc=item_school, n=exam_name: exam_worker.run(
                            recruit_id=r,
                            course_id=c,
                            exam_id=e,
                            student_exam_id=s,
                            school_id=str(sc or ""),
                            exam_name=n,
                        ),
                    )
                except Exception as exc:
                    exam_summary["failed"] += 1
                    exam_summary["details"].append(
                        {"name": exam_name, "ok": False, "error": str(exc)}
                    )
                    logger.warn(f"[{idx}/{total_ex}] 考试「{exam_name}」作答失败: {exc}")
                    logger.event(
                        "知到考试",
                        状态="失败",
                        名称=exam_name,
                        序号=f"{idx}/{total_ex}",
                        错误=logger.summarize_exception(exc),
                    )
                    continue

                if stats.get("skipped"):
                    exam_summary["skipped"] += 1
                    exam_summary["details"].append(
                        {"name": exam_name, "skipped": True, "reason": "locked"}
                    )
                    continue

                vision_stats = None
                if stats.get("needs_vision"):
                    vision_stats = await _run_vision_fallback_on_url(
                        page, exam_url, config, logger, label=exam_name
                    )
                    stats["vision"] = vision_stats

                answered = int(stats.get("answered", 0) or 0)
                if isinstance(vision_stats, dict) and not vision_stats.get("skipped"):
                    answered += int(vision_stats.get("ok", 0) or 0) + int(
                        vision_stats.get("fill", 0) or 0
                    )
                if answered == 0 and isinstance(vision_stats, dict) and vision_stats.get("skipped"):
                    exam_summary["skipped"] += 1
                    exam_summary["details"].append(
                        {
                            "name": exam_name,
                            "skipped": True,
                            "reason": vision_stats.get("reason", "no_vision_model"),
                        }
                    )
                    continue
                if answered == 0 and not stats.get("needs_vision"):
                    exam_summary["skipped"] += 1
                    exam_summary["details"].append(
                        {"name": exam_name, "skipped": True, "reason": "no_questions"}
                    )
                    continue
                exam_summary["done"] += 1
                exam_summary["details"].append(
                    {
                        "name": exam_name,
                        "ok": True,
                        "answered": answered,
                        "submitted": bool(stats.get("submitted")),
                        "needs_vision": bool(stats.get("needs_vision")),
                    }
                )
                logger.info(
                    f"[{idx}/{total_ex}] 考试「{exam_name}」完成: "
                    f"作答 {answered} 题, 已交卷={bool(stats.get('submitted'))}"
                )
                logger.event(
                    "知到考试",
                    状态="完成",
                    名称=exam_name,
                    序号=f"{idx}/{total_ex}",
                    作答=answered,
                    已交卷=bool(stats.get("submitted")),
                )
        logger.event(
            "知到考试汇总",
            发现=exam_summary["found"],
            完成=exam_summary["done"],
            跳过=exam_summary["skipped"],
            失败=exam_summary["failed"],
        )

    return summary


async def run_formal_exam_route(page: Page, parsed: dict, config, logger) -> dict:
    """testDetail 链接: 直接按综合考试接口作答."""
    api = await build_page_exam_api(page, config)
    auto_submit = bool(quiz_page.llm.conf().get("auto_submit", False))
    task = {
        "courseId": parsed.get("courseId", ""),
        "classId": parsed.get("classId", ""),
        "examTestId": parsed.get("examTestId", ""),
        "examPaperId": parsed.get("examPaperId", ""),
    }
    logger.info(f"检测到综合考试链接, 开始接口作答 (自动交卷={auto_submit}) ...")
    logger.event("综合考试", 状态="开始", 自动交卷=auto_submit)
    ctx = AiFormalExamCtx.from_task(api, task, auto_submit=auto_submit)
    submitted, answered, total = await asyncio.to_thread(ctx.start, submit=auto_submit)
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


async def run_learn_page_route(
    page: Page, parsed: dict, config, logger, playback_enabled
) -> dict:
    """learnPage 单知识点链接: 先补资源, 再只对该节点跑接口考试."""
    course_id = str(parsed.get("courseId", ""))
    node_uid = str(parsed.get("nodeUid", ""))
    class_id = str(parsed.get("classId", ""))
    if not course_id or not node_uid or not class_id:
        logger.warn("learnPage 链接缺少 courseId/nodeUid/classId, 已跳过.")
        return {}
    learn_url = build_learn_url(course_id, node_uid, class_id)
    if not getattr(config, "reviewMode", False):
        result = await complete_resources_for_node(
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
    api = await build_page_exam_api(page, config)
    limit = getattr(config, "limitMaxTime", 0) or 0
    deadline = time.monotonic() + limit * 60 if limit > 0 else None
    runner = ExamRunner(
        course_id,
        class_id,
        api,
        target=int(config.masteryTarget),
        enable_ppt=getattr(config, "enablePptReference", True),
    )
    node = {"nodeUid": node_uid, "name": f"知识点 {node_uid}"}
    summary = await asyncio.to_thread(runner.run, [node], deadline)
    logger.event(
        "接口考试汇总",
        达标=len(summary.get("done") or []),
        跳过=len(summary.get("skipped") or []),
        未完成=len(summary.get("failed") or []),
    )
    return summary


_DIRECT_TASK_KINDS = (
    KIND_HOMEWORK,
    KIND_EXAM,
    KIND_TEST_DETAIL,
    KIND_LEARN_PAGE,
)


def course_route(parsed: dict, course_url: str) -> str:
    """决定课程 URL 走精准任务路由 / ai-smart 全课流程 / 常规目录."""
    kind = (parsed or {}).get("kind", "")
    if kind in _DIRECT_TASK_KINDS:
        return "task"
    if kind == KIND_KNOWLEDGE_STUDY or is_ai_smart_url(course_url):
        return "ai_smart"
    return "catalog"


async def run_task_url(
    page: Page, course_url: str, parsed: dict, config, logger, playback_enabled
) -> dict:
    """执行 dohomework/doexam/testDetail/learnPage 链接对应的任务."""
    kind = parsed.get("kind", "")
    if kind in (KIND_HOMEWORK, KIND_EXAM):
        stats = await homework.auto_answer_homework(page, config=config)
        logger.event(
            "任务链接",
            类型=kind,
            作答=stats.get("answered"),
            已交卷=stats.get("submitted"),
        )
        return stats
    if kind == KIND_TEST_DETAIL:
        return await run_formal_exam_route(page, parsed, config, logger)
    return await run_learn_page_route(
        page, parsed, config, logger, playback_enabled
    )


async def run_mastery_stage(page: Page, course_url: str, config, logger) -> dict:
    """AI 智慧课: 视频刷完后, 把每个知识点的掌握度刷到目标值."""
    logger.section("掌握度提升")
    payload = await enumerate_nodes(page, course_url)
    nodes = payload.get("nodes") or []
    free_count = sum(1 for node in nodes if node.get("freeExam"))
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
    summary = await run_course_mastery(
        page,
        nodes,
        payload["courseId"],
        payload["classId"],
        cache,
        logger,
        target=target,
        max_attempts=3,
    )
    return summary


async def maybe_qr_login(config, cookies):
    """启用二维码登录且本地无可用 Cookie 时，先通过终端/图片扫码换取 Cookies。"""
    if cookies or not getattr(config, "qr_login", False):
        return cookies
    logger.info("已启用二维码登录，正在获取智慧树登录二维码...", shift=True)
    show_in_terminal = bool(
        getattr(config, "qr_terminal", False) or not getattr(config, "qr_save_dir", "")
    )
    new_cookies = await asyncio.to_thread(
        qr_login_for_cookies,
        show_in_terminal=show_in_terminal,
        ensure_unicode=bool(getattr(config, "qr_unicode", False)),
        save_dir=getattr(config, "qr_save_dir", ""),
        proxy=getattr(config, "proxy", ""),
        logger=logger,
    )
    if new_cookies:
        try:
            save_cookies(new_cookies, COOKIE_PATH)
            remember_login_cookies(new_cookies)
            logger.info(f"二维码登录成功，已保存 {len(new_cookies)} 条凭证到: {COOKIE_PATH}", shift=True)
        except Exception as exc:
            logger.log_exception("保存二维码登录 Cookies 失败.", exc)
    return new_cookies


async def run_fetch_courses(config, write_config: bool = False) -> int:
    """登录并自动拉取账号下所有课程列表，导出到 data/courses.json（可选写入 config.ini）。"""
    logger.section("拉取账号课程列表")
    cookies = None if getattr(config, "attach_existing_chrome", False) else load_cookies(COOKIE_PATH)
    remember_login_cookies(cookies)
    if not getattr(config, "attach_existing_chrome", False):
        cookies = await maybe_qr_login(config, cookies)
    async with async_playwright() as p:
        session = await create_browser_session(p, config, cookies, logger)
        page, context = session.page, session.context
        try:
            await ensure_login(context, page, cookies, config)
            logger.info("正在从智慧树接口与学生主页读取课程列表...")
            courses = await fetch_courses_from_page(
                page,
                logger=logger,
                proxy=str(getattr(config, "proxy", "") or "").strip(),
            )
            await persist_login_cookies(context)
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


async def main(config) -> bool:
    global event_loop_verify, event_loop_answer, COOKIE_PATH
    event_loop_verify = asyncio.Event()
    event_loop_answer = asyncio.Event()
    COOKIE_PATH = get_runtime_path("data", "cookies.json")
    quiz_page.llm.reset_cache()
    quiz_page.reset_zhidao_cooldown()
    quiz_page.llm.set_proxy(getattr(config, "proxy", ""))
    modules, tasks = [], []
    playback_enabled = asyncio.Event()
    popups_enabled = asyncio.Event()
    popups_enabled.set()
    all_courses_complete = True
    run_ok = True
    notifier = Notifier.from_config(config, logger=logger)
    if config.enableAutoCaptcha:
        logger.section("依赖检查")
        logger.info("正在检查验证码识别依赖...")
        modules = installer.start(config)
        logger.info("验证码识别依赖已就绪!")

    logger.section("登录")
    async with async_playwright() as p:
        cookies = None if getattr(config, "attach_existing_chrome", False) else load_cookies(COOKIE_PATH)
        remember_login_cookies(cookies)
        if not getattr(config, "attach_existing_chrome", False):
            cookies = await maybe_qr_login(config, cookies)
        logger.event("本地凭证", 数量=len(cookies) if cookies else 0, 文件=COOKIE_PATH)
        session = await create_browser_session(p, config, cookies, logger)
        page, context = session.page, session.context
        # 捕获课程流程中新开的测验页面(如课堂测验跳转的新标签页), 自动作答
        def _on_new_page(new_page):
            asyncio.ensure_future(quiz_page.auto_answer_new_page(new_page, config))
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
        monitor_task = None
        try:
            login_by_cookie = await ensure_login(context, page, cookies, config, modules)

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
                            page, config, event_loop_verify, modules, notifier=notifier
                        )
                    ),
                    asyncio.create_task(video_optimize(page, config, playback_enabled)),
                    asyncio.create_task(
                        skip_questions(page, event_loop_answer, playback_enabled, config=config)
                    ),
                    asyncio.create_task(play_video(page, playback_enabled)),
                    asyncio.create_task(dismiss_popups(page, popups_enabled)),
                    asyncio.create_task(periodic_cookie_sync(context)),
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
                    clear_cookies(COOKIE_PATH)
                    remember_login_cookies(None)
                    await ensure_login(context, page, None, config, modules)
                    if should_hide_window(config):
                        await hide_window(page)
                    logger.info("重新进入播放页...")
                    try:
                        await page.goto(course_url, wait_until="domcontentloaded", timeout=30000)
                    except TimeoutError:
                        logger.warn("播放页加载较慢，继续等待目录渲染...", shift=True)
                    await page.wait_for_timeout(2000)

                parsed = parse_url(course_url)
                route = course_route(parsed, course_url)
                if route == "task":
                    kind = parsed.get("kind", "")
                    logger.info(f"检测到 {kind} 任务链接, 走精准路由.")
                    logger.event("任务链接", 类型=kind, 地址=course_url)
                    await persist_login_cookies(context)
                    playback_enabled.clear()
                    try:
                        await run_task_url(
                            page, course_url, parsed, config, logger, playback_enabled
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
                    await persist_login_cookies(context)
                    playback_enabled.clear()
                    outcome = await run_ai_smart_course(
                        page, course_url, config, logger, playback_enabled
                    )
                    playback_enabled.clear()
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
                            page, course_url, config, logger, playback_enabled
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
                        popups_enabled.clear()
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
                            popups_enabled.set()
                    continue

                catalog = await detect_catalog_after_verification(page, page.url)
                logger.info(f"检测到 {catalog.name} 课程目录.")
                logger.context(目录类型=catalog.name)
                logger.event("课程目录", 类型=catalog.name, 地址=page.url)
                await persist_login_cookies(context)
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

                playback_enabled.clear()
                outcome = await run_course(
                    page, catalog, config, logger, playback_enabled
                )
                playback_enabled.clear()
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
                assessment_parsed = parsed
                if not assessment_parsed.get("recruitAndCourseId"):
                    assessment_parsed = parse_url(getattr(page, "url", ""))
                if assessment_parsed.get("recruitAndCourseId") and (
                    getattr(config, "enableHomeworkApi", True)
                    or getattr(config, "enableFormalExamApi", True)
                ):
                    popups_enabled.clear()
                    try:
                        await run_zhidao_assessment_stage(
                            page, course_url, assessment_parsed, config, logger
                        )
                    except Exception as exc:
                        logger.log_exception("知到作业/考试阶段失败.", exc)
                    finally:
                        popups_enabled.set()
        finally:
            for task in tasks:
                task.cancel()
            if monitor_task:
                monitor_task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if monitor_task:
                await asyncio.gather(monitor_task, return_exceptions=True)
            try:
                await persist_login_cookies(context)
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
        logger.event(
            "运行环境",
            版本=__version__,
            Python=platform.python_version(),
            运行方式="打包" if getattr(sys, "frozen", False) else "源码",
            系统=platform.system(),
            系统版本=platform.release(),
            系统架构=platform.machine(),
            启动参数=(f"[{' '.join(sys.argv[1:])}]" if sys.argv[1:] else "无"),
            日志文件=logger.filename,
        )
        installer.validate_python_version()
        base_dir = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
        default_config_name = "config.macos.ini" if sys.platform == "darwin" else "config.ini"
        config_path = args.config or os.path.join(base_dir, default_config_name)
        mirrors_path = os.path.join(base_dir, "data", "mirrors.json")
        # 启动即补齐缺失的配置文件(只创建不覆盖), 个人配置不会被提交到仓库
        created_files = ensure_config_files(config_path)
        for path in created_files:
            logger.info(f"已生成默认配置文件: {path}", shift=True)
        if config_path in created_files and not (
            args.course or args.fetch_courses or args.test_notify or args.import_cookies
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
            大模型=quiz_page.llm.provider_label() if quiz_page.llm.enabled() else "未配置",
            视觉模型=quiz_page.llm.vision_label() if quiz_page.llm.vision_enabled() else "未配置",
            智慧树AI答题=config.enableZhidaoQuiz,
            接口考试=config.enableExamApi,
            掌握度刷题=config.enableMastery,
            复习考试接口=config.enableReviewExamApi,
            目标掌握度=config.masteryTarget,
            消息推送=",".join(notifier.configured_channels()) or "未配置",
            静音=config.soundOff,
            复习模式=config.reviewMode,
            倍速=config.limitSpeed,
            时限分钟=config.limitMaxTime,
            课程数=len(config.course_urls),
            镜像源=len(config.mirrors),
        )
        for index, course_url in enumerate(config.course_urls, 1):
            logger.event("课程地址", 序号=f"{index}/{len(config.course_urls)}", 地址=course_url)
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
            logger.error(f"配置文件错误!")
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
