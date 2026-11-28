import asyncio
import time

from playwright.async_api import TimeoutError
from playwright.async_api import Page
from modules.configs import Config
from modules.llm import answer_with_llm
from modules.utils import (
    display_window,
    get_video_attr,
    hide_window,
    run_on,
    should_hide_window,
)
from modules.quiz_dom import TargetClosedError
from modules.logger import Logger
from modules.captcha_router import solve_any_captcha
from modules.fusion_adapter import FusionAdapter
from modules.slider import IN_COURSE_APPEAR_TIMEOUT_MS
from modules.video_state import (
    COURSE_VIDEO_SELECTOR,
    has_valid_duration,
    is_valid_video_time,
    video_at_end,
)

logger = Logger()

VERIFICATION_SELECTORS = (
    ".yidun_popup .yidun_modal",
    ".yidun_modal__title",
    "[id^='tcaptcha_transform']",
    "#tcaptcha_iframe_dy",
    "iframe[id^='tcaptcha']",
    "iframe.tcaptcha-iframe",
    "iframe[src*='captcha.qq.com']",
)

BLOCKING_OVERLAY_SELECTORS = (
    ".topic-title",
    ".ss2077-custom-dialog",
    ".ai-class-exercise-dialog",
    ".question-body",
)
PLAY_READY_SELECTOR = ".videoArea, .video-play"


async def has_visible_element(page: Page, selectors: tuple[str, ...]) -> bool:
    for selector in selectors:
        try:
            visible = await page.locator(selector).evaluate_all(
                """elements => elements.some(element => {
                    const style = getComputedStyle(element);
                    const rect = element.getBoundingClientRect();
                    const opacity = Number.parseFloat(style.opacity || "1");
                    return style.display !== "none" &&
                        style.visibility !== "hidden" &&
                        opacity > 0.05 &&
                        rect.width > 0 && rect.height > 0 &&
                        rect.bottom > 0 && rect.right > 0 &&
                        rect.top < innerHeight && rect.left < innerWidth;
                })"""
            )
            if visible:
                return True
        except TargetClosedError:
            raise
        except Exception as exc:
            logger.debug_throttled(
                "has_visible_element",
                f"可见元素检测遇到页面切换({', '.join(selectors)}): "
                f"{logger.summarize_exception(exc)}",
            )
    return False


async def has_visible_verification(page: Page) -> bool:
    return await has_visible_element(page, VERIFICATION_SELECTORS)


async def has_blocking_overlay(page: Page) -> bool:
    return await has_visible_verification(page) or await has_visible_element(
        page, BLOCKING_OVERLAY_SELECTORS
    )


async def wait_until_verification_hidden(page: Page) -> None:
    while await has_visible_verification(page):
        await asyncio.sleep(0.5)


def is_expected_polling_error(exc: Exception) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    message = str(exc)
    expected_signals = [
        "waiting for locator",
        "waiting for selector",
        "ElementHandle.press",
        "No node found for selector",
        "Execution context was destroyed",
    ]
    return any(signal in message for signal in expected_signals)


async def task_monitor(tasks: list[asyncio.Task]) -> None:
    checked_tasks = set()
    logger.info("任务监控已启动.")
    while any(not task.done() for task in tasks):
        for _i, task in enumerate(tasks):
            if task.done() and task not in checked_tasks:
                checked_tasks.add(task)
                if task.cancelled():
                    continue
                exc = task.exception()
                func_name = task.get_coro().__name__
                if exc is not None:
                    logger.log_exception(f"任务函数 {func_name} 出现异常.", exc, shift=True)
        await asyncio.sleep(1)
    logger.info("任务监控已退出.", shift=True)


