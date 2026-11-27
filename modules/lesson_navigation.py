import asyncio
import math
import re
from dataclasses import dataclass

from playwright.async_api import Locator, Page, TimeoutError


@dataclass(frozen=True)
class CatalogSelectors:
    name: str
    item: str
    active: str
    finish: str
    title: str
    active_class: str
    progress: str | None = None
    progress_attribute: str | None = None
    course_title: str | None = None
    needs_expand: bool = False
    handles_ai_exercise: bool = False
    click_target: str | None = None


WISDOM_CATALOG = CatalogSelectors(
    name="wisdom",
    item=".child-info.hasvideo",
    active=".child-info.hasvideo.current",
    finish=".child-check",
    title=".child-name",
    active_class="current",
    progress="[role='progressbar'][aria-valuenow]",
    progress_attribute="aria-valuenow",
    course_title=".course-name",
)

FUSION_CATALOG = CatalogSelectors(
    name="fusion",
    # 两种融合目录都只取叶子课时。外层 chapter-item 可能包住多个小课时，
    # 不能把它的任意完成图标、首个标题或点击中心当作同一段视频。
    item=(
        ".chapter-content-second:has(.item-name)"
        ":not(:has(.chapter-content-second, .chapter-item)), "
        ".chapter-item:has(.item-name)"
        ":not(:has(.chapter-item, .chapter-content-second .item-name))"
    ),
    active=".chapter-content-second.current, .chapter-item.current",
    finish=".finish-icon",
    title=".item-name",
    active_class="current",
    # 融合课每课时带一个圆形进度环 .el-progress(在 item 内部),
    # aria-valuenow 给出真实已学百分比(缓增, 而非只到 0/100)。
    # 补上后 lesson_progress 能读到中间值, 支撑"播完未到100->回退重播"上报。
    progress=".el-progress",
    progress_attribute="aria-valuenow",
    needs_expand=True,
    handles_ai_exercise=True,
    click_target=".item-name",
)

MOOC_CATALOG = CatalogSelectors(
    name="wisdom-mooc",
    # 叶子课时才计入目录: 有 videoSmallLessons 的 .chapter-item 只是分组,
    # 真正可播的是内部 .chapter-content-second; 无子课时的 .chapter-item 本身就是课时。
    # .test-box 是章节测试入口, 不是视频课时, 故意不纳入 item。
    item=(
        ".course-catalog .chapter-item:not(:has(.chapter-content-second)), "
        ".course-catalog .chapter-content-second"
    ),
    active=(
        ".course-catalog .chapter-item.current, "
        ".course-catalog .chapter-content-second.current"
    ),
    finish=".finish-icon",
    title=".item-name",
    active_class="current",
    progress=".progress-style, .el-progress",
    progress_attribute="aria-valuenow",
    course_title=".course-catalog .course-name, .course-name",
    needs_expand=True,
    handles_ai_exercise=True,
)

HIKE_CATALOG = CatalogSelectors(
    name="hike",
    # 仅匹配视频条目(<i class="icon-video">)，排除 PPT/PDF 文档(<i class="icon-doc">等)；
    # 兼容无图标子元素的旧版或测试节点。
    item=".file-item:has(.icon-video), .file-item:not(:has(> i))",
    active=".file-item.active",
    finish=".icon-finish",
    title=".file-name, span[title]",
    active_class="active",
    progress=None,
    course_title=".course-name",
)

LEGACY_CATALOG = CatalogSelectors(
    name="legacy",
    item=".clearfix.video",
    active=".clearfix.video.current_play",
    finish=".time_icofinish",
    title="#lessonOrder",
    active_class="current_play",
    progress=".progress-num",
    course_title=".source-name",
)

CATALOGS = (
    WISDOM_CATALOG,
    MOOC_CATALOG,
    FUSION_CATALOG,
    HIKE_CATALOG,
    LEGACY_CATALOG,
)


def parse_progress_value(value) -> int:
    if isinstance(value, str):
        value = value.strip().removesuffix("%")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    if not math.isfinite(number):
        return 0
    return max(0, min(int(number), 100))


