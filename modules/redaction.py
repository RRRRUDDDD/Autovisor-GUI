# encoding=utf-8
"""共享敏感信息脱敏模块（供 logger / notifier / question_bank / CLI 启动参数复用）。"""
from __future__ import annotations

from collections.abc import Mapping
import re
import threading
from urllib.parse import urlparse, urlsplit

# ---- 通用日志脱敏规则（预编译） ----

_REDACT_RULES = (
    (re.compile(r"sk-[A-Za-z0-9_-]{8,}"), "sk-***REDACTED***"),
    (re.compile(r"AIza[0-9A-Za-z_-]{16,}"), "AIza***REDACTED***"),
    (
        re.compile(r"(?:xai|gsk)_[A-Za-z0-9_-]{12,}|xai-[A-Za-z0-9_-]{12,}"),
        "***REDACTED***",
    ),
    (
        re.compile(r"(?i)(authorization\s*:\s*)(?:bearer\s+)?\S+"),
        r"\1***REDACTED***",
    ),
    (re.compile(r"(?im)(\b(?:set-cookie|cookie)\s*:\s*)[^\r\n]+"), r"\1***REDACTED***"),
    (re.compile(r"(?i)((?:https?|socks5h?):\/\/)[^:\s/@]+:[^@\s/]+@"), r"\1***:***@"),
    (re.compile(r"(?i)(https?:\/\/api\.day\.app\/)[^\s/?#]+"), r"\1***REDACTED***"),
    (re.compile(r"(?i)(https?:\/\/sctapi\.ftqq\.com\/)[^\s/?#]+"), r"\1***REDACTED***"),
    (re.compile(r"(?i)(https?:\/\/qmsg\.zendee\.cn\/send\/)[^\s/?#]+"), r"\1***REDACTED***"),
    (re.compile(r"(?i)(https?:\/\/api\.telegram\.org\/bot)[^\s/?#]+"), r"\1***REDACTED***"),
    (
        re.compile(
            r"""(?i)((?:["']?(?:notification[_-]?url|tg[_-]?chat[_-]?id|chat[_-]?id|bark[_-]?url|"""
            r"""pushplus[_-]?token|password|passwd|pwd|api[_-]?key|x-api-key|"""
            r"""x-goog-api-key|token|cookie|\bfp|irToken)["']?)\s*[:=]\s*["']?)([^\s,;"'}\]]+)"""
        ),
        r"\1***REDACTED***",
    ),
    (
        re.compile(r"(?i)([?&](?:token|key|apikey|api_key|access_token|fp|irToken)=)([^&\s]+)"),
        r"\1***REDACTED***",
    ),
    (re.compile(r"\b(1[3-9]\d)\d{4}(\d{4})\b"), r"\1****\2"),
)


def redact_sensitive(text) -> str:
    """脱敏 API Key / Cookie / 代理口令 / 推送地址 / URL token / 手机号及动态注册敏感值。"""
    value = str(text or "")
    for pattern, replacement in _REDACT_RULES:
        value = pattern.sub(replacement, value)
    if value:
        with _secrets_lock:
            secrets = sorted(_secrets, key=len, reverse=True)
        for secret in secrets:
            if secret in value:
                value = value.replace(secret, "***REDACTED***")
    return value


def redact_value(value):
    """按值类型安全脱敏：字符串脱敏，数字/布尔/None 保持原类型以免影响结构化事件解析。"""
    if isinstance(value, str):
        return redact_sensitive(value)
    return value


def redact_argv(argv) -> str:
    """脱敏命令行参数列表（如 --proxy http://user:pass@host），用于安全写入启动日志。"""
    if not argv:
        return "无"
    return "[" + " ".join(redact_sensitive(arg) for arg in argv) + "]"


# ---- 通知专用增强脱敏（预编译正则，修复多行贪婪吞字问题） ----

_SECRETS_LIMIT = 512
_secrets: dict[str, None] = {}
_secrets_lock = threading.RLock()

