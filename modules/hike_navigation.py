# encoding=utf-8
"""翻转/智慧课（Hike）播放页地址解析、fileId 获取与资源页跳转回退。

智慧树翻转/智慧课（hike.zhihuishu.com）的播放页必须同时携带 `courseId` 与 `fileId`：
    https://hike.zhihuishu.com/aidedteaching/sourceLearning/sourceLearning?courseId=<cid>&fileId=<fid>
仅携带 `courseId` 访问该地址会返回 404 页面；而学生端课程入口通常会先打开
`https://wenda.zhihuishu.com/stu/courseInfo/studyResource?courseId=<cid>&polymas=1`。

本模块提供：
1. 纯函数：识别 Hike 播放页 / wenda 学习资源页链接、从资源树挑选目标视频 `fileId`；
2. 接口解析：通过 `queryResourceMenuTree`（回退 `queryLastStudyFile`）直接解析 `fileId`；
3. 浏览器回退：接口不可用时自动经由 `wenda.zhihuishu.com` 资源页读取或点击进入真实播放页。
"""
from __future__ import annotations

import asyncio
import math
import time
from urllib.parse import parse_qs, quote, urlparse

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

HIKE_PLAY_URL = "https://hike.zhihuishu.com/aidedteaching/sourceLearning/sourceLearning"
HIKE_RESOURCE_PAGE_URL = "https://wenda.zhihuishu.com/stu/courseInfo/studyResource"
STUDYRESOURCES_SSO_URL = "https://studyresources.zhihuishu.com/login/gologin"
HIKE_RESOURCE_MENU_TREE_URL = (
    "https://studyresources.zhihuishu.com/studyResources/stuResouce/queryResourceMenuTree"
)
HIKE_LAST_STUDY_FILE_URL = (
    "https://studyresources.zhihuishu.com/studyResources/stuResouce/queryLastStudyFile"
)
HIKE_STU_VIEW_FILE_URL = (
    "https://studyresources.zhihuishu.com/studyResources/stuResouce/stuViewFile"
)

_API_TIMEOUT = 15
_VIDEO_EXTS = (".mp4", ".m3u8", ".flv", ".webm", ".mov")
_VIDEO_SUFFIXES = frozenset({"mp4", "m3u8", "flv", "webm", "mov"})
# Hike 前端 newSourceLearning.js 的 switchProgress 以 Math.floor(studyTime * 1000 / totalTime / 10) >= 80 判已完成
_HIKE_FINISH_PERCENT = 80
_HIKE_404_MARKERS = ("您所访问的页面不存在", "抱歉~您所访问的页面不存在")


def build_hike_play_url(
    course_id: str, file_id: str = "", *, task_id: str = ""
) -> str:
    """构造 Hike 播放页链接；提供 file_id 时附带 &fileId=...。"""
    cid = str(course_id or "").strip()
    if not cid:
        return ""
    url = f"{HIKE_PLAY_URL}?courseId={quote(cid, safe='')}"
    fid = str(file_id or "").strip()
    if fid:
        url += f"&fileId={quote(fid, safe='')}"
    tid = str(task_id or "").strip()
    if tid:
        url += f"&taskId={quote(tid, safe='')}"
    return url


def build_hike_resource_url(course_id: str) -> str:
    """构造翻转/智慧课学习资源入口页（wenda.zhihuishu.com）链接。"""
    cid = str(course_id or "").strip()
    if not cid:
        return ""
    return f"{HIKE_RESOURCE_PAGE_URL}?courseId={quote(cid, safe='')}&polymas=1"


