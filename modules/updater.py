# encoding=utf-8
import html
import os
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET
from urllib.parse import quote, unquote, urlparse

import requests

from modules.http_utils import create_retry_session
from modules.version import __gui_version__, __version__

REPO = "RRRRUDDDD/Autovisor-GUI"
LATEST_RELEASE_API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASE_PAGE_URL = f"https://github.com/{REPO}/releases/latest"
RELEASES_ATOM_URL = f"https://github.com/{REPO}/releases.atom"
EXPANDED_ASSETS_URL_TEMPLATE = f"https://github.com/{REPO}/releases/expanded_assets/{{tag}}"
REQUEST_TIMEOUT = 5
USER_AGENT = f"Autovisor/{__version__}"
REQUEST_HEADERS = {
    "Accept": "application/vnd.github+json",
    "User-Agent": USER_AGENT,
}
WEB_JSON_HEADERS = {
    "Accept": "application/json",
    "User-Agent": USER_AGENT,
}
WEB_PAGE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "User-Agent": USER_AGENT,
}

# 发行包命名形如 Autovisor-3.18.0-windows-amd64.zip
_VERSION_PATTERN = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_RELEASE_TAG_URL_PATTERN = re.compile(r"/releases/tag/([^?#]+)")
_ASSET_HREF_PATTERN = re.compile(r'href="([^"]*/releases/download/[^"]+)"', re.IGNORECASE)
_WINDOWS_ASSET_KEYWORD = "windows-amd64"
_RATE_LIMIT_STATUSES = (403, 429)
_DEFAULT_RATE_LIMIT_COOLDOWN = 300
_MAX_RATE_LIMIT_COOLDOWN = 3600
_API_RETRY_STATUSES = (500, 502, 503, 504)

# 按代理维度记录 GitHub REST API 限流冷却截止时间，避免重复撞 403
_API_RATE_LIMIT_UNTIL: dict[str, float] = {}
_RATE_LIMIT_LOCK = threading.Lock()

# 只展示本仓库 GitHub Releases 官方 HTTPS 链接, 避免被中间人或恶意外链替换
_TRUSTED_HOSTS = ("github.com",)
_REPO_PATH_SEGMENTS = tuple(seg.lower() for seg in REPO.split("/") if seg)


class _NoReleaseFoundError(RuntimeError):
    """仓库可访问，但尚未发布可用的 Release。"""


class _RateLimitFallbackError(RuntimeError):
    """GitHub REST API 受限，且网页备用通道也未能获取更新信息。"""


def parse_version(text):
    """从 "Autovisor-3.18.0" 之类的文本中提取 (major, minor, patch)。"""
    if not text:
        return None
    match = _VERSION_PATTERN.search(str(text))
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def format_version(version):
    return ".".join(str(part) for part in version)


def is_newer(latest, current):
    """latest 是否比 current 新; 任一无法解析时视为无更新。"""
    if latest is None or current is None:
        return False
    return latest > current


def is_trusted_url(url):
    """链接是否为本仓库 GitHub Releases 官方 HTTPS 地址（强校验 /{REPO}/releases/ 前缀）。"""
    parsed = urlparse(str(url or ""))
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    if host not in _TRUSTED_HOSTS:
        return False
    raw_path = unquote(parsed.path or "")
    segments = [seg for seg in raw_path.split("/") if seg]
    if any(seg in (".", "..") for seg in segments):
        return False
    lowered = [seg.lower() for seg in segments]
    prefix_len = len(_REPO_PATH_SEGMENTS)
    return (
        len(lowered) >= prefix_len + 1
        and tuple(lowered[:prefix_len]) == _REPO_PATH_SEGMENTS
        and lowered[prefix_len] == "releases"
    )


def pick_download_url(release, platform=None):
    """按当前平台匹配安装包直链, 未命中返回 None(由调用方引导至发行页)。"""
    assets = release.get("assets") or []
    is_win = (platform == "win32") if platform is not None else (sys.platform == "win32" or os.name == "nt")
    plat = platform or sys.platform

    if is_win:
        # 优先匹配 Windows 安装包 (.exe 且含 setup 或 windows-x64)
        for asset in assets:
            name = str(asset.get("name") or "").lower()
            url = asset.get("browser_download_url")
            if name.endswith(".exe") and ("setup" in name or "windows-x64" in name) and is_trusted_url(url):
                return url
        # 其次匹配便携包 (windows-amd64 / windows-x64)
        for asset in assets:
            name = str(asset.get("name") or "").lower()
            url = asset.get("browser_download_url")
            if (_WINDOWS_ASSET_KEYWORD in name or "windows-x64" in name) and is_trusted_url(url):
                return url
        return None

    if plat == "darwin":
        for asset in assets:
            name = str(asset.get("name") or "").lower()
            url = asset.get("browser_download_url")
            if name.endswith(".dmg") and is_trusted_url(url):
                return url
        return None

    if plat.startswith("linux"):
        for suffix in (".appimage", ".deb"):
            for asset in assets:
                name = str(asset.get("name") or "").lower()
                url = asset.get("browser_download_url")
                if name.endswith(suffix) and is_trusted_url(url):
                    return url
        return None

    return None


