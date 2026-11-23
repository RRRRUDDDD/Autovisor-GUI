# -*- coding: utf-8 -*-
"""知识点 PPT 课件文本提取 (零依赖, 仅标准库).

流程: 知识点资源列表 -> 下载 .pptx 到临时文件 -> zipfile 读取 slide XML
-> ElementTree 提取段落/表格文本 -> 删除临时文件 -> 返回可注入 Prompt 的纯文本.

旧版 .ppt/.pdf、损坏包、超限下载都会静默降级为空串, 不影响考试主流程.
"""
from __future__ import annotations

import io
import os
import re
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests

from modules.http_utils import create_retry_session
from modules.logger import Logger

logger = Logger()

NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
A_P = f"{{{NS_A}}}p"
A_T = f"{{{NS_A}}}t"
A_TBL = f"{{{NS_A}}}tbl"
A_TR = f"{{{NS_A}}}tr"
A_TC = f"{{{NS_A}}}tc"

SLIDE_PATTERN = re.compile(r"^ppt/slides/slide(\d+)\.xml$", re.IGNORECASE)
ZIP_MAGIC = b"PK\x03\x04"
MAX_DOWNLOAD_BYTES = 30 * 1024 * 1024
DOWNLOAD_TIMEOUT = 30
DEFAULT_TOTAL_CHARS = 12000
DEFAULT_PER_PPT_CHARS = 12000


def _clean_line(text: str) -> str:
    return re.sub(r"[ \t\u3000\xa0]+", " ", text or "").strip()


def _collect_slide_lines(element, lines: list[str]) -> None:
    """按文档顺序收集段落文本; 表格按行以 ' | ' 拼接单元格."""
    if element.tag == A_TBL:
        for row in element.iter(A_TR):
            cells = []
            for cell in row.findall(A_TC):
                text = "".join(node.text or "" for node in cell.iter(A_T))
                cells.append(_clean_line(text))
            if any(cells):
                lines.append(" | ".join(cells))
        return
    if element.tag == A_P:
        text = "".join(node.text or "" for node in element.iter(A_T))
        text = _clean_line(text)
        if text:
            lines.append(text)
        return
    for child in element:
        _collect_slide_lines(child, lines)


def _slide_text(root) -> str:
    lines: list[str] = []
    _collect_slide_lines(root, lines)
    return "\n".join(line for line in lines if line)


def extract_pptx_bytes(data: bytes | None) -> str:
    """从 .pptx 字节流提取文本; 非 ZIP/损坏文件返回空串."""
    if not data or not data.startswith(ZIP_MAGIC):
        return ""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return ""
    with archive:
        slides: list[tuple[int, str]] = []
        for name in archive.namelist():
            match = SLIDE_PATTERN.match(name)
            if match:
                slides.append((int(match.group(1)), name))
        slides.sort(key=lambda item: item[0])
        chunks: list[str] = []
        for number, name in slides:
            try:
                info = archive.getinfo(name)
                if info.file_size > MAX_DOWNLOAD_BYTES:
                    continue
                raw = archive.read(name)
                upper = raw.upper()
                # 拒绝带 DTD/实体声明的 slide XML, 避免实体膨胀解析
                if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
                    continue
                root = ET.fromstring(raw)
            except (ET.ParseError, KeyError, OSError):
                continue
            text = _slide_text(root)
            if text:
                chunks.append(f"[幻灯片 {number}]\n{text}")
    return "\n\n".join(chunks)


def extract_pptx(path: str | Path) -> str:
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        logger.debug(f"读取 PPT 文件失败: {exc}")
        return ""
    return extract_pptx_bytes(data)


