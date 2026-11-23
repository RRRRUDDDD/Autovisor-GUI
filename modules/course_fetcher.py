# encoding=utf-8
"""从智慧树学生主页自动拉取账号课程清单，并可选写回 config.ini。"""
import asyncio
import json
import os
import re
from dataclasses import asdict, dataclass
from urllib.parse import urlparse

STUDENT_HOME_URL = "https://onlineweb.zhihuishu.com/onlinestuh5"

COURSE_HOST_LABELS = (
    ("ai-smart-course-student-pro.zhihuishu.com", "AI 智慧课"),
    ("wisdom-mooc.zhihuishu.com", "AI 慕课"),
    ("fusioncourseh5.zhihuishu.com", "融合共享课"),
    ("studywisdomh5.zhihuishu.com", "翻转 / 智慧课"),
    ("hike.zhihuishu.com", "翻转 / 智慧课"),
    ("studyvideoh5.zhihuishu.com", "智慧共享课"),
)

_URL_FIELDS = (
    "studyUrl",
    "playUrl",
    "pcUrl",
    "courseUrl",
    "linkUrl",
    "videoUrl",
    "jumpUrl",
    "h5Url",
    "newCourseUrl",
    "learnUrl",
    "studentUrl",
    "coursePlayUrl",
    "detailUrl",
    "webUrl",
    "href",
    "url",
)
_TITLE_FIELDS = ("courseName", "courseTitle", "name", "title")
_TEACHER_FIELDS = (
    "teacherName",
    "speakerName",
    "mainTeacher",
    "lecturerName",
    "masterTeacherName",
    "teacherNames",
    "teacher",
)
_SCHOOL_FIELDS = ("schoolName", "collegeName", "universityName", "orgName", "school")
_PROGRESS_FIELDS = (
    "progress",
    "studyProcess",
    "studyProgress",
    "lessonProgress",
    "learnProgress",
    "studyRate",
    "progressRate",
    "completeRate",
    "progressPercent",
    "finishedPercent",
    "schedule",
    "process",
    "finishPercent",
)
_SKIP_CONTAINER_KEYWORDS = (
    "recommend",
    "hotcourse",
    "banner",
    "advert",
    "adlist",
    "guesslike",
    "popular",
    "endcourse",
    "finishcourse",
    "finishedcourse",
    "overcourse",
    "historycourse",
    "pastcourse",
    "closedcourse",
)
_GENERIC_TITLES = frozenset(
    {
        "",
        "智慧共享课",
        "融合共享课",
        "翻转 / 智慧课",
        "校内学分课",
        "AI 慕课",
        "AI 智慧课",
        "智慧树课程",
        "进入学习",
        "继续学习",
        "开始学习",
        "去学习",
        "立即学习",
        "查看详情",
        "进入课程",
        "开始上课",
    }
)
_STATIC_ASSET_EXTS = (
    ".js",
    ".css",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".webp",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".mp4",
    ".m3u8",
    ".ts",
    ".map",
)


@dataclass
class FetchedCourse:
    title: str
    url: str
    course_type: str = "智慧树课程"
    teacher: str = ""
    school: str = ""
    progress: str = ""


def normalize_course_url(url: str) -> str:
    """规范化课程链接：补全协议、统一 https，并去掉末尾空锚点。"""
    text = str(url or "").strip()
    if not text:
        return ""
    if text.startswith("//"):
        text = "https:" + text
    elif text.startswith("http://"):
        text = "https://" + text[len("http://"):]
    if text.endswith("#/"):
        text = text[:-2]
    elif text.endswith("#"):
        text = text[:-1]
    return text


def classify_course_url(url: str) -> str | None:
    """识别智慧树有效课程播放页地址，返回课程类型标签；非课程播放页返回 None。"""
    text = normalize_course_url(url)
    if not text.startswith("https://"):
        return None
    host = (urlparse(text).hostname or "").lower()
    for target_host, label in COURSE_HOST_LABELS:
        if host == target_host or host.endswith("." + target_host):
            return label
    return None


def _is_generic_title(title: str) -> bool:
    return str(title or "").strip() in _GENERIC_TITLES


def _should_skip_container_key(key: str) -> bool:
    lower = str(key or "").strip().lower()
    if not lower:
        return False
    return any(bad in lower for bad in _SKIP_CONTAINER_KEYWORDS)