async def video_optimize(
    page: Page, config: Config, playback_enabled: asyncio.Event | None = None
) -> None:
    await page.wait_for_load_state("domcontentloaded")
    while True:
        try:
            await asyncio.sleep(2)
            if playback_enabled is not None and not playback_enabled.is_set():
                continue
            try:
                await page.wait_for_selector(
                    COURSE_VIDEO_SELECTOR, state="attached", timeout=1500
                )
            except TimeoutError:
                logger.debug_throttled(
                    "video:optimize",
                    f"视频调节跳过: 页面未找到元素 {COURSE_VIDEO_SELECTOR}",
                )
                continue
            volume = await get_video_attr(page, "volume")
            rate = await get_video_attr(page, "playbackRate")
            changes = []
            if config.soundOff and volume != 0:
                await run_on(
                    page,
                    COURSE_VIDEO_SELECTOR,
                    "(el) => { el.volume = 0; }",
                    "设置静音",
                )
                await run_on(
                    page,
                    ".volumeBox",
                    '(el) => el.classList.add("volumeNone")',
                    "更新静音图标",
                )
                changes.append(f"音量 {volume}->0")
            if rate != config.limitSpeed:
                await run_on(
                    page,
                    COURSE_VIDEO_SELECTOR,
                    f"(el) => {{ el.playbackRate = {config.limitSpeed}; }}",
                    "设置倍速",
                )
                await run_on(
                    page,
                    ".speedBox span",
                    f'(el) => {{ el.innerText = "X {config.limitSpeed}"; }}',
                    "更新倍速标签",
                )
                changes.append(f"倍速 {rate}->{config.limitSpeed}")
            if changes:
                logger.info(f"已调节播放器: {', '.join(changes)}")
                logger.event("播放器调节", 项目=", ".join(changes))
        except TargetClosedError:
            logger.debug("浏览器已关闭, 视频调节模块停止运行.")
            return
        except Exception as e:
            if is_expected_polling_error(e):
                logger.debug_throttled(
                    "video_optimize",
                    f"视频调节模块轮询未命中: {logger.summarize_exception(e)}",
                )
            else:
                logger.log_exception("视频调节模块执行失败.", e)
            continue


async def play_video(
    page: Page, playback_enabled: asyncio.Event | None = None
) -> None:
    from modules.ai_smart import is_ai_segment_finished, is_ai_smart_url

    await page.wait_for_load_state("domcontentloaded")
    while True:
        try:
            await asyncio.sleep(2)
            if playback_enabled is not None and not playback_enabled.is_set():
                continue
            try:
                await page.wait_for_selector(
                    COURSE_VIDEO_SELECTOR, state="attached", timeout=1000
                )
            except TimeoutError:
                logger.debug_throttled(
                    "video:play",
                    f"视频播放跳过: 页面未找到元素 {COURSE_VIDEO_SELECTOR}",
                )
                continue
            if is_ai_smart_url(page.url) and await is_ai_segment_finished(page):
                continue
            state = await run_on(
                page,
                COURSE_VIDEO_SELECTOR,
                """(el) => ({
                    paused: el.paused,
                    ended: el.ended,
                    currentTime: el.currentTime,
                    duration: el.duration,
                })""",
                "读取播放器状态",
            )
            if state is None:
                continue
            paused = state["paused"]
            blocked = await has_blocking_overlay(page)
            if blocked:
                if not paused:
                    await run_on(
                        page,
                        COURSE_VIDEO_SELECTOR,
                        "(el) => el.pause()",
                        "遮罩层暂停视频",
                    )
                    logger.info("检测到遮罩层,已暂停视频等待处理.")
                    logger.event("遮罩层暂停", 播放器时间=round(state["currentTime"], 1))
                continue
            at_end = state["ended"] or video_at_end(
                state["currentTime"], state["duration"]
            )
            if paused and not at_end:
                logger.info("检测到视频暂停,正在尝试播放.")
                try:
                    await page.wait_for_selector(PLAY_READY_SELECTOR, timeout=1000)
                except TimeoutError:
                    logger.debug_throttled(
                        "videoArea",
                        f"尝试播放跳过: 页面未找到元素 {PLAY_READY_SELECTOR}",
                    )
                    continue
                played = await run_on(
                    page,
                    COURSE_VIDEO_SELECTOR,
                    "async (el) => { await el.play(); return !el.paused; }",
                    "恢复播放",
                )
                if played:
                    current_sec = (
                        round(state["currentTime"], 1)
                        if is_valid_video_time(state["currentTime"])
                        else None
                    )
                    total_sec = (
                        round(state["duration"], 1)
                        if has_valid_duration(state["duration"])
                        else None
                    )
                    logger.info("尝试播放成功,视频已恢复播放.")
                    logger.event(
                        "恢复播放",
                        播放器时间=current_sec,
                        总时长=total_sec,
                    )
                elif played is False:
                    logger.debug_throttled(
                        "video:play_unconfirmed",
                        "尝试播放未生效,视频仍处于暂停状态.",
                    )
        except TargetClosedError:
            logger.debug("浏览器已关闭, 视频播放模块停止运行.")
            return
        except Exception as e:
            if is_expected_polling_error(e):
                logger.debug_throttled(
                    "play_video",
                    f"视频播放模块轮询未命中: {logger.summarize_exception(e)}",
                )
            else:
                logger.log_exception("视频播放模块执行失败.", e)
            continue