def _session_get(client, url, *, timeout, headers):
    return client.get(url, timeout=timeout, headers=headers)


def _proxy_cache_key(proxy) -> str:
    return str(proxy or "").strip()


def _is_api_rate_limited(proxy) -> bool:
    key = _proxy_cache_key(proxy)
    with _RATE_LIMIT_LOCK:
        until = _API_RATE_LIMIT_UNTIL.get(key, 0.0)
        if until <= 0:
            return False
        if time.time() >= until:
            _API_RATE_LIMIT_UNTIL.pop(key, None)
            return False
        return True


def _record_api_rate_limit(proxy, response) -> int | None:
    now = time.time()
    headers = getattr(response, "headers", None) or {}
    raw_reset = headers.get("X-RateLimit-Reset") or headers.get("x-ratelimit-reset")
    wait_seconds = None
    if raw_reset is not None:
        try:
            reset_ts = float(raw_reset)
            delta = int(reset_ts - now)
            if delta > 0:
                wait_seconds = min(delta, _MAX_RATE_LIMIT_COOLDOWN)
        except (TypeError, ValueError):
            wait_seconds = None

    cooldown = max(wait_seconds or _DEFAULT_RATE_LIMIT_COOLDOWN, 60)
    with _RATE_LIMIT_LOCK:
        _API_RATE_LIMIT_UNTIL[_proxy_cache_key(proxy)] = now + min(
            cooldown, _MAX_RATE_LIMIT_COOLDOWN
        )
    return wait_seconds


def _extract_tag_from_url(url) -> str:
    if not url or not is_trusted_url(url):
        return ""
    match = _RELEASE_TAG_URL_PATTERN.search(urlparse(str(url)).path or "")
    if not match:
        return ""
    return unquote(match.group(1)).strip("/")