def _is_truthy_flag(obj: dict, keys: tuple[str, ...]) -> bool:
    for key in keys:
        val = obj.get(key)
        if val is True or val == 1:
            return True
        if isinstance(val, str) and val.strip().lower() in ("1", "true"):
            return True
    return False


def _pick_str(obj: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return str(val)
    return ""


def _format_progress(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    if text.endswith("%"):
        return text
    try:
        num = float(text)
    except ValueError:
        return text
    if 0 <= num <= 1 and "." in text:
        return f"{round(num * 100)}%"
    if 0 <= num <= 100:
        return f"{int(num) if num.is_integer() else round(num, 1)}%"
    return text


def _build_course_from_dict(
    obj: dict,
    *,
    parent_key: str = "",
    source_url: str = "",
) -> FetchedCourse | None:
    if not isinstance(obj, dict):
        return None

    parent_lower = str(parent_key or "").lower()
    source_lower = str(source_url or "").lower()
    raw_title = _pick_str(obj, _TITLE_FIELDS)
    if len(raw_title) > 120:
        return None

    url = ""
    for field in _URL_FIELDS:
        candidate = obj.get(field)
        if isinstance(candidate, str):
            norm = normalize_course_url(candidate)
            if classify_course_url(norm):
                url = norm
                break

    has_course_marker = (
        "courseName" in obj
        or "courseTitle" in obj
        or "recruitAndCourseId" in obj
        or "courseId" in obj
        or "courseType" in obj
        or "teacherName" in obj
        or "schoolName" in obj
        or "course" in parent_lower
    )

    if not url and has_course_marker:
        secret = _pick_str(obj, ("secret", "recruitAndCourseId", "secretStr"))
        if secret and len(secret) >= 4 and not any(ch.isspace() for ch in secret):
            is_fusion_flag = _is_truthy_flag(
                obj, ("fusionCourseFlag", "isFusion", "fusionFlag")
            )
            course_type_raw = obj.get("courseType")
            try:
                course_type_num = int(str(course_type_raw).strip())
            except (TypeError, ValueError):
                course_type_num = 0
            mode_hint = " ".join(
                [
                    str(obj.get(k) or "")
                    for k in (
                        "courseType",
                        "shareType",
                        "courseModel",
                        "typeName",
                        "courseTag",
                        "courseName",
                    )
                ]
                + [parent_lower, source_lower]
            ).lower()
            is_fusion = (
                is_fusion_flag
                or course_type_num == 4
                or "fusion" in mode_hint
                or "融合" in mode_hint
            )
            if is_fusion:
                url = f"https://fusioncourseh5.zhihuishu.com/stuStudy?recruitAndCourseId={secret}"
            else:
                url = f"https://studyvideoh5.zhihuishu.com/stuStudy?recruitAndCourseId={secret}"

    if not url:
        course_id = _pick_str(obj, ("courseId", "aiCourseId"))
        class_id = _pick_str(obj, ("classId", "recruitId"))
        ai_hint = " ".join(
            [
                str(obj.get(k) or "")
                for k in ("courseType", "typeName", "courseModel")
            ]
            + [parent_lower, source_lower]
        ).lower()
        is_ai_smart = (
            bool(obj.get("aiCourseId"))
            or "ai-smart" in ai_hint
            or "aicourse" in ai_hint
            or "ai智慧" in ai_hint
            or "ai 智慧" in ai_hint
            or ("ai" in ai_hint and "aided" not in ai_hint)
        )
        if course_id and class_id and is_ai_smart:
            url = (
                "https://ai-smart-course-student-pro.zhihuishu.com/"
                f"learnPage?courseId={course_id}&classId={class_id}"
            )

    if not url and has_course_marker:
        course_id = _pick_str(obj, ("courseId",))
        pc_url_lower = _pick_str(obj, ("pcUrl", "studyUrl", "url")).lower()
        is_hike = (
            "startinng" in parent_lower
            or "hike" in parent_lower
            or "aided" in parent_lower
            or "schoolcourse" in parent_lower
            or "creditcourse" in parent_lower
            or "hikeservice.zhihuishu.com" in source_lower
            or "hike.zhihuishu.com" in source_lower
            or "/aided/" in source_lower
            or obj.get("courseNature") is not None
            or obj.get("aidedTeaching") is not None
            or "hike" in pc_url_lower
        )
        if course_id and is_hike:
            url = f"https://hike.zhihuishu.com/aidedteaching/sourceLearning/sourceLearning?courseId={course_id}"

    course_type = classify_course_url(url)
    if not course_type:
        return None

    title = raw_title if not _is_generic_title(raw_title) else course_type
    return FetchedCourse(
        title=title,
        url=url,
        course_type=course_type,
        teacher=_pick_str(obj, _TEACHER_FIELDS),
        school=_pick_str(obj, _SCHOOL_FIELDS),
        progress=_format_progress(_pick_str(obj, _PROGRESS_FIELDS)),
    )


def extract_courses_from_json(payload, *, source_url: str = "") -> list[FetchedCourse]:
    """递归从智慧树接口 JSON 响应中提取课程信息。"""
    found: list[FetchedCourse] = []

    def _walk(node, parent_key: str = "") -> None:
        if isinstance(node, dict):
            item = _build_course_from_dict(
                node, parent_key=parent_key, source_url=source_url
            )
            if item is not None:
                found.append(item)
            for key, value in node.items():
                if _should_skip_container_key(key):
                    continue
                if isinstance(value, (dict, list)):
                    _walk(value, str(key))
                elif (
                    isinstance(value, str)
                    and key in ("result", "data", "rt", "content")
                ):
                    trimmed = value.strip()
                    if (trimmed.startswith("{") and trimmed.endswith("}")) or (
                        trimmed.startswith("[") and trimmed.endswith("]")
                    ):
                        try:
                            nested = json.loads(trimmed)
                        except json.JSONDecodeError:
                            continue
                        _walk(nested, str(key))
        elif isinstance(node, list):
            for entry in node:
                if isinstance(entry, (dict, list)):
                    _walk(entry, parent_key)

    _walk(payload)
    return dedupe_courses(found)


def dedupe_courses(courses: list[FetchedCourse]) -> list[FetchedCourse]:
    """按课程 URL 去重，保留元数据更完整的一项。"""
    by_url: dict[str, FetchedCourse] = {}
    order: list[str] = []
    for item in courses:
        url = normalize_course_url(item.url)
        course_type = classify_course_url(url)
        if not course_type:
            continue
        title = item.title.strip()
        if _is_generic_title(title):
            title = item.course_type or course_type
        if url not in by_url:
            by_url[url] = FetchedCourse(
                title=title or course_type,
                url=url,
                course_type=item.course_type or course_type,
                teacher=item.teacher.strip(),
                school=item.school.strip(),
                progress=item.progress.strip(),
            )
            order.append(url)
        else:
            prev = by_url[url]
            if _is_generic_title(prev.title) and not _is_generic_title(item.title):
                prev.title = item.title.strip()
            if not prev.teacher and item.teacher.strip():
                prev.teacher = item.teacher.strip()
            if not prev.school and item.school.strip():
                prev.school = item.school.strip()
            if not prev.progress and item.progress.strip():
                prev.progress = item.progress.strip()
    return [by_url[u] for u in order]


def save_fetched_courses(courses: list[FetchedCourse], output_path: str) -> str:
    """将拉取到的课程列表保存为 JSON 文件。"""
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    data = [asdict(c) for c in courses]
    with open(output_path, "w", encoding="utf-8", newline="\n") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.write("\n")
    return output_path


def append_courses_to_config(config_path: str, courses: list[FetchedCourse]) -> int:
    """将尚未存在于 config.ini 中的课程链接追加到 [course-url] 段，返回新增条数。"""
    if not courses or not os.path.isfile(config_path):
        return 0

    raw = None
    used_encoding = "utf-8"
    for encoding in ("utf-8-sig", "gbk"):
        try:
            with open(config_path, "r", encoding=encoding) as file:
                raw = file.read()
            used_encoding = "utf-8" if encoding == "utf-8-sig" else encoding
            break
        except UnicodeDecodeError:
            continue
    if raw is None:
        return 0

    existing_urls = set(
        re.findall(r"https://[-A-Za-z0-9+&@#/%?=~_|!:,.;]+[-A-Za-z0-9+&@#/%=~_|]", raw)
    )
    new_urls = [c.url for c in courses if c.url not in existing_urls]
    if not new_urls:
        return 0

    existing_indices = [
        int(m.group(1))
        for m in re.finditer(r"(?im)^[ \t]*URL(\d+)[ \t]*=", raw)
    ]
    next_index = max(existing_indices, default=0) + 1

    # 优先复用空的 URL1 = / URL2 = 占位行，避免配置文件里留下过多空行
    lines = raw.splitlines()
    in_course_section = False
    section_end_idx = len(lines)
    empty_slot_indices: list[tuple[int, str]] = []
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_course_section:
                section_end_idx = idx
                break
            in_course_section = stripped.lower() == "[course-url]"
            continue
        if in_course_section:
            match = re.match(r"^([ \t]*URL\d+[ \t]*=[ \t]*)$", line, flags=re.IGNORECASE)
            if match:
                empty_slot_indices.append((idx, match.group(1)))

    added = 0
    remaining = list(new_urls)
    for line_idx, prefix in empty_slot_indices:
        if not remaining:
            break
        url = remaining.pop(0)
        lines[line_idx] = f"{prefix.rstrip()} {url}"
        added += 1

    if remaining:
        insert_lines = []
        if not in_course_section:
            insert_lines.append("")
            insert_lines.append("[course-url]")
        for url in remaining:
            insert_lines.append(f"URL{next_index} = {url}")
            next_index += 1
            added += 1
        lines[section_end_idx:section_end_idx] = insert_lines

    newline = "\r\n" if "\r\n" in raw else "\n"
    with open(config_path, "w", encoding=used_encoding, newline="") as file:
        file.write(newline.join(lines) + newline)
    return added


def _load_script(filename: str) -> str:
    from modules.utils import get_resource_path

    path = get_resource_path("resources", "scripts", filename)
    with open(path, "r", encoding="utf-8") as file:
        return file.read()


async def fetch_courses_from_page(
    page,
    *,
    wait_seconds: float = 5.0,
    logger=None,
) -> list[FetchedCourse]:
    """打开智慧树学生首页，从页面网络响应与 DOM/Vue 状态收集所有课程列表。"""
    captured: list[FetchedCourse] = []
    pending_tasks: list[asyncio.Task] = []

    async def _inspect_response(response) -> None:
        try:
            url = response.url or ""
            url_lower = url.lower()
            if "zhihuishu.com" not in url_lower:
                return
            path = url_lower.split("?", 1)[0]
            if any(path.endswith(ext) for ext in _STATIC_ASSET_EXTS):
                return
            status = getattr(response, "status", 200)
            if not (200 <= status < 300):
                return
            content_type = (response.headers or {}).get("content-type", "").lower()
            resource_type = ""
            try:
                resource_type = (response.request.resource_type or "").lower()
            except Exception:
                pass
            if (
                "json" not in content_type
                and "text/plain" not in content_type
                and resource_type not in ("xhr", "fetch")
            ):
                return
            raw_text = (await response.text()).strip()
            if not (raw_text.startswith("{") or raw_text.startswith("[")):
                return
            body = json.loads(raw_text)
            extracted = extract_courses_from_json(body, source_url=url)
            if extracted:
                captured.extend(extracted)
        except Exception:
            return

    def _on_response(response) -> None:
        pending_tasks.append(asyncio.create_task(_inspect_response(response)))

    page.on("response", _on_response)
    try:
        await page.goto(STUDENT_HOME_URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(int(wait_seconds * 1000))

        # 尝试切换课程分类标签页与分页，触发各类型课程懒加载接口
        try:
            await page.evaluate(_load_script("zhs_tab_switch.js"))
            await page.wait_for_timeout(1200)
        except Exception as exc:
            if logger:
                logger.debug(f"切换首页课程标签失败: {exc}")

        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)

        try:
            dom_result = await page.evaluate(_load_script("zhs_dom_extract.js"))
            if isinstance(dom_result, dict):
                dom_items = dom_result.get("courses") or []
            elif isinstance(dom_result, list):
                dom_items = dom_result
            else:
                dom_items = []
            for item in dom_items:
                norm_url = normalize_course_url(item.get("url", ""))
                course_type = classify_course_url(norm_url)
                if course_type:
                    raw_title = str(item.get("title") or "").strip()
                    captured.append(
                        FetchedCourse(
                            title=raw_title if not _is_generic_title(raw_title) else course_type,
                            url=norm_url,
                            course_type=course_type,
                            teacher=str(item.get("teacher") or "").strip(),
                            school=str(item.get("school") or "").strip(),
                            progress=_format_progress(str(item.get("progress") or "")),
                        )
                    )
        except Exception as exc:
            if logger:
                logger.debug(f"扫描首页 DOM 课程链接失败: {exc}")
    finally:
        try:
            page.remove_listener("response", _on_response)
        except Exception:
            pass

    return dedupe_courses(captured)
