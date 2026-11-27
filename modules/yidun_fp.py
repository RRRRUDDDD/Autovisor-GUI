# encoding=utf-8
"""易盾 IRisk 设备指纹（fp）被动采集与纯内存会话态管理。

- 仅保存在内存会话态，绝不落盘到配置文件或 Cookie 文件；
- 捕获到合法 fp 后立即注册到 ``modules.redaction``，严防日志与推送泄露；
- 在 Playwright / CDP 会话中通过 ``context.on("request", ...)`` 被动拦截
  ``c.dun.163.com/api/v3/get`` 的 ``fp`` 参数，并支持从页面存储兜底探测。
"""
from __future__ import annotations

import re
import threading
from urllib.parse import parse_qs, quote, unquote, urlsplit

from modules.logger import Logger
from modules.redaction import register_secret

FP_MIN_LEN = 150
FP_MAX_LEN = 260
YIDUN_GET_HOST = "c.dun.163.com"
YIDUN_GET_PATH = "/api/v3/get"

_FP_TAIL_RE = re.compile(r":\d{13}$")
_MAX_CONTEXT_ENTRIES = 32

_PROBE_STORAGE_JS = """() => {
    const found = [];
    const seen = new Set();
    const push = (val) => {
        if (typeof val !== 'string') return;
        const trimmed = val.trim();
        if (trimmed.length < 150 || trimmed.length > 360) return;
        if (!/:\\d{13}$/.test(trimmed)) return;
        if (seen.has(trimmed)) return;
        seen.add(trimmed);
        found.push(trimmed);
    };
    const inspect = (raw) => {
        if (typeof raw !== 'string') return;
        push(raw);
        if (raw.length >= 150 && raw.length <= 4096 && (raw[0] === '{' || raw[0] === '[')) {
            try {
                const parsed = JSON.parse(raw);
                if (parsed && typeof parsed === 'object') {
                    for (const value of Object.values(parsed)) {
                        if (typeof value === 'string') push(value);
                    }
                }
            } catch (_) {}
        }
    };
    for (const storeName of ['localStorage', 'sessionStorage']) {
        try {
            const store = window[storeName];
            if (!store) continue;
            for (let i = 0; i < store.length && found.length < 20; i++) {
                const key = store.key(i);
                if (!key) continue;
                inspect(store.getItem(key));
            }
        } catch (_) {}
    }
    return found;
}"""

logger = Logger()


def normalize_fp(text: str | None) -> str:
    """去掉空白、换行与包裹引号，不做其它改写。"""
    return (
        str(text or "")
        .strip()
        .strip('"')
        .strip("'")
        .replace("\r", "")
        .replace("\n", "")
    )


def validate_fp_format(fp: str) -> list[str]:
    """校验易盾 IRisk fp 格式；返回问题列表，空列表表示合法。"""
    problems: list[str] = []
    if not fp:
        problems.append("fp 为空")
        return problems
    if not (FP_MIN_LEN <= len(fp) <= FP_MAX_LEN):
        problems.append(f"长度异常: {len(fp)}（预期约 190）")
    if not _FP_TAIL_RE.search(fp):
        problems.append("尾段不是 :13位毫秒时间戳")
    if "%" in fp:
        problems.append("疑似未做 decodeURIComponent（含 %xx）")
    return problems


def is_valid_fp(fp: str | None) -> bool:
    """判断 fp 归一化后是否满足 IRisk 格式约束。"""
    normalized = normalize_fp(fp)
    return bool(normalized) and not validate_fp_format(normalized)


def _register_fp_secrets(fp: str, raw_fp: str | None = None) -> None:
    """向脱敏模块注册 fp 的明文、原始查询串与 URL 编码形态。"""
    register_secret(fp)
    if raw_fp and raw_fp != fp:
        register_secret(raw_fp)
    encoded = quote(fp, safe="")
    if encoded and encoded != fp:
        register_secret(encoded)


def _extract_raw_query_param(query: str, name: str) -> str | None:
    for pair in query.split("&"):
        if not pair:
            continue
        key, sep, value = pair.partition("=")
        if sep and key == name and value:
            return value
    return None


def extract_fp_from_url(url: str) -> tuple[str | None, str | None]:
    """从 ``c.dun.163.com/api/v3/get`` 请求 URL 中提取 ``(fp, raw_fp)``。"""
    if not url or YIDUN_GET_HOST not in url:
        return None, None
    try:
        parsed = urlsplit(str(url))
    except ValueError:
        return None, None
    if (parsed.hostname or "").lower() != YIDUN_GET_HOST:
        return None, None
    if parsed.path.rstrip("/") != YIDUN_GET_PATH:
        return None, None

    raw_fp = _extract_raw_query_param(parsed.query, "fp")
    values = parse_qs(parsed.query, keep_blank_values=False).get("fp") or []
    candidate = normalize_fp(values[0] if values else (unquote(raw_fp) if raw_fp else ""))
    if "%" in candidate:
        candidate = normalize_fp(unquote(candidate))
    if not is_valid_fp(candidate):
        return None, None
    return candidate, raw_fp