def parse_hike_course_url(url: str) -> dict:
    """解析 Hike 播放页或 wenda 学习资源页链接，返回 {courseId, fileId, taskId, isResourcePage}。"""
    raw = str(url or "").strip()
    if not raw:
        return {}
    try:
        parsed = urlparse(raw)
    except ValueError:
        return {}
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    is_hike_play = (
        host == "hike.zhihuishu.com" or host.endswith(".hike.zhihuishu.com")
    ) and "/sourcelearning" in path
    is_resource_page = (
        host == "wenda.zhihuishu.com" or host.endswith(".wenda.zhihuishu.com")
    ) and "/stu/courseinfo/studyresource" in path
    if not is_hike_play and not is_resource_page:
        return {}

    qs = parse_qs(parsed.query)
    course_id = str((qs.get("courseId") or [""])[0] or "").strip()
    if not course_id:
        return {}
    file_id = str((qs.get("fileId") or [""])[0] or "").strip()
    task_id = str((qs.get("taskId") or [""])[0] or "").strip()
    return {
        "courseId": course_id,
        "fileId": file_id,
        "taskId": task_id,
        "isResourcePage": is_resource_page,
    }


def needs_hike_file_id_resolution(url: str) -> bool:
    """判断链接是否属于需要解析 fileId 才能播放的翻转/智慧课地址。"""
    info = parse_hike_course_url(url)
    if not info:
        return False
    return bool(info["isResourcePage"] or not info["fileId"])


def _extract_id(*values) -> str:
    for val in values:
        if isinstance(val, bool) or val is None:
            continue
        text = str(val).strip()
        if text and text != "0":
            return text
    return ""


def _to_float(val) -> float:
    if isinstance(val, bool) or val is None:
        return 0.0
    try:
        num = float(val)
    except (TypeError, ValueError):
        return 0.0
    return num if num > 0 else 0.0


def _is_video_node(node: dict) -> bool:
    data_type = node.get("dataType")
    if data_type is not None:
        if str(data_type).strip() == "3":
            return True
    node_type = node.get("type")
    if data_type is None and node_type is not None and str(node_type).strip() == "3":
        return True
    suffix = str(node.get("suffix") or "").strip().lower().lstrip(".")
    if suffix in _VIDEO_SUFFIXES:
        return True
    name = str(node.get("fileName") or node.get("name") or "").strip().lower()
    if any(name.endswith(ext) for ext in _VIDEO_EXTS):
        return True
    return False


def _hike_node_progress_percent(node: dict) -> int:
    """按 Hike newSourceLearning.js 的 getProgress 公式计算节点进度百分比。"""
    total = _to_float(node.get("totalTime"))
    studied = _to_float(node.get("studyTime"))
    if total <= 0 or studied <= 0:
        return 0
    per = math.floor(studied * 1000 / total / 10)
    if per > 100:
        return 100
    if per == 0:
        return 1
    return per


def _is_node_finished(node: dict) -> bool:
    if str(node.get("isFinish") or node.get("studyStatus") or "").strip() == "1":
        return True
    return _hike_node_progress_percent(node) >= _HIKE_FINISH_PERCENT


def collect_hike_document_nodes(nodes) -> list[dict]:
    """从 Hike 资源树中收集尚未完成的非视频文档节点（供 stuViewFile 标记已读）。

    规则对齐 zhs HikeVideoPlayer._traverse:
    - 跳过目录节点、视频节点（dataType == 3 或视频后缀）和已完成节点；
    - dataType 为 None 时仅在存在 fileId 时视为文件（跳过无 fileId 的测验/讨论）；
    - 其他非视频类型需有有效 fileId（或 type == 1 的叶节点 id）且具备非空名称。
    """
    if isinstance(nodes, dict):
        nodes = nodes.get("rt") if isinstance(nodes.get("rt"), list) else [nodes]
    if not isinstance(nodes, list):
        return []

    docs: list[dict] = []
    seen: set[str] = set()

    def _walk(items, depth: int = 0) -> None:
        if depth > 20 or not isinstance(items, list):
            return
        for node in items:
            if not isinstance(node, dict):
                continue
            children = node.get("childList")
            if children is not None:
                if isinstance(children, list) and children:
                    _walk(children, depth + 1)
                continue
            node_type = str(node.get("type") if node.get("type") is not None else "").strip()
            if node_type == "0" and not node.get("fileId"):
                continue
            if _is_video_node(node) or _is_node_finished(node):
                continue

            data_type = node.get("dataType")
            explicit_fid = _extract_id(node.get("fileId"))
            file_name = str(node.get("fileName") or node.get("name") or "").strip()
            if data_type is None:
                if not explicit_fid:
                    continue
                fid = explicit_fid
            else:
                if explicit_fid:
                    if not file_name:
                        continue
                    fid = explicit_fid
                else:
                    if node_type != "1" or not file_name:
                        continue
                    fid = _extract_id(node.get("id"))
                    if not fid:
                        continue

            if fid in seen:
                continue
            seen.add(fid)
            docs.append(
                {
                    "fileId": fid,
                    "name": file_name or fid,
                    "dataType": data_type,
                }
            )

    _walk(nodes)
    return docs