def truncate_text(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    return text[:limit]


def _looks_like_old_office(url: str) -> bool:
    path = unquote(urlparse(url).path).lower()
    return path.endswith((".ppt", ".pdf", ".doc", ".docx"))


def download_pptx(
    url: str,
    *,
    session=None,
    proxy: str = "",
    timeout: int = DOWNLOAD_TIMEOUT,
    max_bytes: int = MAX_DOWNLOAD_BYTES,
    referer: str = "",
) -> bytes | None:
    """流式下载 PPT; 失败/超限/非 ZIP 一律返回 None."""
    if not url:
        return None
    if _looks_like_old_office(url):
        logger.debug(f"跳过非 pptx 课件: {url}")
        return None
    owns_session = session is None
    client = session or create_retry_session(proxy)
    headers = {}
    if referer:
        headers["Referer"] = referer
    response = None
    try:
        response = client.get(
            url, headers=headers, stream=True, timeout=timeout
        )
        response.raise_for_status()
        length = int(response.headers.get("Content-Length") or 0)
        if length and length > max_bytes:
            logger.debug(f"跳过超限课件 ({length} bytes): {url}")
            return None
        buffer = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            buffer.extend(chunk)
            if len(buffer) > max_bytes:
                logger.debug(f"跳过超限课件 (>{max_bytes} bytes): {url}")
                return None
        data = bytes(buffer)
        if not data.startswith(ZIP_MAGIC):
            logger.debug(f"跳过非 ZIP 课件: {url}")
            return None
        return data
    except (requests.RequestException, OSError, ValueError) as exc:
        logger.debug(f"下载课件失败: {url} ({exc})")
        return None
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()
        if owns_session:
            close_session = getattr(client, "close", None)
            if callable(close_session):
                close_session()


def download_and_extract(
    url: str,
    *,
    session=None,
    proxy: str = "",
    timeout: int = DOWNLOAD_TIMEOUT,
    max_bytes: int = MAX_DOWNLOAD_BYTES,
    referer: str = "",
    temp_dir: str | Path | None = None,
    per_ppt_char_limit: int = DEFAULT_PER_PPT_CHARS,
) -> str:
    """下载到临时文件后提取, 并保证 finally 删除临时文件."""
    data = download_pptx(
        url,
        session=session,
        proxy=proxy,
        timeout=timeout,
        max_bytes=max_bytes,
        referer=referer,
    )
    if data is None:
        return ""
    descriptor = None
    path = None
    try:
        descriptor, path = tempfile.mkstemp(
            prefix="autovisor-ppt-", suffix=".pptx", dir=temp_dir
        )
        with os.fdopen(descriptor, "wb") as file:
            descriptor = None
            file.write(data)
        return truncate_text(extract_pptx(path), per_ppt_char_limit)
    except OSError as exc:
        logger.debug(f"解析 PPT 失败: {exc}")
        return ""
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if path:
            try:
                os.remove(path)
            except OSError:
                pass


def _study_completed(item: dict, detail: dict) -> bool:
    status = detail.get("studyStatus", item.get("studyStatus"))
    try:
        return int(status) == 1
    except (TypeError, ValueError):
        return False


def _resource_url(detail: dict, item: dict) -> str:
    return (
        detail.get("resourcesUrl")
        or detail.get("resources_url")
        or item.get("resourcesUrl")
        or item.get("resources_url")
        or ""
    )


def _resource_name(detail: dict, item: dict, url: str) -> str:
    name = (
        detail.get("resourcesName")
        or detail.get("resources_name")
        or item.get("resourcesName")
        or item.get("name")
        or ""
    )
    if name:
        return str(name)
    try:
        return os.path.basename(unquote(urlparse(url).path)) or "课件"
    except ValueError:
        return "课件"


def select_ppt_resources(resources: list[dict] | None) -> list[dict]:
    """筛选 PPT 资源; 已完成 (studyStatus == 1) 排在未完成之前."""
    selected = []
    for item in resources or []:
        if not isinstance(item, dict):
            continue
        detail = item.get("resourcesDetail") or item.get("resources_detail") or {}
        if not isinstance(detail, dict):
            continue
        try:
            resource_type = int(detail.get("resourcesType", -1))
            distribute_type = int(detail.get("resourcesDistributeType", -1))
        except (TypeError, ValueError):
            continue
        url = _resource_url(detail, item)
        if resource_type != 1 or distribute_type != 4 or not url:
            continue
        selected.append(
            {
                "url": url,
                "name": _resource_name(detail, item, url),
                "completed": _study_completed(item, detail),
            }
        )
    selected.sort(key=lambda entry: not entry["completed"])
    return selected


class PptExtractor:
    """带内存缓存的提取器: 同一知识点多轮重考不重复下载解析."""

    def __init__(
        self,
        *,
        proxy: str = "",
        session=None,
        timeout: int = DOWNLOAD_TIMEOUT,
        max_bytes: int = MAX_DOWNLOAD_BYTES,
        total_char_limit: int = DEFAULT_TOTAL_CHARS,
        per_ppt_char_limit: int = DEFAULT_PER_PPT_CHARS,
        temp_dir: str | Path | None = None,
    ):
        self.proxy = proxy
        self.session = session
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.total_char_limit = max(0, int(total_char_limit))
        self.per_ppt_char_limit = max(0, int(per_ppt_char_limit))
        self.temp_dir = temp_dir
        self._cache: dict[str, str] = {}

    def clear_cache(self) -> None:
        self._cache.clear()

    def extract_url(self, url: str, *, referer: str = "") -> str:
        cached = self._cache.get(url)
        if cached is not None:
            return cached
        text = download_and_extract(
            url,
            session=self.session,
            proxy=self.proxy,
            timeout=self.timeout,
            max_bytes=self.max_bytes,
            referer=referer,
            temp_dir=self.temp_dir,
            per_ppt_char_limit=self.per_ppt_char_limit,
        )
        if text:
            self._cache[url] = text
        else:
            # 负缓存: 非 .pptx 或 404 链接不必在后续轮次反复下载
            self._cache[url] = ""
        return text

    def collect(
        self,
        resources: list[dict] | None,
        *,
        course_name: str = "",
        referer: str = "https://ai-smart-course-student-pro.zhihuishu.com/",
    ) -> list[dict]:
        """把资源列表转为 reference_materials, 受总字符预算限制."""
        del course_name  # 预留: 资源名称不含课程名时可用于提示词标注
        materials: list[dict] = []
        used = 0
        for resource in select_ppt_resources(resources):
            remaining = self.total_char_limit - used
            if remaining <= 0:
                break
            text = self.extract_url(resource["url"], referer=referer)
            if not text:
                continue
            chunk = truncate_text(text, remaining)
            if not chunk:
                continue
            materials.append({"name": resource["name"], "content": chunk})
            used += len(chunk)
        return materials