class YidunFpStore:
    """纯内存易盾 fp 存储（线程安全，按浏览器上下文隔离并保留最近会话值）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_context: dict[int, str] = {}
        self._latest: str | None = None

    def set(
        self,
        fp: str | None,
        *,
        context=None,
        raw_fp: str | None = None,
    ) -> bool:
        normalized = normalize_fp(fp)
        if not is_valid_fp(normalized):
            return False
        _register_fp_secrets(normalized, raw_fp=raw_fp)
        with self._lock:
            self._latest = normalized
            if context is not None:
                key = id(context)
                self._by_context.pop(key, None)
                self._by_context[key] = normalized
                while len(self._by_context) > _MAX_CONTEXT_ENTRIES:
                    del self._by_context[next(iter(self._by_context))]
        return True

    def get(self, *, context=None) -> str | None:
        with self._lock:
            if context is not None:
                value = self._by_context.get(id(context))
                if value:
                    return value
            return self._latest

    def clear(self, *, context=None) -> None:
        with self._lock:
            if context is None:
                self._by_context.clear()
                self._latest = None
                return
            removed = self._by_context.pop(id(context), None)
            if removed and self._latest == removed and removed not in self._by_context.values():
                self._latest = None


_DEFAULT_STORE = YidunFpStore()


def get_session_fp(context=None) -> str | None:
    """读取当前内存会话中已捕获的易盾 fp；未捕获返回 None。"""
    return _DEFAULT_STORE.get(context=context)


def set_session_fp(fp: str | None, *, context=None, raw_fp: str | None = None) -> bool:
    """写入合法易盾 fp 到内存会话态并立即注册日志脱敏。"""
    return _DEFAULT_STORE.set(fp, context=context, raw_fp=raw_fp)


def clear_session_fp(context=None) -> None:
    """清理内存会话态中的易盾 fp。"""
    _DEFAULT_STORE.clear(context=context)


class YidunFpCollector:
    """Playwright / CDP 易盾 fp 被动采集器（仅只读监听 request 事件）。"""

    def __init__(
        self,
        *,
        context=None,
        page=None,
        store: YidunFpStore | None = None,
    ) -> None:
        self.context = context
        self.page = page
        self.store = store or _DEFAULT_STORE
        self._target = None
        self._attached = False

    @property
    def attached(self) -> bool:
        return self._attached

    def attach(self) -> "YidunFpCollector":
        if self._attached:
            return self
        target = None
        if self.context is not None and callable(getattr(self.context, "on", None)):
            target = self.context
        elif self.page is not None and callable(getattr(self.page, "on", None)):
            target = self.page
        if target is None:
            return self
        try:
            target.on("request", self._on_request)
        except Exception as exc:
            logger.debug(f"挂载易盾 fp 被动监听失败: {exc}")
            return self
        self._target = target
        self._attached = True
        logger.debug("易盾 fp 被动拦截监听已挂载.")
        return self

    def detach(self, *, clear_store: bool = False) -> None:
        if self._attached and self._target is not None:
            remover = getattr(self._target, "remove_listener", None)
            if callable(remover):
                try:
                    remover("request", self._on_request)
                except Exception as exc:
                    logger.debug(f"卸载易盾 fp 被动监听失败: {exc}")
        self._target = None
        self._attached = False
        if clear_store:
            self.store.clear(context=self.context)

    def _on_request(self, request) -> None:
        try:
            url = getattr(request, "url", "") or ""
            fp, raw_fp = extract_fp_from_url(url)
            if not fp:
                return
            changed = self.store.get(context=self.context) != fp
            if self.store.set(fp, context=self.context, raw_fp=raw_fp) and changed:
                logger.debug(f"已被动捕获易盾设备指纹 fp (len={len(fp)})")
        except Exception:
            return


async def probe_fp_from_page(
    page,
    *,
    store: YidunFpStore | None = None,
    context=None,
) -> str | None:
    """兜底探测页面 localStorage/sessionStorage 中已存在的易盾 fp（不抛出异常）。"""
    active_store = store or _DEFAULT_STORE
    effective_ctx = context if context is not None else getattr(page, "context", None)
    existing = active_store.get(context=effective_ctx)
    if existing:
        return existing

    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return None
    try:
        candidates = await evaluate(_PROBE_STORAGE_JS)
    except Exception:
        return None
    if not isinstance(candidates, (list, tuple)):
        return None
    for item in candidates:
        fp = normalize_fp(item)
        if active_store.set(fp, context=effective_ctx):
            logger.debug(f"已从页面存储提取易盾设备指纹 fp (len={len(fp)})")
            return fp
    return None


__all__ = [
    "FP_MAX_LEN",
    "FP_MIN_LEN",
    "YIDUN_GET_HOST",
    "YIDUN_GET_PATH",
    "YidunFpCollector",
    "YidunFpStore",
    "clear_session_fp",
    "extract_fp_from_url",
    "get_session_fp",
    "is_valid_fp",
    "normalize_fp",
    "probe_fp_from_page",
    "set_session_fp",
    "validate_fp_format",
]