def pick_hike_file_id_from_tree(nodes, *, review_mode: bool = False) -> str:
    """从 queryResourceMenuTree / queryResourceTree 的 rt 节点树中挑选最合适的 fileId。

    优先级：
    - 常规模式：首个未完成视频 -> 首个视频 -> 首个文件资源；
    - 复习模式：首个视频 -> 首个文件资源。
    """
    if isinstance(nodes, dict):
        nodes = nodes.get("rt") if isinstance(nodes.get("rt"), list) else [nodes]
    if not isinstance(nodes, list):
        return ""

    unfinished_videos: list[str] = []
    all_videos: list[str] = []
    all_files: list[str] = []

    def _walk(items, depth: int = 0) -> None:
        if depth > 20 or not isinstance(items, list):
            return
        for node in items:
            if not isinstance(node, dict):
                continue
            children = node.get("childList")
            if children is not None:
                if isinstance(children, list) and children:
                    _walk(children, depth + 1)
                continue
            # 空目录（type == 0 且无 fileId）不是可播放文件
            if str(node.get("type") if node.get("type") is not None else "").strip() == "0" and not node.get("fileId"):
                continue
            fid = _extract_id(node.get("fileId"), node.get("id"))
            if not fid:
                continue
            all_files.append(fid)
            if _is_video_node(node):
                all_videos.append(fid)
                if not _is_node_finished(node):
                    unfinished_videos.append(fid)

    _walk(nodes)
    if review_mode:
        candidates = all_videos or all_files
    else:
        candidates = unfinished_videos or all_videos or all_files
    return candidates[0] if candidates else ""


def pick_hike_file_id_from_last_study(payload) -> str:
    """从 queryLastStudyFile 响应中提取 fileId。"""
    if not isinstance(payload, dict):
        return ""
    rt = payload.get("rt")
    if isinstance(rt, dict):
        return _extract_id(rt.get("fileId"), rt.get("id"))
    return ""


def _has_host_cookie(session, host: str, name: str) -> bool:
    jar = getattr(session, "cookies", None)
    if jar is None:
        return False
    target = host.lower()
    for cookie in jar:
        if getattr(cookie, "name", "") != name:
            continue
        domain = str(getattr(cookie, "domain", "") or "").lstrip(".").lower()
        if domain == target or (domain and target.endswith("." + domain)):
            if getattr(cookie, "value", ""):
                return True
    return False


def _is_studyresources_auth_error(status: int, body: dict | None, *, non_json: bool = False) -> bool:
    if status in (401, 403):
        return True
    if non_json:
        return True
    if not isinstance(body, dict):
        return True
    code = body.get("code")
    st = str(body.get("status") or "").strip()
    msg = str(body.get("message") or body.get("msg") or "").lower()
    return bool(
        code in (401, 403)
        or st in ("401", "403")
        or any(tok in msg for tok in ("未登录", "unauthorized", "forbidden"))
    )


