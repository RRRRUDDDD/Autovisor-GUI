import asyncio
import time
from enum import Enum

from playwright.async_api import Page, TimeoutError

from modules.course_playback import elapsed_minutes, learn_lesson, review_lesson
from modules.lesson_navigation import (
    CatalogSelectors,
    detect_catalog,
    lesson_progress,
    wait_for_lesson_active,
    active_lesson,
    click_lesson,
    lesson_key,
    LessonChanged,
)
from modules.popups import dismiss_course_popups
from modules.tasks import has_visible_verification, wait_until_verification_hidden
from modules.utils import get_filtered_class, get_lesson_name, get_video_attr
from modules.video_state import COURSE_VIDEO_SELECTOR, has_valid_duration
from modules.fusion_adapter import FusionAdapter

# 课时点击需要显式限时；弹窗未关、被遮挡时应转入等待恢复，而不是长时间卡住。
LESSON_CLICK_TIMEOUT_MS = 10_000

# 目录节点在 detect_catalog 里刚确认过存在, 这里只做一次快速复核。
CATALOG_ATTACH_TIMEOUT_MS = 5_000
CATALOG_READY_TIMEOUT_MS = 20_000


class CourseOutcome(Enum):
    COMPLETED = "completed"
    TIME_LIMIT = "time_limit"
    FAILED = "failed"


async def detect_catalog_after_verification(
    page: Page, course_url: str, timeout_seconds: int = 60
) -> CatalogSelectors:
    deadline = time.monotonic() + timeout_seconds
    while True:
        if await has_visible_verification(page):
            await wait_until_verification_hidden(page)
            deadline = time.monotonic() + timeout_seconds
        remaining_ms = int(max(0, deadline - time.monotonic()) * 1000)
        if remaining_ms <= 0:
            raise RuntimeError(
                f"课程目录加载超时({timeout_seconds}秒)，且未检测到可处理的安全验证"
            )
        try:
            return await detect_catalog(
                page, course_url, timeout_ms=min(1500, remaining_ms)
            )
        except RuntimeError:
            await asyncio.sleep(1)
            continue


