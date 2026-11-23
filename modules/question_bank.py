# encoding=utf-8
"""外部第三方题库客户端 (默认关闭, 严格隔离智慧树会话).

只作为答题参考注入 Prompt, 不直接决定答案; 任何网络/JSON 异常都降级为
返回 None, 绝不打断主流程. 会话内仅保留普通 User-Agent 与用户显式配置的
代理, 不携带任何智慧树 Cookie / Origin / Referer.
"""
from __future__ import annotations

import re

try:
    import requests
except ImportError:  # pragma: no cover - 运行环境缺失 requests 时整体降级
    requests = None

from modules.logger import Logger
from modules.http_utils import build_requests_proxies

logger = Logger()

DEFAULT_QUERY_URL = "https://tk.enncy.cn/query"
DEFAULT_INFO_URL = "https://tk.enncy.cn/info"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Autovisor-QuestionBank/1.0"
)
_SENSITIVE_HEADERS = ("Cookie", "Origin", "Referer", "Authorization")
_WS_RE = re.compile(r"\s+")

_TYPE_NAMES = {
    1: "single",
    2: "multiple",
    3: "completion",
    14: "judgement",
}


def type_name(question_type) -> str:
    """把题目类型映射为 enncy 题库约定的 type 字符串."""
    try:
        key = int(question_type)
    except (TypeError, ValueError):
        return "unknown"
    return _TYPE_NAMES.get(key, "unknown")


def _clean(value) -> str:
    return _WS_RE.sub(" ", str(value or "")).strip()


def _extract_answer(data) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return data.strip()
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
    ):
        self.token = _clean(token)
        self.query_url = _clean(query_url) or DEFAULT_QUERY_URL
        self.info_url = _clean(info_url) or self._derive_info_url(self.query_url)
        self.proxy = _clean(proxy)
        try:
            self.timeout = float(timeout)
        except (TypeError, ValueError):
            self.timeout = 8.0
        self.session = session if session is not None else self._new_session()
        self._sanitize_session(self.session)
        self._remaining = None
        self._remaining_loaded = False
        self._hit_count = 0
        self._total_queries = 0

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

    def _request_json(self, url: str, params: dict):
        """返回 (payload, error); error 为 None 表示拿到了合法 JSON 响应."""
        if self.session is None:
            return None, "disabled"
        kwargs = {"params": params, "timeout": self.timeout}
        proxies = build_requests_proxies(self.proxy)
        if proxies:
            kwargs["proxies"] = proxies
        try:
            response = self.session.get(url, **kwargs)
        except Exception as exc:
            logger.debug(f"题库请求失败({url}): {exc}")
            return None, "network"
        status = getattr(response, "status_code", 200)
        if isinstance(status, int) and status >= 400:
            logger.debug(f"题库请求失败({url}): HTTP {status}")
            return None, "http"
        try:
            return response.json(), None
        except Exception as exc:
            logger.debug(f"题库响应不是合法 JSON({url}): {exc}")
            return None, "json"

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

    def query(self, title, options=None, question_type=1) -> str | None:
        """查询题目, 命中返回参考答案文本, 未命中或异常返回 None."""
        if not self.enabled():
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
        return answer

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
        )