def _studyresources_get_json(
    client,
    url: str,
    params: dict,
    *,
    state: dict,
    own_client: bool = False,
) -> tuple[int, dict | None, bool]:
    """带 studyresources 单次 SSO 预热/重试的只读 GET 请求封装。"""
    headers = {
        "Origin": "https://hike.zhihuishu.com",
        "Referer": "https://hike.zhihuishu.com/",
    }

    def _ensure_sso_once() -> None:
        if state.get("refreshed"):
            return
        state["refreshed"] = True
        ensure_fn = getattr(client, "ensure_service_sso", None)
        if callable(ensure_fn):
            ensure_fn(STUDYRESOURCES_SSO_URL)

    def _once() -> tuple[int, dict | None, bool]:
        lock = getattr(client, "lock", None)
        if lock is not None:
            with lock:
                resp = client.session.get(
                    url, params=params, headers=headers, timeout=_API_TIMEOUT
                )
        else:
            resp = client.session.get(
                url, params=params, headers=headers, timeout=_API_TIMEOUT
            )
        status = int(getattr(resp, "status_code", 0) or 0)
        if status in (401, 403):
            return status, None, True
        if status != 200:
            return status, None, False
        try:
            body = resp.json()
        except ValueError:
            return status, None, True
        if not isinstance(body, dict):
            return status, None, True
        return status, body, _is_studyresources_auth_error(status, body)

    if own_client and not state.get("refreshed") and not _has_host_cookie(
        client.session, "studyresources.zhihuishu.com", "SESSION"
    ):
        _ensure_sso_once()

    status, body, need_retry = _once()
    if need_retry and not state.get("refreshed"):
        _ensure_sso_once()
        status, body, need_retry = _once()
    return status, body, need_retry


def fetch_hike_file_id_via_api(
    client,
    course_id: str,
    *,
    uuid: str = "",
    review_mode: bool = False,
    own_client: bool = False,
    sso_state: dict | None = None,
    logger=None,
) -> str:
    """通过 studyresources 官方接口查询 Hike 课程的可播放 fileId（失败返回空字符串）。"""
    cid = str(course_id or "").strip()
    if not cid or client is None:
        return ""
    state = sso_state if sso_state is not None else {"refreshed": False}

    try:
        tree_params = {"courseId": cid, "_": int(time.time() * 1000)}
        status, body, need_retry = _studyresources_get_json(
            client,
            HIKE_RESOURCE_MENU_TREE_URL,
            tree_params,
            state=state,
            own_client=own_client,
        )

        if status == 200 and isinstance(body, dict) and not need_retry:
            fid = pick_hike_file_id_from_tree(body.get("rt"), review_mode=review_mode)
            if fid:
                return fid

        if uuid:
            last_params = {
                "courseId": cid,
                "uuid": uuid,
                "dateFormate": int(time.time() * 1000),
            }
            l_status, l_body, l_retry = _studyresources_get_json(
                client,
                HIKE_LAST_STUDY_FILE_URL,
                last_params,
                state=state,
                own_client=own_client,
            )
            if l_status == 200 and isinstance(l_body, dict) and not l_retry:
                fid = pick_hike_file_id_from_last_study(l_body)
                if fid:
                    return fid
    except Exception as exc:
        if logger:
            logger.debug(f"接口解析翻转课 fileId 失败 (courseId={cid}): {exc}")
    return ""