async def read_popup_title(page) -> str:
    """读取当前弹题题干文本, 用于按题隔离匹配拦截答案."""
    try:
        element = await page.query_selector(".topic-title")
        if element is None:
            return ""
        return " ".join((await element.inner_text()).split())
    except TargetClosedError:
        raise
    except Exception:
        return ""


async def click_intercepted_answers(
    page, interceptor, choices, *, question_title=None
) -> bool:
    """按接口拦截到的标准答案点选 DOM 选项; 未命中返回 False 静默回退."""
    if interceptor is None or not choices:
        return False
    handles = []
    texts = []
    for choice in choices:
        try:
            text = await choice.inner_text()
        except TargetClosedError:
            raise
        except Exception:
            continue
        handles.append(choice)
        texts.append(text or "")
    if not texts:
        return False
    title = question_title
    if title is None:
        title = await read_popup_title(page)
    try:
        indices = interceptor.match_choices(texts, question_title=title or "")
    except Exception as exc:
        logger.debug(f"接口拦截答案匹配失败: {logger.summarize_exception(exc)}")
        return False
    clicked = 0
    for index in indices:
        if index < 0 or index >= len(handles):
            continue
        try:
            await handles[index].click(timeout=500)
            clicked += 1
        except TargetClosedError:
            raise
        except Exception:
            continue
    return clicked > 0


async def skip_questions(
    page: Page,
    event_loop,
    playback_enabled: asyncio.Event | None = None,
    config=None,
) -> None:
    """挂载弹题接口只读监听, 运行答题轮询, 结束后必定移除监听."""
    interceptor = None
    if config is None or getattr(config, "enablePopupIntercept", True):
        from modules.popup_interceptor import PopupAnswerInterceptor

        try:
            interceptor = PopupAnswerInterceptor(page)
            interceptor.attach()
        except Exception as exc:
            logger.debug(f"课中弹题接口拦截挂载失败: {logger.summarize_exception(exc)}")
            interceptor = None
    try:
        await _skip_questions_loop(
            page, event_loop, playback_enabled, config, interceptor
        )
    finally:
        if interceptor is not None:
            interceptor.detach()