def has_class(class_attr: str | None, class_name: str) -> bool:
    return class_name in (class_attr or "").split()


def catalog_candidates(course_url: str) -> tuple[CatalogSelectors, ...]:
    if "hike.zhihuishu.com" in course_url:
        return (HIKE_CATALOG,)
    if "wisdom-mooc.zhihuishu.com" in course_url:
        return (MOOC_CATALOG, FUSION_CATALOG)
    if "fusioncourseh5" in course_url:
        return (FUSION_CATALOG, WISDOM_CATALOG, LEGACY_CATALOG)
    return (WISDOM_CATALOG, LEGACY_CATALOG, FUSION_CATALOG)


async def detect_catalog(
    page: Page, course_url: str = "", timeout_ms: int = 20_000
) -> CatalogSelectors:
    candidates = catalog_candidates(course_url or page.url)
    selector = ", ".join(catalog.item for catalog in candidates)
    try:
        await page.wait_for_selector(selector, state="attached", timeout=timeout_ms)
    except TimeoutError as exc:
        raise RuntimeError(
            f"课程目录加载超时, 未识别到新版/旧版/翻转课/AI慕课目录(尝试的选择器: {selector})"
        ) from exc

    for catalog in candidates:
        if await page.locator(catalog.item).count() > 0:
            return catalog
    raise RuntimeError(
        f"课程目录结构无法识别(已存在目录节点但未匹配: {selector})"
    )


async def lesson_progress(lesson: Locator, catalog: CatalogSelectors) -> int:
    if await lesson.locator(catalog.finish).count() > 0:
        return 100
    if not catalog.progress:
        return 0

    progress = lesson.locator(catalog.progress).first
    if await progress.count() == 0:
        return 0
    if catalog.progress_attribute:
        value = await progress.get_attribute(catalog.progress_attribute)
    else:
        value = await progress.text_content()
    return parse_progress_value(value)


async def lesson_is_complete(lesson: Locator, catalog: CatalogSelectors) -> bool:
    return await lesson_progress(lesson, catalog) >= 100


async def wait_for_lesson_completion(
    lesson: Locator, catalog: CatalogSelectors, timeout_ms: int = 5_000
) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
    while asyncio.get_running_loop().time() < deadline:
        if await lesson_is_complete(lesson, catalog):
            return True
        await asyncio.sleep(0.25)
    return False


def _is_transient_navigation_error(exc: Exception) -> bool:
    msg = str(exc or "").lower()
    return any(
        tok in msg
        for tok in (
            "execution context was destroyed",
            "cannot find context",
            "frame was detached",
            "navigating",
        )
    )


async def wait_for_lesson_active(
    lesson: Locator, catalog: CatalogSelectors, timeout_ms: int = 8_000
) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
    while asyncio.get_running_loop().time() < deadline:
        remaining_ms = max(
            200, int((deadline - asyncio.get_running_loop().time()) * 1000)
        )
        try:
            if await lesson_is_active(
                lesson, catalog, timeout_ms=min(1000, remaining_ms)
            ):
                return True
        except TimeoutError:
            pass
        except Exception as exc:
            if not _is_transient_navigation_error(exc):
                raise
        await asyncio.sleep(0.2)
    return False


async def lesson_is_active(
    lesson: Locator, catalog: CatalogSelectors, *, timeout_ms: int | None = None
) -> bool:
    script = (
        """(el, config) => el.classList.contains(config.className) ||
            Boolean(config.selector && el.querySelector(config.selector))"""
    )
    arg = {"className": catalog.active_class, "selector": catalog.active}
    if timeout_ms is not None:
        try:
            return await lesson.evaluate(script, arg, timeout=timeout_ms)
        except TypeError:
            return await lesson.evaluate(script, arg)
    return await lesson.evaluate(script, arg)