def mark_hike_documents_read_via_api(
    client,
    course_id: str,
    *,
    nodes=None,
    own_client: bool = False,
    sso_state: dict | None = None,
    delay: float = 0.2,
    sleep=time.sleep,
    logger=None,
) -> dict:
    """通过 GET stuViewFile 将 Hike 课程中未完成的非视频文档节点标记已读。"""
    cid = str(course_id or "").strip()
    if not cid or client is None:
        return {"found": 0, "done": 0, "failed": 0}
    state = sso_state if sso_state is not None else {"refreshed": False}

    try:
        if nodes is None:
            tree_params = {"courseId": cid, "_": int(time.time() * 1000)}
            status, body, auth_err = _studyresources_get_json(
                client,
                HIKE_RESOURCE_MENU_TREE_URL,
                tree_params,
                state=state,
                own_client=own_client,
            )
            if status != 200 or not isinstance(body, dict) or auth_err:
                return {"found": 0, "done": 0, "failed": 0}
            nodes = body.get("rt")

        docs = collect_hike_document_nodes(nodes)
        if not docs:
            return {"found": 0, "done": 0, "failed": 0}

        done = 0
        failed = 0
        for doc in docs:
            fid = doc["fileId"]
            try:
                status, body, auth_err = _studyresources_get_json(
                    client,
                    HIKE_STU_VIEW_FILE_URL,
                    {"courseId": cid, "fileId": fid, "_": int(time.time() * 1000)},
                    state=state,
                    own_client=own_client,
                )
            except Exception as exc:
                failed += 1
                if logger:
                    logger.debug(f"Hike 文档标记已读失败 (fileId={fid}): {exc}")
                continue
            ok = (
                status == 200
                and isinstance(body, dict)
                and not auth_err
                and (
                    body.get("rt") is not None
                    or str(body.get("status") or body.get("code") or "").strip()
                    in ("200", "0", "1")
                )
            )
            if ok:
                done += 1
                if logger:
                    logger.info(f"Hike 文档已标记已读: {doc['name']}")
            else:
                failed += 1
                if logger:
                    logger.debug(
                        f"Hike 文档标记已读未确认 (fileId={fid}, status={status})"
                    )
            if delay > 0 and callable(sleep):
                sleep(delay)
        return {"found": len(docs), "done": done, "failed": failed}
    except Exception as exc:
        if logger:
            logger.debug(f"Hike 文档扫描/已读补齐失败 (courseId={cid}): {exc}")
        return {"found": 0, "done": 0, "failed": 0}


def _mark_hike_documents_from_cookies(
    cookies,
    course_id: str,
    *,
    proxy: str = "",
    logger=None,
) -> dict:
    from modules.zhs_api import ZhsApiClient

    if not cookies:
        return {"found": 0, "done": 0, "failed": 0}
    client = ZhsApiClient(
        cookies,
        proxy=proxy,
        total_retries=1,
        timeout=_API_TIMEOUT,
    )
    try:
        return mark_hike_documents_read_via_api(
            client,
            course_id,
            own_client=True,
            logger=logger,
        )
    finally:
        client.close()


async def complete_hike_document_nodes(
    page,
    course_url: str,
    *,
    config=None,
    logger=None,
    proxy: str = "",
) -> dict:
    """扫描 Hike 课程中的非视频文档节点并调用 stuViewFile 补齐阅读进度。"""
    if getattr(config, "reviewMode", False) is True:
        return {"found": 0, "done": 0, "failed": 0}
    info = parse_hike_course_url(course_url) or parse_hike_course_url(
        getattr(page, "url", "")
    )
    course_id = str(info.get("courseId") or "").strip()
    if not course_id:
        return {"found": 0, "done": 0, "failed": 0}
    context = getattr(page, "context", None)
    if context is None or not hasattr(context, "cookies"):
        return {"found": 0, "done": 0, "failed": 0}
    effective_proxy = proxy or str(getattr(config, "proxy", "") or "").strip()
    try:
        cookies = await context.cookies()
        if not cookies:
            return {"found": 0, "done": 0, "failed": 0}
        summary = await asyncio.to_thread(
            _mark_hike_documents_from_cookies,
            cookies,
            course_id,
            proxy=effective_proxy,
            logger=logger,
        )
    except Exception as exc:
        if logger:
            logger.debug(f"补齐 Hike 文档阅读进度失败 (courseId={course_id}): {exc}")
        return {"found": 0, "done": 0, "failed": 0}
    if logger and summary.get("found"):
        logger.info(
            f"Hike 非视频文档补齐完成: 已读 {summary['done']}/{summary['found']} 项"
            + (f", 失败 {summary['failed']} 项." if summary.get("failed") else ".")
        )
        logger.event(
            "Hike 文档补齐",
            发现=summary["found"],
            已读=summary["done"],
            失败=summary["failed"],
        )
    return summary