async def _skip_questions_loop(
    page: Page,
    event_loop,
    playback_enabled: asyncio.Event | None = None,
    config=None,
    interceptor=None,
) -> None:
    await page.wait_for_load_state("domcontentloaded")
    while True:
        try:
            if playback_enabled is not None and not playback_enabled.is_set():
                await asyncio.sleep(2)
                continue
            if "studywisdomh5.zhihuishu.com" in page.url:
                await asyncio.sleep(2)
                if not await has_visible_element(page, (".topic-title",)):
                    continue
                # 优先接口拦截答案, 未命中再走智慧树 AI / 自定义大模型, 仍失败回退手动
                llm_answered = False
                intercepted = False
                try:
                    choices = await page.query_selector_all(".topic-item")
                    if len(choices) < 2:
                        choices = await page.query_selector_all(
                            "[class*=option], [class*=answer], .el-radio, .el-checkbox"
                        )
                    intercepted = await click_intercepted_answers(
                        page, interceptor, choices
                    )
                    if not intercepted:
                        answerer = None
                        if config is not None and getattr(config, "enableZhidaoQuiz", True):
                            from modules.quiz_page import make_zhidao_answerer

                            answerer = await make_zhidao_answerer(page, config)
                        try:
                            if answerer is not None:
                                llm_answered = bool(
                                    await answer_with_llm(
                                        page, None, choices, answerer=answerer
                                    )
                                )
                            else:
                                llm_answered = bool(
                                    await answer_with_llm(page, None, choices)
                                )
                        finally:
                            close_fn = getattr(answerer, "close", None)
                            if callable(close_fn):
                                close_fn()
                except Exception as exc:
                    logger.debug(f"新版弹题大模型作答未命中: {logger.summarize_exception(exc)}")
                if intercepted:
                    logger.info(
                        "检测到新版课中弹题, 已按接口返回答案选择选项, 等待弹题弹窗关闭...",
                        shift=True,
                    )
                    logger.event("新版课中弹题", 处理方式="接口答案拦截", 地址=page.url)
                elif llm_answered:
                    logger.info("检测到新版课中弹题, 已由 AI 选择选项, 等待弹题弹窗关闭...", shift=True)
                    logger.event("新版课中弹题", 处理方式="AI作答", 地址=page.url)
                else:
                    logger.warn("检测到新版课中弹题, 若未自动作答请在浏览器中手动处理.", shift=True)
                    logger.event("新版课中弹题", 处理方式="AI+手动兜底", 地址=page.url)
                    logger.event("人工介入", 状态="需要", 原因="检测到新版课中弹题, 若未自动作答请在浏览器中手动处理.")
                while await has_visible_element(page, (".topic-title",)):
                    await asyncio.sleep(0.5)
                logger.info("新版课中弹题弹窗已关闭, 继续学习.")
                logger.event("新版课中弹题", 状态="已关闭")
                logger.event("人工介入", 状态="已解除", 原因="新版课中弹题弹窗已关闭")
                event_loop.set()
                continue
            if "wisdom-mooc.zhihuishu.com" in page.url:
                await asyncio.sleep(2)
                if await FusionAdapter.skip_blocking_quizzes(page, config=config):
                    event_loop.set()
                continue
            if "hike.zhihuishu.com" in page.url:
                logger.warn("当前课程为新版本,不支持自动答题.", shift=True)
                logger.event("答题", 结果="跳过", 原因="课程版本不支持")
                return
            await asyncio.sleep(2)
            ques_element = await page.wait_for_selector(".el-scrollbar__view", state="attached", timeout=1000)
            total_ques = await ques_element.query_selector_all(".number")
            if total_ques:
                logger.info(f"检测到课中弹题(共 {len(total_ques)} 题), 开始自动作答...", shift=True)
                answerer = None
                if config is not None and getattr(config, "enableZhidaoQuiz", True):
                    from modules.quiz_page import make_zhidao_answerer

                    answerer = await make_zhidao_answerer(page, config)
                try:
                    answered = 0
                    llm_count = 0
                    intercept_count = 0
                    fallback_count = 0
                    skipped_count = 0
                    for idx, ques in enumerate(total_ques, 1):
                        await ques.click(timeout=500)
                        if not await page.query_selector(".answer"):
                            choices = await page.query_selector_all(".topic-item")
                            # 优先接口拦截答案, 再走 AI, 最后回退默认行为(点前两个选项)
                            intercepted = await click_intercepted_answers(
                                page, interceptor, choices
                            )
                            if intercepted:
                                intercept_count += 1
                                logger.info(
                                    f"课中弹题第 {idx}/{len(total_ques)} 题(接口答案拦截): 已按服务端答案点选"
                                )
                                logger.event(
                                    "课中答题",
                                    序号=f"{idx}/{len(total_ques)}",
                                    方式="接口答案拦截",
                                )
                            ai_ok = False
                            if not intercepted:
                                ai_ok = (
                                    await answer_with_llm(page, ques, choices, answerer=answerer)
                                    if answerer is not None
                                    else await answer_with_llm(page, ques, choices)
                                )
                            if intercepted:
                                pass
                            elif ai_ok:
                                llm_count += 1
                            else:
                                q_title = ""
                                try:
                                    title_el = await page.query_selector(".topic-title")
                                    if title_el:
                                        q_title = " ".join((await title_el.inner_text()).split())
                                except Exception:
                                    pass
                                picked_texts = []
                                for each in choices[:2]:
                                    try:
                                        opt_text = " ".join((await each.inner_text()).split())
                                        if opt_text:
                                            picked_texts.append(opt_text[:40])
                                    except Exception:
                                        pass
                                    await each.click(timeout=500)
                                    await page.wait_for_timeout(100)
                                fallback_count += 1
                                picked_summary = "; ".join(picked_texts) or f"前 {min(len(choices), 2)} 项"
                                title_summary = (q_title[:48] + "...") if len(q_title) > 48 else q_title
                                if title_summary:
                                    logger.info(
                                        f"课中弹题第 {idx}/{len(total_ques)} 题(默认选择): "
                                        f"题目「{title_summary}」 -> {picked_summary}"
                                    )
                                else:
                                    logger.info(
                                        f"课中弹题第 {idx}/{len(total_ques)} 题(默认选择): {picked_summary}"
                                    )
                                logger.event(
                                    "课中答题",
                                    序号=f"{idx}/{len(total_ques)}",
                                    方式="默认选择",
                                    题目=q_title[:80] if q_title else None,
                                    选项=picked_summary,
                                )
                            answered += 1
                        else:
                            skipped_count += 1
                            logger.debug(f"课中弹题第 {idx}/{len(total_ques)} 题已作答, 跳过.")
                    logger.info(
                        f"课中弹题处理完成: 共 {len(total_ques)} 题, "
                        f"接口拦截 {intercept_count} 题, 大模型作答 {llm_count} 题, "
                        f"默认选择 {fallback_count} 题, 已作答跳过 {skipped_count} 题."
                    )
                    logger.event(
                        "课中答题",
                        题目数=len(total_ques),
                        已作答=answered,
                        接口拦截=intercept_count,
                        大模型作答=llm_count,
                        默认选择=fallback_count,
                        跳过=skipped_count,
                    )
                finally:
                    close_fn = getattr(answerer, "close", None)
                    if callable(close_fn):
                        close_fn()
            await page.press(".el-dialog", "Escape", timeout=1000)
            event_loop.set()
        except TargetClosedError:
            logger.debug("浏览器已关闭, 答题模块停止运行.")
            return
        except Exception as e:
            if is_expected_polling_error(e):
                logger.debug_throttled(
                    "skip_questions",
                    f"答题模块轮询未命中(元素 .el-scrollbar__view/.el-dialog): "
                    f"{logger.summarize_exception(e)}",
                )
            else:
                logger.log_exception("答题模块执行失败.", e)
            if "fusioncourseh5" in page.url:
                try:
                    not_finish_close = await page.query_selector(".el-dialog")
                    if not_finish_close:
                        await page.press(".el-dialog", "Escape", timeout=1000)
                except Exception as cleanup_exc:
                    # 页面切换时执行上下文会被销毁, 清理失败不应终止答题任务
                    logger.debug_throttled(
                        "skip_questions:cleanup",
                        f"答题弹窗清理跳过: {logger.summarize_exception(cleanup_exc)}",
                    )
            elif "hike.zhihuishu.com" in page.url:
                logger.warn("当前课程为新版本,不支持自动答题.", shift=True)
                return
            else:
                try:
                    not_finish_close = await page.query_selector(".el-message-box__headerbtn")
                    if not_finish_close:
                        await not_finish_close.click()
                except Exception as cleanup_exc:
                    logger.debug_throttled(
                        "skip_questions:cleanup",
                        f"答题弹窗清理跳过: {logger.summarize_exception(cleanup_exc)}",
                    )
            continue


