# encoding=utf-8
"""智慧树官方免费 AI 解析接口 (明文 JSON + SSE).

流程: GET user/info 取 data.userId -> POST question/analysis/thread/run
逐行读取 SSE 中 choices[0].message.content, 直到 stop == true.
"""
from __future__ import annotations

import json

import requests

from modules.http_utils import build_requests_proxies

AI_BASE = "https://ai-course-assistant-api.zhihuishu.com"
USER_INFO_URL = f"{AI_BASE}/api/v1/user/info"
ANALYSIS_RUN_URL = f"{AI_BASE}/api/v1/question/analysis/thread/run"

REQUEST_TIMEOUT = 20
ANALYSIS_TIMEOUT = 120


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def _flag(value) -> bool:
    return str(value).strip().lower() in {"1", "true"}


def _as_int(value):
    text = str(value or "").strip()
    return int(text) if text.isdigit() else value


class AiAnalysisApi:
    """封装官方 AI 解析, 失败时返回空串由调用方静默跳过."""

    def __init__(self, session=None, proxy: str = ""):
        self.session = session or requests.Session()
        self.proxy = proxy

    def _proxies(self):
        if not self.proxy:
            return None
        try:
            return build_requests_proxies(self.proxy) or None
        except ValueError:
            return None

    def get_user_id(self, course_id="", recruit_id="") -> str:
        params = {"userId": 0, "courseId": str(course_id), "recruitId": str(recruit_id)}
        try:
            response = self.session.get(
                USER_INFO_URL, params=params, timeout=REQUEST_TIMEOUT,
                proxies=self._proxies(),
            )
            response.raise_for_status()
            payload = response.json()
        except Exception:
            return ""
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return ""
        return _text(data.get("userId"))

    def analyze(
        self,
        question: dict | None = None,
        *,
        question_id="",
        user_id="",
        course_id="",
        recruit_id="",
        timeout: float = ANALYSIS_TIMEOUT,
    ) -> str:
        if not question_id and isinstance(question, dict):
            question_id = question.get("question_id") or question.get("id") or ""
        body = {
            "courseId": str(course_id),
            "recruitId": str(recruit_id),
            "userRole": "STUDENT",
            "userId": int(user_id) if str(user_id).isdigit() else 0,
            "threadId": "",
            "questionId": _as_int(question_id),
            "regenerate": False,
            "runId": None,
        }
        response = None
        try:
            response = self.session.post(
                ANALYSIS_RUN_URL,
                json=body,
                timeout=timeout,
                stream=True,
                headers={"Accept": "text/event-stream"},
                proxies=self._proxies(),
            )
            response.raise_for_status()
            return self._read_sse(response)
        except Exception:
            return ""
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

    @staticmethod
    def _read_sse(response) -> str:
        parts: list[str] = []
        try:
            # 不使用 requests 的自动解码: 缺省 ISO-8859-1 会让中文乱码.
            iterator = iter(response.iter_lines(decode_unicode=False))
        except Exception:
            return ""
        for raw_line in iterator:
            if isinstance(raw_line, bytes):
                raw_line = raw_line.decode("utf-8", "replace")
            line = _text(raw_line)
            if not line or not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if not payload:
                continue
            if payload == "[DONE]":
                break
            try:
                data = json.loads(payload)
            except (ValueError, TypeError):
                continue
            if not isinstance(data, dict):
                continue
            choices = data.get("choices") or []
            choice = choices[0] if choices and isinstance(choices[0], dict) else {}
            message = choice.get("message") or {}
            content = message.get("content")
            if content is None:
                content = (choice.get("delta") or {}).get("content")
            if content:
                parts.append(str(content))
            if _flag(data.get("stop")) or _flag(choice.get("stop")):
                break
        return "".join(parts).strip()