async def active_lesson(page: Page, catalog: CatalogSelectors):
    # 用同一叶子节点集解析 active，避免章节内别的小课时被误认成当前课时。
    rows = page.locator(catalog.item)
    index = await rows.evaluate_all(
        """(rows, config) => rows.findIndex(el =>
            el.classList.contains(config.className) ||
            Boolean(config.selector && el.querySelector(config.selector)))""",
        {"className": catalog.active_class, "selector": catalog.active},
    )
    return rows.nth(index) if index >= 0 else None


async def lesson_key(lesson: Locator, catalog: CatalogSelectors) -> str:
    return await lesson.evaluate(
        """(el, config) => {
            const rows = Array.from(document.querySelectorAll(config.item));
            const title = el.querySelector(config.title);
            const identity = el.getAttribute('data-id') || el.id || '';
            return JSON.stringify([rows.indexOf(el), identity,
                (title ? title.textContent : '').trim().replace(/\\s+/g, ' ')]);
        }""",
        {"item": catalog.item, "title": catalog.title},
    )


async def batch_catalog_status(
    page: Page, catalog: CatalogSelectors
) -> list[tuple[str, int]]:
    """单次 evaluate_all 批量采集目录全部课时的 (key, progress)，避免逐行 DOM 往返。"""
    raw_items = await page.locator(catalog.item).evaluate_all(
        """(rows, config) => rows.map((el, index) => {
            const title = el.querySelector(config.title);
            const identity = el.getAttribute('data-id') || el.id || '';
            const key = JSON.stringify([
                index,
                identity,
                (title ? title.textContent : '').trim().replace(/\\s+/g, ' '),
            ]);
            if (config.finish && el.querySelector(config.finish)) {
                return { key, finished: true, rawProgress: 100 };
            }
            if (!config.progress) {
                return { key, finished: false, rawProgress: 0 };
            }
            const progressEl = el.querySelector(config.progress);
            if (!progressEl) {
                return { key, finished: false, rawProgress: 0 };
            }
            const rawProgress = config.progressAttr
                ? progressEl.getAttribute(config.progressAttr)
                : progressEl.textContent;
            return { key, finished: false, rawProgress };
        })""",
        {
            "title": catalog.title,
            "finish": catalog.finish,
            "progress": catalog.progress,
            "progressAttr": catalog.progress_attribute,
        },
    )
    results: list[tuple[str, int]] = []
    for item in raw_items or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "")
        progress = 100 if item.get("finished") else parse_progress_value(item.get("rawProgress"))
        results.append((key, progress))
    return results


class LessonChanged(Exception):
    """用户或网站已切到别的课时；主循环应采用新课时，不能继续读旧进度。"""


async def ensure_lesson_unchanged(page: Page, catalog: CatalogSelectors, key: str):
    selected = await active_lesson(page, catalog)
    if selected is not None and await lesson_key(selected, catalog) != key:
        raise LessonChanged


async def click_lesson(lesson: Locator, catalog: CatalogSelectors, timeout_ms: int):
    target = lesson.locator(catalog.click_target).first if catalog.click_target else lesson
    await target.click(timeout=timeout_ms)


_LESSON_NUMBER = re.compile(r"^\s*(\d+(?:\.\d+)+)(?=[^\d.]|$)")


async def get_lesson_title(
    page: Page, lesson: Locator, catalog: CatalogSelectors
) -> str:
    # A missing label must not fall back to an unrelated title elsewhere on
    # the page (for example, the first chapter or a course-summary row).
    title_element = lesson.locator(catalog.title).first
    title = ""
    if await title_element.count():
        title = await title_element.get_attribute("title") or ""
        if not title.strip():
            title = await title_element.text_content() or ""
        title = " ".join(title.split())

    if title and catalog.name != "fusion":
        return title

    row_text = " ".join((await lesson.text_content() or "").split())
    if title:
        # Fusion renders the lesson number separately from .item-name.
        number = _LESSON_NUMBER.match(row_text)
        if number and not _LESSON_NUMBER.match(title):
            return f"{number.group(1)} {title}"
        return title
    return row_text or "当前课时"