def _resolve_file_id_from_cookies(
    cookies,
    course_id: str,
    *,
    proxy: str = "",
    review_mode: bool = False,
    logger=None,
) -> str:
    from modules.zhs_api import ZhsApiClient, extract_caslogc_info

    if not cookies:
        return ""
    client = ZhsApiClient(
        cookies,
        proxy=proxy,
        total_retries=1,
        timeout=_API_TIMEOUT,
    )
    try:
        caslogc = extract_caslogc_info(cookies)
        uuid = str(caslogc.get("uuid") or "").strip()
        return fetch_hike_file_id_via_api(
            client,
            course_id,
            uuid=uuid,
            review_mode=review_mode,
            own_client=True,
            logger=logger,
        )
    finally:
        client.close()


async def is_hike_404_page(page) -> bool:
    """检测当前页面是否处于 Hike 404（页面不存在）提示页。"""
    try:
        body_text = await page.locator("body").inner_text(timeout=2000)
    except Exception:
        return False
    text = str(body_text or "")
    return any(marker in text for marker in _HIKE_404_MARKERS)


async def _resolve_via_wenda_page(
    page,
    course_id: str,
    resource_url: str,
    *,
    review_mode: bool = False,
    logger=None,
) -> str:
    """经由 wenda.zhihuishu.com 学习资源页获取真实 Hike 播放页 URL（失败返回空字符串）。"""
    try:
        current_info = parse_hike_course_url(getattr(page, "url", ""))
        if not (current_info.get("isResourcePage") and current_info.get("courseId") == course_id):
            try:
                await page.goto(resource_url, wait_until="domcontentloaded", timeout=30000)
            except PlaywrightTimeoutError:
                pass
        if "login" in str(getattr(page, "url", "") or "").lower():
            return ""
        try:
            await page.wait_for_selector(
                ".file-item, .continue-study-btn", state="attached", timeout=10000
            )
        except PlaywrightTimeoutError:
            pass

        # 优先在页面会话上下文内直接请求资源树接口，不依赖点击弹窗
        try:
            tree_payload = await page.evaluate(
                """async (url) => {
                    try {
                        const resp = await fetch(url, { credentials: 'include' });
                        if (!resp.ok) return null;
                        return await resp.json();
                    } catch (e) {
                        return null;
                    }
                }""",
                f"{HIKE_RESOURCE_MENU_TREE_URL}?courseId={quote(course_id, safe='')}&_={int(time.time() * 1000)}",
            )
            if isinstance(tree_payload, dict):
                fid = pick_hike_file_id_from_tree(
                    tree_payload.get("rt"), review_mode=review_mode
                )
                if fid:
                    return build_hike_play_url(course_id, fid)
        except Exception as exc:
            if logger:
                logger.debug(f"资源页内请求 queryResourceMenuTree 失败: {exc}")

        # 兜底：点击学习资源页中的视频条目或「继续学习」按钮，捕获弹出的播放页地址
        selectors = (
            [
                ".file-item:has(.zhihuishu-shipin)",
                ".last-study .continue-study-btn",
                ".file-item",
            ]
            if review_mode
            else [
                ".file-item:has(.zhihuishu-shipin):not(:has(.zhihuishu-wancheng))",
                ".file-item:has(.zhihuishu-shipin)",
                ".last-study .continue-study-btn",
                ".file-item",
            ]
        )
        context = getattr(page, "context", None)
        for sel in selectors:
            target = page.locator(sel).first
            try:
                if await target.count() == 0:
                    continue
            except Exception:
                continue

            popup = None
            try:
                if context is not None and hasattr(context, "expect_page"):
                    async with context.expect_page(timeout=6000) as popup_info:
                        await target.click(timeout=5000)
                    popup = await popup_info.value
                else:
                    await target.click(timeout=5000)
            except PlaywrightTimeoutError:
                # 可能在当前标签页跳转而非新开标签页
                pass
            except Exception as exc:
                if logger:
                    logger.debug(f"资源页点击入口 {sel} 失败: {exc}")
                continue

            if popup is not None:
                try:
                    for _ in range(25):
                        popup_url = str(getattr(popup, "url", "") or "")
                        parsed_popup = parse_hike_course_url(popup_url)
                        if parsed_popup.get("fileId"):
                            return popup_url
                        await asyncio.sleep(0.2)
                finally:
                    try:
                        await popup.close()
                    except Exception:
                        pass
            else:
                for _ in range(15):
                    cur_url = str(getattr(page, "url", "") or "")
                    parsed_cur = parse_hike_course_url(cur_url)
                    if parsed_cur.get("fileId"):
                        return cur_url
                    await asyncio.sleep(0.2)
    except Exception as exc:
        if logger:
            logger.debug(f"通过 wenda 资源页解析翻转课播放地址失败: {exc}")
    return ""


