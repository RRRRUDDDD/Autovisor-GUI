# encoding=utf-8
"""智慧树自带 AI（大明白 dmb-chat）与通用自定义大模型答题通道。"""
from __future__ import annotations

import json
import time
from contextlib import nullcontext

import requests

from modules import llm, zhidao_crypto
from modules.answer_parser import parse_answer_ids
from modules.exam_api import ExamApiError
from modules.http_utils import build_requests_proxies
from modules.llm_client import _ThinkAwareAimTracker
from modules.text_utils import strip_think_tags as _strip_think_tags

ZHIDAO_AI_CHAT = (
    "https://dmb-chat.zhihuishu.com/gateway/t/v1/completions/chat/stream"
)
ZHIDAO_AI_HEADERS = {
    "service-code": "SI_POLYMAS",
    "Accept": "text/event-stream",
    "Content-Type": "application/json",
}


class ZhidaoAnswerer:
    """智慧树自带 AI 答题通道(大明白 dmb-chat 流式接口, 优先级高于自定义大模型)."""

    def __init__(
        self,
        session: requests.Session,
        lock=None,
        timeout: int = 60,
        *,
        owns_session: bool = False,
    ):
        self.session = session
        self.lock = lock
        self.timeout = timeout
        self._owns_session = bool(owns_session)

    def close(self) -> None:
        if self._owns_session and self.session is not None:
            close_fn = getattr(self.session, "close", None)
            if callable(close_fn):
                try:
                    close_fn()
                except Exception:
                    pass
            self._owns_session = False

    def __enter__(self) -> "ZhidaoAnswerer":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @staticmethod
    def _parse_sse_payload(payload: dict) -> tuple[str, str | None, bool]:
        """解析单条 JSON 事件, 返回 (增量文本, 最终完整答案, 是否结束)."""
        code = payload.get("code")
        if code is not None and str(code) not in ("0", "200"):
            msg = payload.get("message") or payload.get("msg") or str(payload)
            raise ExamApiError(f"智慧树 AI 接口返回异常(code={code}): {msg}")

        # 新版 dmb-chat SSE 格式: {"messageType":"TEXT","message":"...","end":false/true,"context":{"answer":"..."}}
        if "message" in payload or "end" in payload or "context" in payload:
            chunk = payload.get("message") if isinstance(payload.get("message"), str) else ""
            is_end = bool(payload.get("end"))
            final_answer = None
            ctx = payload.get("context")
            if is_end and isinstance(ctx, dict) and isinstance(ctx.get("answer"), str) and ctx["answer"].strip():
                final_answer = ctx["answer"]
            return chunk, final_answer, is_end

        # 兼容 OpenAI 风格响应结构
        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0] if isinstance(choices[0], dict) else {}
            msg = first.get("message")
            if isinstance(msg, dict) and isinstance(msg.get("content"), str):
                return "", msg["content"], True
            delta = first.get("delta")
            if isinstance(delta, dict) and isinstance(delta.get("content"), str):
                return delta["content"], None, bool(first.get("finish_reason"))
        return "", None, False

    @classmethod
    def _consume_response(
        cls,
        response,
        *,
        aim_start: str = "```answer",
        aim_end: str = "```",
        timeout: float = 60.0,
    ) -> str:
        chunks: list[str] = []
        final_answer: str | None = None
        parsed_any = False
        deadline = time.monotonic() + max(float(timeout), 0.001)
        tracker = _ThinkAwareAimTracker(aim_start, aim_end)

        if hasattr(response, "iter_lines"):
            raw_lines = response.iter_lines()
        else:
            raw_lines = (getattr(response, "text", "") or "").splitlines()

        for raw in raw_lines:
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"智慧树 AI 流式响应超时: 总时长超过 {float(timeout):g} 秒"
                )
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
            line = line.strip()
            if not line or line.startswith(":"):
                continue
            if line.startswith("data:"):
                line = line[5:].strip()
            if not line or line == "[DONE]":
                continue
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            parsed_any = True
            chunk, final, is_end = cls._parse_sse_payload(payload)
            if chunk:
                chunks.append(chunk)
                if tracker.feed(chunk):
                    break
            if final:
                final_answer = final
            if is_end:
                break

        content = _strip_think_tags(final_answer or "".join(chunks)).strip()
        if not content:
            if not parsed_any:
                raise ValueError("智慧树 AI 返回格式无法解析或为空")
            raise ValueError("智慧树 AI 未返回有效回答内容")
        return content

    def chat(self, prompt: str) -> str:
        data = {
            "terminalType": 1,
            "question": prompt,
            "relationType": 1,
            "timestamp": int(time.time() * 1000),
        }
        url, body = zhidao_crypto.sign_zhidao_ai(ZHIDAO_AI_CHAT, data)
        post_kwargs = {
            "json": body,
            "headers": dict(ZHIDAO_AI_HEADERS),
            "timeout": self.timeout,
        }
        if isinstance(self.session, requests.Session):
            post_kwargs["stream"] = True
        with (self.lock or nullcontext()):
            response = self.session.post(url, **post_kwargs)
        try:
            response.raise_for_status()
            return self._consume_response(response, timeout=self.timeout)
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()

    def answer(self, prompt: str) -> list:
        return parse_answer_ids(self.chat(prompt))


class LlmAnswerer:
    """复用 data/llm.json 的通用大模型通道（支持 OpenAI/Claude/Gemini/Azure/Ollama 等）, 作为智慧树 AI 失败后的兜底."""

    def __init__(self, proxy: str = ""):
        self.proxy = proxy
        self.proxies = build_requests_proxies(proxy)

    def answer(self, prompt: str) -> list:
        if not llm.enabled():
            raise ValueError("未配置可用的大模型接口")
        content = llm.chat_sync(prompt, json_mode=False, proxy=self.proxy, stream=True)
        return parse_answer_ids(content)


DeepSeekAnswerer = LlmAnswerer