def _html_to_text(raw_html: str) -> str:
    if not raw_html:
        return ""
    text = str(raw_html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "- ", text)
    text = re.sub(r"(?i)</(p|div|li|h[1-6]|tr|ul|ol|pre|blockquote)>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    cleaned = []
    for line in (item.strip() for item in text.splitlines()):
        if line or (cleaned and cleaned[-1] != ""):
            cleaned.append(line)
    return "\n".join(cleaned).strip()


def _parse_expanded_assets(raw_html: str) -> list[dict[str, str]]:
    if not raw_html:
        return []
    assets = []
    seen = set()
    for raw_href in _ASSET_HREF_PATTERN.findall(str(raw_html)):
        href = html.unescape(raw_href).strip()
        if href.startswith("/"):
            full_url = f"https://github.com{href}"
        else:
            full_url = href
        if not is_trusted_url(full_url) or full_url in seen:
            continue
        seen.add(full_url)
        filename = unquote(urlparse(full_url).path.rsplit("/", 1)[-1])
        if filename:
            assets.append({"name": filename, "browser_download_url": full_url})
    return assets


def _parse_release_from_atom(raw_xml: str, tag_name: str) -> dict[str, str]:
    if not raw_xml or not tag_name:
        return {}
    try:
        root = ET.fromstring(raw_xml)
    except ET.ParseError:
        return {}

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    encoded_tag = quote(tag_name, safe="")
    target_suffixes = (f"/releases/tag/{tag_name}", f"/releases/tag/{encoded_tag}")

    for entry in root.findall("atom:entry", ns):
        matched = False
        for link in entry.findall("atom:link", ns):
            href = str(link.attrib.get("href") or "")
            if any(href.endswith(suffix) for suffix in target_suffixes):
                matched = True
                break
        if not matched:
            entry_id = str(entry.findtext("atom:id", default="", namespaces=ns) or "")
            if entry_id.endswith(f"/{tag_name}") or entry_id.endswith(f"/{encoded_tag}"):
                matched = True
        if not matched:
            continue

        title = str(entry.findtext("atom:title", default="", namespaces=ns) or "").strip()
        updated = str(entry.findtext("atom:updated", default="", namespaces=ns) or "").strip()
        content = str(entry.findtext("atom:content", default="", namespaces=ns) or "")
        return {
            "name": title,
            "published_at": updated,
            "body": _html_to_text(content),
        }
    return {}


def _fetch_latest_release_from_web(client, timeout=REQUEST_TIMEOUT):
    """通过 GitHub 网页端 releases/latest 获取最新版本（不受 api.github.com 60次/小时 限流影响）。"""
    response = _session_get(
        client,
        RELEASE_PAGE_URL,
        timeout=timeout,
        headers=WEB_JSON_HEADERS,
    )
    response.raise_for_status()

    final_url = str(getattr(response, "url", "") or "")
    if final_url and not is_trusted_url(final_url):
        raise RuntimeError("更新检查跳转至非 GitHub 官方域名，已中止")

    tag_name = ""
    try:
        payload = response.json()
    except Exception:
        payload = None
    if isinstance(payload, dict) and payload.get("tag_name"):
        tag_name = str(payload.get("tag_name") or "").strip()

    if not tag_name:
        tag_name = _extract_tag_from_url(final_url)

    if not tag_name:
        raise _NoReleaseFoundError("未找到可用的发行版本（Release）")

    tag_url = f"https://github.com/{REPO}/releases/tag/{quote(tag_name, safe='')}"
    page_url = final_url if (final_url and "/releases/tag/" in final_url) else tag_url
    if not is_trusted_url(page_url):
        page_url = RELEASE_PAGE_URL

    release = {
        "tag_name": tag_name,
        "name": tag_name,
        "html_url": page_url,
        "body": "",
        "published_at": "",
        "assets": [],
    }

    enrich_timeout = min(timeout, 4)
    try:
        assets_url = EXPANDED_ASSETS_URL_TEMPLATE.format(tag=quote(tag_name, safe=""))
        assets_resp = _session_get(
            client,
            assets_url,
            timeout=enrich_timeout,
            headers=WEB_PAGE_HEADERS,
        )
        assets_resp.raise_for_status()
        release["assets"] = _parse_expanded_assets(getattr(assets_resp, "text", ""))
    except Exception:
        pass

    try:
        atom_resp = _session_get(
            client,
            RELEASES_ATOM_URL,
            timeout=enrich_timeout,
            headers=WEB_PAGE_HEADERS,
        )
        atom_resp.raise_for_status()
        atom_meta = _parse_release_from_atom(getattr(atom_resp, "text", ""), tag_name)
        if atom_meta.get("name"):
            release["name"] = atom_meta["name"]
        if atom_meta.get("published_at"):
            release["published_at"] = atom_meta["published_at"]
        if atom_meta.get("body"):
            release["body"] = atom_meta["body"]
    except Exception:
        pass

    return release


def check_update_info(
    current_version=__gui_version__, timeout=REQUEST_TIMEOUT, session=None, proxy=""
) -> dict:
    """查询 GitHub 最新 Release 并返回结构化更新信息（供桌面端 API 使用）。

    网络、限流、证书校验失败或返回数据不可解析时抛出可读异常（不再降级关闭 TLS 校验）。
    """
    current = parse_version(current_version)
    try:
        release = fetch_latest_release(
            timeout=timeout, session=session, proxy=proxy
        )
    except _NoReleaseFoundError as exc:
        raise RuntimeError(str(exc)) from exc
    except _RateLimitFallbackError as exc:
        raise RuntimeError(str(exc)) from exc
    except requests.exceptions.SSLError as exc:
        raise RuntimeError(
            f"检查更新证书校验失败，请检查系统时间、根证书或代理设置: {exc}"
        ) from exc
    except requests.exceptions.HTTPError as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in _RATE_LIMIT_STATUSES:
            raise RuntimeError("GitHub API 请求受限（可能触发频率限制），请稍后再试或配置代理") from exc
        if status == 404:
            raise RuntimeError("未找到可用的发行版本（Release）") from exc
        raise RuntimeError(f"检查更新请求失败（HTTP {status or '错误'}）: {exc}") from exc
    except Exception as exc:
        raise RuntimeError(f"检查更新失败，请检查网络或代理设置: {exc}") from exc

    if not isinstance(release, dict):
        raise RuntimeError("检查更新返回数据格式异常")

    latest = parse_version(release.get("tag_name")) or parse_version(release.get("name"))
    page_url = release.get("html_url")
    if not is_trusted_url(page_url):
        page_url = RELEASE_PAGE_URL
    download_url = pick_download_url(release)
    return {
        "currentVersion": str(current_version),
        "latestVersion": format_version(latest) if latest else "",
        "hasUpdate": is_newer(latest, current),
        "releaseName": str(release.get("name") or release.get("tag_name") or ""),
        "releaseUrl": page_url,
        "downloadUrl": download_url or "",
        "notes": str(release.get("body") or "")[:2000],
        "publishedAt": str(release.get("published_at") or ""),
    }


def fetch_latest_release(timeout=REQUEST_TIMEOUT, session=None, proxy=""):
    own_session = session is None
    client = session or create_retry_session(
        proxy=proxy,
        total_retries=2,
        backoff_factor=0.2,
        status_forcelist=_API_RETRY_STATUSES,
    )
    try:
        if own_session and _is_api_rate_limited(proxy):
            return _fetch_latest_release_from_web(client, timeout=timeout)

        response = _session_get(
            client,
            LATEST_RELEASE_API,
            timeout=timeout,
            headers=REQUEST_HEADERS,
        )
        try:
            response.raise_for_status()
        except requests.exceptions.HTTPError as api_exc:
            resp_obj = getattr(api_exc, "response", None) or response
            status = getattr(resp_obj, "status_code", None)
            if status not in _RATE_LIMIT_STATUSES:
                raise
            wait_seconds = (
                _record_api_rate_limit(proxy, resp_obj)
                if own_session
                else None
            )
            try:
                return _fetch_latest_release_from_web(client, timeout=timeout)
            except (_NoReleaseFoundError, requests.exceptions.SSLError):
                raise
            except requests.exceptions.HTTPError as web_http_exc:
                web_status = getattr(
                    getattr(web_http_exc, "response", None), "status_code", None
                )
                if web_status == 404:
                    raise _NoReleaseFoundError(
                        "未找到可用的发行版本（Release）"
                    ) from web_http_exc
                hint = (
                    f"（约 {max(1, (wait_seconds + 59) // 60)} 分钟后重置）"
                    if wait_seconds
                    else ""
                )
                raise _RateLimitFallbackError(
                    f"GitHub API 请求受限{hint}，且备用通道访问失败（HTTP {web_status or '错误'}），请稍后再试或检查代理设置"
                ) from web_http_exc
            except Exception as web_exc:
                hint = (
                    f"（约 {max(1, (wait_seconds + 59) // 60)} 分钟后重置）"
                    if wait_seconds
                    else ""
                )
                raise _RateLimitFallbackError(
                    f"GitHub API 请求受限{hint}，且备用通道访问失败，请稍后再试或检查代理设置: {web_exc}"
                ) from web_exc
        return response.json()
    finally:
        if own_session:
            client.close()


def check_for_update(
    logger, current_version=__version__, timeout=REQUEST_TIMEOUT, session=None, proxy=""
):
    """查询最新 Release, 对比当前版本并打印结果。

    3.18.1 起 tag 使用版本号; 旧版 release 的 tag 是日期(2026/9/15),
    因此优先解析 tag_name, 失败再回退到 release 名称。
    始终严格校验 TLS 证书，绝不静默降级关闭证书校验；下载链接仅展示本仓库 GitHub Releases 官方地址。
    检查失败(断网、限流、证书错误等)只记录 debug 日志, 不影响程序运行。
    """
    current = parse_version(current_version)
    try:
        release = fetch_latest_release(timeout=timeout, session=session, proxy=proxy)
    except requests.exceptions.SSLError as exc:
        logger.debug(f"证书校验失败，已中止更新检查: {logger.summarize_exception(exc)}")
        logger.event("更新检查", 结果="失败", 原因="证书校验失败", 错误=logger.summarize_exception(exc))
        return None
    except Exception as exc:
        logger.debug(f"检查更新失败: {logger.summarize_exception(exc)}")
        logger.event("更新检查", 结果="失败", 错误=logger.summarize_exception(exc))
        return None

    latest = parse_version(release.get("tag_name")) or parse_version(release.get("name"))
    if latest is None:
        logger.debug("无法解析最新版本号, 跳过更新检查.")
        return None
    if not is_newer(latest, current):
        logger.info(f"当前版本{current_version},已是最新版本.")
        logger.event("更新检查", 当前=current_version, 结果="已是最新")
        return None

    page_url = release.get("html_url")
    if not is_trusted_url(page_url):
        page_url = RELEASE_PAGE_URL
    download_url = pick_download_url(release)
    logger.info(f"检测到新版本 {format_version(latest)} (当前 {current_version}), 请前往更新!")
    logger.info(f"更新说明与下载: {page_url}")
    logger.event(
        "更新检查",
        当前=current_version,
        最新=format_version(latest),
        结果="发现新版本",
        下载页=page_url,
    )
    if download_url:
        logger.info(f"安装包直链: {download_url}")
    return page_url