async def run_course(
    page: Page,
    catalog: CatalogSelectors,
    config,
    logger,
    playback_enabled,
) -> CourseOutcome:
    page.set_default_timeout(10_000)
    await page.wait_for_selector(
        catalog.item, state="attached", timeout=CATALOG_ATTACH_TIMEOUT_MS
    )
    if catalog.name == "legacy":
        # 旧版目录会先渲染视频列表, 再恢复当前课时与已学进度。
        # 列表存在不代表数据就绪, 过早筛选会把已学课时当成待学并连续切换。
        try:
            await page.wait_for_selector(
                catalog.active, state="attached", timeout=CATALOG_READY_TIMEOUT_MS
            )
        except TimeoutError:
            logger.warn("目录尚未恢复当前课时,本轮停止切课.")
            return CourseOutcome.FAILED
    # 融合共享课 / AI 慕课: 默认折叠的目录需先展开, 否则 get_filtered_class 采不全课时。
    if catalog.needs_expand:
        await FusionAdapter.expand_catalog(page)
        await page.wait_for_timeout(400)
    review_mode = getattr(config, "reviewMode", False) is True
    if review_mode:
        learning = False
        lessons = await get_filtered_class(page, catalog, include_all=True)
        logger.event(
            "目录筛选",
            模式="复习",
            待学课时=0,
            本轮课时=len(lessons),
        )
        if not lessons:
            logger.error("课程目录中没有可播放的视频课时.")
            logger.event("课程结果", 结果="无可用课时")
            return CourseOutcome.FAILED
    else:
        to_learn = await get_filtered_class(page, catalog)
        learning = True
        if not to_learn:
            all_lessons = await get_filtered_class(page, catalog, include_all=True)
            if not all_lessons:
                logger.event("目录筛选", 模式="学习", 待学课时=0, 本轮课时=0)
                logger.error("课程目录中没有可播放的视频课时.")
                logger.event("课程结果", 结果="无可用课时")
                return CourseOutcome.FAILED
            logger.event(
                "目录筛选",
                模式="跳过",
                待学课时=0,
                总课时=len(all_lessons),
            )
            logger.info(
                "当前课程全部课时已 100% 完成，跳过本门课程（如需刷习惯分请开启复习模式）.",
                shift=True,
            )
            logger.event("课程结果", 结果="已完成跳过", 课时数=len(all_lessons))
            return CourseOutcome.COMPLETED
        lessons = to_learn
        logger.event(
            "目录筛选",
            模式="学习",
            待学课时=len(to_learn),
            本轮课时=len(lessons),
        )

    start_time = time.time()
    paused_time = 0.0
    cursor = -1
    reviewed = set()
    adopted = None
    title = "当前课时"
    while not page.is_closed():
        if 0 < config.limitMaxTime <= elapsed_minutes(start_time, paused_time):
            return CourseOutcome.TIME_LIMIT
        # 每次切课重新读取目录，平台完成标记和手动选择都是当前现场状态。
        rows = await get_filtered_class(page, catalog, include_all=True)
        if not rows:
            logger.event("等待课程目录", interval=60, 结果="目录正在重载")
            playback_enabled.set()
            await asyncio.sleep(1)
            continue
        try:
            keys = [await lesson_key(row, catalog) for row in rows]
            pending = []
            for index, row in enumerate(rows):
                if learning:
                    if await lesson_progress(row, catalog) < 100:
                        pending.append(index)
                elif keys[index] not in reviewed:
                    pending.append(index)
        except TimeoutError:
            logger.event("等待课程目录", interval=60, 结果="目录节点正在更新")
            await asyncio.sleep(1)
            continue
        if adopted is None:
            if not pending:
                if (
                    not learning
                    and config.limitMaxTime > 0
                    and elapsed_minutes(start_time, paused_time) < config.limitMaxTime
                    and reviewed
                ):
                    reviewed.clear()
                    cursor = -1
                    logger.info(
                        "本轮复习已播完全部课时，未达时长上限，开始新一轮循环复习..."
                    )
                    logger.event(
                        "复习循环",
                        已学=f"{elapsed_minutes(start_time, paused_time):.1f}min",
                        时限=f"{config.limitMaxTime:g}min",
                    )
                    continue
                break
            index = next((i for i in pending if i > cursor), pending[0])
            lesson = rows[index]
            previous = await active_lesson(page, catalog)
            previous_key = await lesson_key(previous, catalog) if previous is not None else None
            requested_key = keys[index]
            playback_enabled.clear()
            active = False
            for attempt in range(2):
                try:
                    await dismiss_course_popups(page, max_rounds=3, settle_ms=200)
                    await click_lesson(lesson, catalog, LESSON_CLICK_TIMEOUT_MS)
                    active = await wait_for_lesson_active(lesson, catalog)
                except TimeoutError:
                    active = False
                if active:
                    break
                logger.event("课时激活超时", 课时=requested_key, 尝试=attempt + 1)
            if not active:
                logger.warn("课时未选中,保留页面等待手动切课.", shift=True)
                logger.event("人工介入", 状态="需要", 原因="课时未选中,保留页面等待手动切课.")
                # 重新开放播放，不能让后台任务暂停用户刚手动选好的视频。
                playback_enabled.set()
                while not page.is_closed():
                    if 0 < config.limitMaxTime <= elapsed_minutes(start_time, paused_time):
                        return CourseOutcome.TIME_LIMIT
                    if await has_visible_verification(page):
                        wait_start = time.time()
                        await wait_until_verification_hidden(page)
                        paused_time += time.time() - wait_start
                    await dismiss_course_popups(page, max_rounds=2, settle_ms=200)
                    selected = await active_lesson(page, catalog)
                    if selected is not None:
                        selected_key = await lesson_key(selected, catalog)
                        if selected_key == requested_key or selected_key != previous_key:
                            adopted = selected
                            logger.event("人工介入", 状态="已解除", 原因="已完成手动切课")
                            break
                    await asyncio.sleep(1)
                if page.is_closed():
                    return CourseOutcome.FAILED
                continue
        else:
            lesson = adopted
            adopted = None
            selected_key = await lesson_key(lesson, catalog)
            if selected_key not in keys:
                continue
            index = keys.index(selected_key)

        position = f"{index + 1}/{len(rows)}"

        await page.wait_for_timeout(1000)
        title = await get_lesson_name(page, lesson, catalog)
        logger.info(f"{'正在学习' if learning else '正在复习'}:{title}")
        initial_progress = await lesson_progress(lesson, catalog)
        logger.context(课时序号=position, 课时名称=title)
        logger.event(
            "课时开始",
            序号=position,
            标题=title,
            模式="学习" if learning else "复习",
            平台进度=f"{initial_progress}%",
        )
        lesson_start = time.time()
        page.set_default_timeout(10000)
        playback_enabled.set()
        try:
            if learning:
                added_pause, completed, reached_limit = await learn_lesson(
                    page, start_time, paused_time, lesson, catalog, config, logger
                )
            else:
                added_pause, completed, reached_limit = await review_lesson(
                    page, start_time, paused_time, config, logger,
                    current_lesson=lesson, catalog=catalog,
                )
        except LessonChanged as exc:
            paused_time += getattr(exc, "paused_time", 0.0)
            adopted = await active_lesson(page, catalog)
            logger.event("手动切课接续", 原课时=title, 原课时已完成=False)
            continue
        paused_time += added_pause

        final_progress = await lesson_progress(lesson, catalog)
        logger.event(
            "课时结果",
            序号=position,
            标题=title,
            完成=completed,
            达到时限=reached_limit,
            平台进度=f"{final_progress}%",
            本课用时=f"{time.time() - lesson_start:.0f}s",
        )
        if reached_limit:
            logger.info(f"当前课程已达时限:{config.limitMaxTime}min", shift=True)
            logger.info("即将进入下门课程!")
            logger.event("达到课程时限", 时限分钟=config.limitMaxTime, 复习模式=not learning)
            logger.event("课程结果", 结果="达到时限", 课时数=len(lessons))
            return CourseOutcome.TIME_LIMIT
        if not completed:
            if page.is_closed():
                return CourseOutcome.FAILED
            logger.warn(
                f'"{title}" 播放器暂不可用,保留页面等待恢复或手动切课.',
                shift=True,
            )
            logger.event(
                "人工介入",
                状态="需要",
                原因=f'"{title}" 播放器暂不可用,保留页面等待恢复或手动切课.',
            )
            logger.event(
                "等待播放器恢复",
                序号=position,
                平台进度=f"{final_progress}%",
            )
            while not page.is_closed():
                if 0 < config.limitMaxTime <= elapsed_minutes(start_time, paused_time):
                    return CourseOutcome.TIME_LIMIT
                if await has_visible_verification(page):
                    wait_start = time.time()
                    await wait_until_verification_hidden(page)
                    paused_time += time.time() - wait_start
                selected = await active_lesson(page, catalog)
                if selected is not None and has_valid_duration(await get_video_attr(page, "duration")):
                    adopted = selected
                    logger.event("人工介入", 状态="已解除", 原因="播放器已恢复")
                    break
                await asyncio.sleep(1)
            if page.is_closed():
                return CourseOutcome.FAILED
            continue

        cursor = index
        reviewed.add(keys[index])
        elapsed_min = round(elapsed_minutes(start_time, paused_time), 1)
        logger.info(f'"{title}" 已完成!', shift=True)
        logger.info(f"本次课程已学习:{elapsed_min:.1f} min")
        logger.event(
            "课时已完成",
            标题=title,
            模式="学习" if learning else "复习",
            已学分钟=elapsed_min,
        )

    if page.is_closed():
        return CourseOutcome.FAILED

    if learning:
        logger.info("已学完本课程全部内容!", shift=True)
        print("==" * 10)
    else:
        logger.info("本课程复习已完成!", shift=True)
        print("==" * 10)
    logger.event("课程结果", 结果="全部完成", 课时数=len(lessons))
    return CourseOutcome.COMPLETED
