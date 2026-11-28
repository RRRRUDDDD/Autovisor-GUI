# encoding=utf-8
"""外部第三方题库客户端 (默认关闭, 严格隔离智慧树会话).

只作为答题参考注入 Prompt, 不直接决定答案; 任何网络/JSON 异常都降级为
返回 None, 绝不打断主流程. 会话内仅保留普通 User-Agent 与用户显式配置的
代理, 不携带任何智慧树 Cookie / Origin / Referer.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import time

try:
    import requests
except ImportError:  # pragma: no cover - 运行环境缺失 requests 时整体降级
    requests = None

from modules.logger import Logger
from modules.http_utils import build_requests_proxies
from modules.redaction import redact_sensitive, register_secret

logger = Logger()

DEFAULT_QUERY_URL = "https://tk.enncy.cn/query"
DEFAULT_INFO_URL = "https://tk.enncy.cn/info"
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_DELAY = 0.25
QUESTION_BANK_SCOPES = (
    "zhidao_homework",
    "zhidao_exam",
    "ai_homework",
    "ai_exam",
)
DEFAULT_QUESTION_BANK_SCOPES = ("zhidao_exam", "ai_exam")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Autovisor-QuestionBank/1.0"
)
_SENSITIVE_HEADERS = ("Cookie", "Origin", "Referer", "Authorization")
_WS_RE = re.compile(r"\s+")
_RETRYABLE_HTTP_STATUS = frozenset({429})

_TYPE_NAMES = {
    1: "single",
    2: "multiple",
    3: "completion",
    14: "judgement",
}


@dataclass(frozen=True)
class QuestionBankResult:
    """外部题库单次命中的结构化结果."""

    answer: str
    ai: bool | None = False
    remaining: int | None = None

    @property
    def source_label(self) -> str:
        if self.ai is True:
            return "题库AI生成"
        if self.ai is False:
            return "题库数据库"
        return "外部题库"


def type_name(question_type) -> str:
    """把题目类型映射为 enncy 题库约定的 type 字符串."""
    try:
        key = int(question_type)
    except (TypeError, ValueError):
        return "unknown"
    return _TYPE_NAMES.get(key, "unknown")


def _clean(value) -> str:
    return _WS_RE.sub(" ", str(value or "")).strip()


def _clean_answer_lines(text: str) -> str:
    """逐行去除题库答案末尾的 `#` 污染字符与空白行."""
    cleaned: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        while line.endswith("#"):
            line = line[:-1].strip()
        if line:
            cleaned.append(line)
    return "\n".join(cleaned)


def _extract_answer(data) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return _clean_answer_lines(data)
    if isinstance(data, bool):
        return ""
    if isinstance(data, (int, float)):
        return str(data)
    if isinstance(data, list):
        parts = [_extract_answer(item) for item in data]
        return " | ".join(part for part in parts if part)
    if isinstance(data, dict):
        for key in ("answer", "answers", "result", "content", "option", "answerText"):
            if key in data:
                text = _extract_answer(data[key])
                if text:
                    return text
    return ""


def _extract_ai_flag(payload) -> bool:
    """解析题库响应中的 `ai` 布尔字段（区分题库数据库与题库 AI 生成）."""
    if not isinstance(payload, dict):
        return False
    candidates = []
    if isinstance(payload.get("data"), dict):
        candidates.append(payload["data"])
    candidates.append(payload)
    for source in candidates:
        if "ai" not in source:
            continue
        value = source.get("ai")
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            text = value.strip().lower()
            if text in ("true", "1", "yes"):
                return True
            if text in ("false", "0", "no", ""):
                return False
    return False


def _normalize_scopes(scopes) -> frozenset[str] | None:
    if scopes is None:
        return None
    if isinstance(scopes, str):
        items = [part.strip().lower() for part in re.split(r"[\s,;]+", scopes) if part.strip()]
    elif isinstance(scopes, (list, tuple, set, frozenset)):
        items = [str(item or "").strip().lower() for item in scopes]
    else:
        return None
    return frozenset(item for item in items if item in QUESTION_BANK_SCOPES)


def _extract_remaining(payload) -> int | None:
    """从响应中读剩余次数; 缺失或非整数时返回 None (未知, 不据此跳过)."""
    if not isinstance(payload, dict):
        return None
    candidates = [payload]
    if isinstance(payload.get("data"), dict):
        candidates.append(payload["data"])
    for source in candidates:
        for key in ("times", "count", "remain", "remaining"):
            value = source.get(key)
            if isinstance(value, bool) or value is None:
                continue
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    return None


class QuestionBankClient:
    """enncy 兼容协议的外部题库客户端; token 为空时整体禁用."""

    def __init__(
        self,
        token="",
        query_url=DEFAULT_QUERY_URL,
        info_url="",
        proxy="",
        timeout=8.0,
        session=None,
        scopes=None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_delay: float = DEFAULT_RETRY_DELAY,
        sleep=None,
    ):
        self.token = _clean(token)
        if self.token:
            register_secret(self.token)
        self.query_url = _clean(query_url) or DEFAULT_QUERY_URL
        self.info_url = _clean(info_url) or self._derive_info_url(self.query_url)
        self.proxy = _clean(proxy)
        try:
            self.timeout = float(timeout)
        except (TypeError, ValueError):
            self.timeout = 8.0
        self.scopes = _normalize_scopes(scopes)
        try:
            self.max_retries = max(1, int(max_retries))
        except (TypeError, ValueError):
            self.max_retries = DEFAULT_MAX_RETRIES
        try:
            self.retry_delay = max(0.0, float(retry_delay))
        except (TypeError, ValueError):
            self.retry_delay = DEFAULT_RETRY_DELAY
        self._sleep = sleep
        self._owns_session = session is None
        self.session = session if session is not None else self._new_session()
        self._sanitize_session(self.session)
        self._remaining = None
        self._remaining_loaded = False
        self._hit_count = 0
        self._total_queries = 0

    def close(self) -> None:
        if self._owns_session and self.session is not None:
            close_fn = getattr(self.session, "close", None)
            if callable(close_fn):
                try:
                    close_fn()
                except Exception:
                    pass
            self._owns_session = False

    def __enter__(self) -> QuestionBankClient:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _redact_for_log(self, value) -> str:
        text = redact_sensitive(value)
        if self.token:
            text = text.replace(self.token, "***REDACTED***")
        return text

    # ------------------------------------------------------------- session
    @staticmethod
    def _derive_info_url(query_url: str) -> str:
        head, sep, _ = query_url.rstrip("/").rpartition("/")
        return f"{head}/info" if sep else DEFAULT_INFO_URL

    def _new_session(self):
        if requests is None:
            return None
        session = requests.Session()
        # 不读取环境里的代理/凭据; 只使用显式传入的代理
        session.trust_env = False
        return session

    @staticmethod
    def _sanitize_session(session) -> None:
        cookies = getattr(session, "cookies", None)
        if cookies is not None:
            try:
                cookies.clear()
            except Exception:
                pass
        headers = getattr(session, "headers", None)
        if headers is None:
            return
        for key in _SENSITIVE_HEADERS:
            try:
                headers.pop(key, None)
            except (AttributeError, TypeError):
                try:
                    headers[key] = ""
                except Exception:
                    pass
        try:
            headers["User-Agent"] = USER_AGENT
        except Exception:
            pass

    def _backoff(self, attempt: int) -> None:
        if self.retry_delay <= 0:
            return
        delay = min(self.retry_delay * (2 ** attempt), 2.0)
        sleep_fn = self._sleep if callable(self._sleep) else time.sleep
        sleep_fn(delay)

    def _request_json(self, url: str, params: dict):
        """返回 (payload, error); error 为 None 表示拿到了合法 JSON 响应.

        对瞬时网络异常、HTTP 5xx/429 及 JSON 解析异常执行短退避重试（最多 max_retries 次）；
        4xx 鉴权/参数错误与无效代理配置不重试，直接返回。
        """
        if self.session is None:
            return None, "disabled"
        kwargs = {"params": params, "timeout": self.timeout}
        safe_url = self._redact_for_log(url)
        try:
            proxies = build_requests_proxies(self.proxy)
        except ValueError as exc:
            logger.debug(
                f"题库代理配置无效({self._redact_for_log(self.proxy)!r}): {self._redact_for_log(exc)}"
            )
            return None, "network"
        if proxies:
            kwargs["proxies"] = proxies

        last_error = "network"
        for attempt in range(self.max_retries):
            try:
                response = self.session.get(url, **kwargs)
            except Exception as exc:
                last_error = "network"
                logger.debug(
                    f"题库请求失败({safe_url}, 尝试 {attempt + 1}/{self.max_retries}): "
                    f"{self._redact_for_log(exc)}"
                )
                if attempt + 1 < self.max_retries:
                    self._backoff(attempt)
                    continue
                return None, last_error

            status = getattr(response, "status_code", 200)
            if isinstance(status, int) and status >= 400:
                last_error = "http"
                logger.debug(
                    f"题库请求失败({safe_url}, 尝试 {attempt + 1}/{self.max_retries}): HTTP {status}"
                )
                retryable = status >= 500 or status in _RETRYABLE_HTTP_STATUS
                if retryable and attempt + 1 < self.max_retries:
                    self._backoff(attempt)
                    continue
                return None, last_error

            try:
                return response.json(), None
            except Exception as exc:
                last_error = "json"
                logger.debug(
                    f"题库响应不是合法 JSON({safe_url}, 尝试 {attempt + 1}/{self.max_retries}): "
                    f"{self._redact_for_log(exc)}"
                )
                if attempt + 1 < self.max_retries:
                    self._backoff(attempt)
                    continue
                return None, last_error
        return None, last_error

    def _get_json(self, url: str, params: dict):
        payload, _ = self._request_json(url, params)
        return payload

    # ------------------------------------------------------------ balance
    @property
    def remaining(self):
        return self._remaining

    def _ensure_remaining(self) -> None:
        if self._remaining_loaded:
            return
        self._remaining_loaded = True
        payload = self._get_json(self.info_url, {"token": self.token})
        remaining = _extract_remaining(payload)
        if remaining is not None:
            self._remaining = remaining

    def _out_of_balance(self) -> bool:
        return self._remaining is not None and self._remaining <= 0

    # -------------------------------------------------------------- query
    def enabled(self) -> bool:
        return bool(self.token) and self.session is not None

    def allows_scope(self, scope: str | None = None) -> bool:
        """检查当前客户端是否允许在指定作用域查询题库（scopes=None 表示不限制）."""
        if self.scopes is None:
            return True
        if not scope:
            return bool(self.scopes)
        return str(scope).strip().lower() in self.scopes

    def stats(self) -> tuple[int, int]:
        return self._hit_count, self._total_queries

    def probe(self) -> dict:
        """连通性自检: 只读 GET /info, 不查询题目、不消耗次数."""
        if not self.enabled():
            return {"ok": False, "message": "未配置第三方题库 Token"}
        payload, error = self._request_json(self.info_url, {"token": self.token})
        if error == "http":
            return {"ok": False, "message": "题库请求失败, 请检查查询地址或 Token"}
        if error == "network":
            return {"ok": False, "message": "题库请求失败, 请检查网络或查询地址"}
        if not isinstance(payload, dict):
            return {"ok": False, "message": "题库响应无效, 请检查查询地址"}
        code = payload.get("code")
        if code is not None:
            try:
                code_value = int(code)
            except (TypeError, ValueError):
                code_value = None
            if code_value == 0:
                message = payload.get("message") or payload.get("msg") or "Token 无效或已过期"
                return {"ok": False, "message": str(message)}
        remaining = _extract_remaining(payload)
        if remaining is None:
            return {"ok": True, "remaining": None, "message": "题库连接正常"}
        self._remaining = remaining
        self._remaining_loaded = True
        return {
            "ok": True,
            "remaining": remaining,
            "message": f"题库连接正常, 剩余 {remaining} 次",
        }

    def log_stats(self) -> None:
        if not self.enabled() or not self._total_queries:
            return
        logger.event(
            "第三方题库统计",
            命中=self._hit_count,
            查询=self._total_queries,
            剩余=self._remaining if self._remaining is not None else "未知",
        )

    def query_result(
        self, title, options=None, question_type=1, *, scope: str | None = None
    ) -> QuestionBankResult | None:
        """查询题目, 命中返回结构化结果(含 answer 与 ai 来源标识), 未命中或异常返回 None."""
        if not self.enabled():
            return None
        if not self.allows_scope(scope):
            logger.debug(f"第三方题库作用域({scope})未启用, 跳过查询.")
            return None
        text = _clean(title)
        if not text:
            return None
        self._ensure_remaining()
        if self._out_of_balance():
            logger.debug("第三方题库次数已用尽, 本轮跳过.")
            return None
        option_texts = []
        for option in options or []:
            if isinstance(option, dict):
                option = option.get("content") or option.get("text") or ""
            cleaned = _clean(option)
            if cleaned:
                option_texts.append(cleaned)
        params = {
            "token": self.token,
            "title": text,
            "options": "\n".join(option_texts),
            "type": type_name(question_type),
        }
        self._total_queries += 1
        payload = self._get_json(self.query_url, params)
        if not isinstance(payload, dict):
            return None
        try:
            code = int(payload.get("code"))
        except (TypeError, ValueError):
            return None
        if code != 1:
            # 无答案不扣次, 也不更新剩余次数
            return None
        remaining = _extract_remaining(payload)
        if remaining is not None:
            self._remaining = remaining
        answer = _extract_answer(payload.get("data"))
        if not answer:
            answer = _extract_answer(payload.get("answer"))
        if not answer:
            return None
        self._hit_count += 1
        return QuestionBankResult(
            answer=answer,
            ai=_extract_ai_flag(payload),
            remaining=self._remaining,
        )

    def query(
        self, title, options=None, question_type=1, *, scope: str | None = None
    ) -> str | None:
        """查询题目, 命中返回参考答案文本, 未命中或异常返回 None."""
        result = self.query_result(title, options, question_type, scope=scope)
        return result.answer if result is not None else None

    # -------------------------------------------------------------- factory
    @classmethod
    def from_config(cls, proxy="", session=None):
        """按 data/llm.json 中的 question_bank_* 配置构建客户端."""
        from modules import llm

        conf = llm.question_bank_conf()
        return cls(
            token=conf.get("token", ""),
            query_url=conf.get("url", DEFAULT_QUERY_URL),
            proxy=proxy,
            session=session,
            scopes=conf.get("scopes"),
        )