_SENSITIVE_FIELDS = (
    r"(?:password|passwd|pwd|cookies?|set-cookie|authorization|api[_-]?key|key|"
    r"tokens?|[a-z_]*token|secret|enc|aienc|username|account|_?uid|fid|cpi|"
    r"clazzid|classid|courseid|tg_chat_id|chat_id|fp|irtoken)"
)
_SENSITIVE_RE = re.compile(_SENSITIVE_FIELDS, re.I)
_ASSIGNMENT_RE = re.compile(
    rf"""(?i)(["']?\b{_SENSITIVE_FIELDS}["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;&}}\]]+)"""
)
_URL_RE = re.compile(r"""https?://[^\s<>"']+""", re.I)
_COOKIE_JAR_RE = re.compile(r"<RequestsCookieJar\[.*?\]>", re.S)
_BEARER_RE = re.compile(r"""(?i)\bBearer\s+[^\s,;"']+""")
# 仅脱敏到行尾，绝不跨行吞掉后续正文
_COOKIE_LINE_RE = re.compile(
    r"""(?im)(["']?\b(?:set-cookie|cookies?)["']?\s*[:=]\s*)[^\r\n]*"""
)
_CONTAINER_LINE_RE = re.compile(
    rf"""(?i)(["']?\b{_SENSITIVE_FIELDS}["']?\s*[:=]\s*)(?:\{{[^}}\r\n]*\}}|\[[^\]\r\n]*\]|[\{{\[][^\r\n]*)"""
)
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_API_KEY_RE = re.compile(
    r"sk-[A-Za-z0-9_-]{8,}|AIza[0-9A-Za-z_-]{16,}|(?:xai|gsk)_[A-Za-z0-9_-]{12,}|xai-[A-Za-z0-9_-]{12,}"
)
_PROXY_CRED_RE = re.compile(r"(?i)((?:https?|socks5h?):\/\/)[^:\s/@]+:[^@\s/]+@")


def register_secret(value) -> None:
    if isinstance(value, (str, int)) and len(str(value)) >= 4:
        with _secrets_lock:
            _secrets[str(value)] = None
            while len(_secrets) > _SECRETS_LIMIT:
                del _secrets[next(iter(_secrets))]


def register_config(config) -> None:
    if isinstance(config, Mapping):
        for key, value in config.items():
            if _SENSITIVE_RE.fullmatch(str(key)) or str(key).lower() in {"url", "subscription"}:
                register_secret(value)
                if str(key).lower() == "tokens" and isinstance(value, str):
                    for token in re.split(r"[,;\s]+", value):
                        register_secret(token)
            register_config(value)
    elif isinstance(config, (list, tuple)):
        for item in config:
            register_config(item)


def redact_notification(value) -> str:
    """通知正文脱敏：按行匹配敏感容器/Cookie，保留多行通知其余内容。"""
    text = str(value)
    with _secrets_lock:
        secrets = sorted(_secrets, key=len, reverse=True)
    for secret in secrets:
        text = text.replace(secret, "[redacted]")
    text = _COOKIE_JAR_RE.sub("[redacted cookies]", text)
    text = _BEARER_RE.sub("Bearer [redacted]", text)
    text = _API_KEY_RE.sub("[redacted]", text)
    text = _PROXY_CRED_RE.sub(r"\1***:***@", text)
    text = _COOKIE_LINE_RE.sub(lambda m: m[1] + "[redacted cookies]", text)
    text = _CONTAINER_LINE_RE.sub(lambda m: m[1] + "[redacted container]", text)
    text = _ASSIGNMENT_RE.sub(lambda m: m[1] + "[redacted]", text)

    def _hide_url(match):
        try:
            host = urlsplit(match[0]).hostname or "service"
            return "https://" + host + "/[redacted]"
        except ValueError:
            return "[redacted URL]"

    text = _URL_RE.sub(_hide_url, text)
    return _PHONE_RE.sub("[redacted account]", text)


def mask_secret(value: str | None) -> str:
    """脱敏 Token 或包含 Key 的推送 URL，避免敏感凭据写入日志。"""
    text = str(value or "").strip()
    if not text:
        return ""
    if "://" in text:
        parsed = urlparse(text)
        host = parsed.netloc or ""
        return f"{parsed.scheme}://{host}/***"
    if len(text) <= 8:
        return "***"
    return f"{text[:4]}***{text[-4:]}"
