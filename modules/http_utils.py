# encoding=utf-8
"""HTTP 代理标准化、Playwright 代理构建与指数退避重试会话工具。"""
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

SUPPORTED_PROXY_SCHEMES = {"http", "https", "socks5", "socks5h"}
DEFAULT_RETRY_STATUSES = (429, 500, 502, 503, 504)
DEFAULT_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/149.0.0.0 Safari/537.36"
)


def normalize_proxy_url(proxy: str | None) -> str:
    """校验并标准化代理地址；留空返回空字符串，不支持的协议抛出 ValueError。"""
    raw = str(proxy or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    if scheme not in SUPPORTED_PROXY_SCHEMES or not parsed.hostname:
        raise ValueError(
            f"不支持的代理格式: {raw!r}，请使用 http://、https:// 或 socks5://host:port"
        )
    return raw


def build_requests_proxies(proxy: str | None) -> dict[str, str]:
    """构建 requests 可用的 proxies 字典。"""
    normalized = normalize_proxy_url(proxy)
    if not normalized:
        return {}
    return {"http": normalized, "https": normalized}


def build_playwright_proxy(proxy: str | None) -> dict[str, str] | None:
    """构建 Playwright browser.launch(proxy=...) 字典。"""
    normalized = normalize_proxy_url(proxy)
    if not normalized:
        return None
    parsed = urlparse(normalized)
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = f":{parsed.port}" if parsed.port else ""
    server = f"{parsed.scheme.lower()}://{host}{port}"
    result = {"server": server}
    if parsed.username:
        result["username"] = parsed.username
    if parsed.password:
        result["password"] = parsed.password
    return result


def create_retry_session(
    proxy: str | None = None,
    total_retries: int = 3,
    backoff_factor: float = 0.3,
    status_forcelist: tuple[int, ...] = DEFAULT_RETRY_STATUSES,
    headers: dict[str, str] | None = None,
) -> requests.Session:
    """创建带指数退避重试与可选代理的 requests.Session。"""
    session = requests.Session()
    if total_retries <= 0:
        adapter = HTTPAdapter(max_retries=0)
    else:
        retry = Retry(
            total=total_retries,
            connect=total_retries,
            read=total_retries,
            status=total_retries,
            backoff_factor=backoff_factor,
            status_forcelist=list(status_forcelist),
            allowed_methods=frozenset({"GET", "POST", "HEAD", "OPTIONS"}),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    proxies = build_requests_proxies(proxy)
    if proxies:
        session.proxies.update(proxies)
    if headers:
        session.headers.update(headers)
    return session