async def resolve_hike_playback_url(
    page,
    course_url: str,
    *,
    config=None,
    logger=None,
    proxy: str = "",
    review_mode: bool = False,
    force: bool = False,
) -> str:
    """将缺失 fileId 的 Hike 地址或 wenda 学习资源页解析为真实播放页 URL。

    对非翻转/智慧课或已包含 fileId 的地址（且 force=False）直接原样返回，零开销。
    """
    info = parse_hike_course_url(course_url)
    if not info:
        return course_url
    if not force and not info["isResourcePage"] and info["fileId"]:
        return course_url

    course_id = info["courseId"]
    task_id = info.get("taskId", "")
    effective_proxy = proxy or str(getattr(config, "proxy", "") or "").strip()
    effective_review = review_mode or (getattr(config, "reviewMode", False) is True)

    # 1. 优先复用浏览器 Cookie 走官方资源树接口，免除额外页面跳转
    context = getattr(page, "context", None)
    if context is not None and hasattr(context, "cookies"):
        try:
            cookies = await context.cookies()
            if cookies:
                fid = await asyncio.to_thread(
                    _resolve_file_id_from_cookies,
                    cookies,
                    course_id,
                    proxy=effective_proxy,
                    review_mode=effective_review,
                    logger=logger,
                )
                if fid:
                    resolved = build_hike_play_url(course_id, fid, task_id=task_id)
                    if logger:
                        logger.info(f"已解析翻转/智慧课播放页地址: fileId={fid}")
                    return resolved
        except Exception as exc:
            if logger:
                logger.debug(f"通过浏览器 Cookie 解析翻转课 fileId 失败: {exc}")

    # 2. 回退到 wenda.zhihuishu.com 学习资源页获取真实播放页地址
    resource_url = course_url if info["isResourcePage"] else build_hike_resource_url(course_id)
    if logger:
        logger.info("正在从翻转/智慧课学习资源页定位视频播放地址...")
    resolved = await _resolve_via_wenda_page(
        page,
        course_id,
        resource_url,
        review_mode=effective_review,
        logger=logger,
    )
    if resolved:
        if logger:
            parsed_res = parse_hike_course_url(resolved)
            logger.info(
                f"已从学习资源页跳转到翻转/智慧课播放页: fileId={parsed_res.get('fileId', '')}"
            )
        return resolved

    if logger and "login" not in str(getattr(page, "url", "") or "").lower():
        logger.warn(
            f"未能解析翻转/智慧课(courseId={course_id})的视频 fileId，将继续尝试原地址.",
            shift=True,
        )
    return course_url