async def wait_for_verify(
    page: Page, config, event_loop, captcha_modules=None, notifier=None
) -> None:
    await page.wait_for_load_state("domcontentloaded")
    while True:
        try:
            await asyncio.sleep(2)
            if not await has_visible_verification(page):
                continue
            event_loop.clear()
            wait_start = time.time()
            auto_ok = False
            if config.enableAutoCaptcha:
                logger.info("检测到安全验证,正在尝试自动识别...", shift=True)
                logger.event("安全验证", 状态="自动", 地址=page.url)
                auto_ok = bool(
                    await solve_any_captcha(
                        page,
                        captcha_modules or [None, None],
                        appear_timeout_ms=IN_COURSE_APPEAR_TIMEOUT_MS,
                    )
                )
            if auto_ok and not await has_visible_verification(page):
                event_loop.set()
                logger.info("安全验证已自动完成.", shift=True)
                logger.event(
                    "安全验证",
                    状态="自动完成",
                    耗时=f"{time.time() - wait_start:.1f}s",
                )
                logger.event("人工介入", 状态="已解除", 原因="安全验证已自动完成")
                await asyncio.sleep(2)
                continue
            logger.warn("检测到安全验证,请手动完成验证...", shift=True)
            logger.event("安全验证", 状态="开始", 地址=page.url)
            logger.event("人工介入", 状态="需要", 原因="检测到安全验证,请手动完成验证...")
            if notifier is not None:
                await notifier.notify_verify(
                    f"检测到课中安全验证，请及时在浏览器中完成。\n页面：{page.url}"
                )
            if should_hide_window(config):
                await display_window(page)
            await wait_until_verification_hidden(page)
            event_loop.set()
            if should_hide_window(config):
                await hide_window(page)
            logger.info("安全验证已完成.", shift=True)
            logger.event("安全验证", 状态="完成", 耗时=f"{time.time() - wait_start:.1f}s")
            logger.event("人工介入", 状态="已解除", 原因="安全验证已完成")
            await asyncio.sleep(2)
        except TargetClosedError:
            logger.debug("浏览器已关闭, 安全验证模块停止运行.")
            return
        except Exception as e:
            if is_expected_polling_error(e):
                logger.debug_throttled(
                    "wait_for_verify",
                    f"安全验证模块轮询未命中(元素 .yidun_modal/.yidun_popup/tcaptcha): "
                    f"{logger.summarize_exception(e)}",
                )
            else:
                logger.log_exception("安全验证模块执行失败.", e)
            continue
