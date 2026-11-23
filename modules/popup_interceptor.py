# encoding=utf-8
"""课中弹题接口只读拦截.

播放知到共享课/旧版课程时, 浏览器会请求 popupAnswer/lessonPopupExam 并拿到
服务端标注好的正确答案 (questionOptions[].result == "1"). 本模块只监听响应,
绝不伪造或篡改请求, 命中后可直接点选, 零 LLM 成本且 100% 准确.
"""
from __future__ import annotations

import asyncio
import re
import threading
import time

from modules.html_utils import extract_plain_text
from modules.logger import Logger

logger = Logger()

POPUP_ANSWER_ENDPOINT = "popupAnswer/lessonPopupExam"
DEFAULT_TTL_SECONDS = 600.0
_OPTION_PREFIX_RE = re.compile(
    r"^\s*(?:[A-Ha-h]\s*[.、．:：)）]|[（(]\s*[A-Ha-h]\s*[)）]|\d+\s*[.、．)）])\s*"
)
_WS_RE = re.compile(r"\s+")


def normalize_option_text(text) -> str:
    """HTML 清洗 + 空白折叠, 用于选项文本比对."""
    plain = extract_plain_text(text) or str(text or "")
    return _WS_RE.sub(" ", plain).strip()


def strip_option_prefix(text: str) -> str:
    """去掉 "A." / "(B)" / "1、" 之类的序号前缀."""
    return _OPTION_PREFIX_RE.sub("", text, count=1).strip()


class PopupAnswerInterceptor:
    """监听 lessonPopupExam 响应, 缓存标准答案供 DOM 点选比对."""

    endpoint = POPUP_ANSWER_ENDPOINT

    def __init__(self, page, *, ttl: float = DEFAULT_TTL_SECONDS, clock=time.monotonic):
        self.page = page
        self.ttl = float(ttl)
        self._clock = clock
        self._lock = threading.Lock()
        self._global: dict[str, float] = {}
        self._by_question: dict[str, dict[str, float]] = {}
        self._attached = False
        self.hit_count = 0

    # ------------------------------------------------------------- lifecycle
    def attach(self) -> "PopupAnswerInterceptor":
        if self._attached:
            return self
        self.page.on("response", self._on_response)
        self._attached = True
        logger.debug("课中弹题接口拦截已挂载.")
        return self

    def detach(self) -> None:
        if not self._attached:
            return
        try:
            self.page.remove_listener("response", self._on_response)
        except Exception as exc:
            logger.debug(f"移除弹题响应监听失败: {exc}")
        finally:
            self._attached = False

    @property
    def attached(self) -> bool:
        return self._attached

    def _on_response(self, response) -> None:
        try:
            url = getattr(response, "url", "") or ""
        except Exception:
            return
        if self.endpoint not in url:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._consume(response))

    async def _consume(self, response) -> None:
        try:
            payload = await response.json()
        except Exception as exc:
            logger.debug(f"弹题接口响应解析失败: {exc}")
            return
        self.ingest(payload)

    # --------------------------------------------------------------- parsing
    @classmethod
    def _extract(cls, payload) -> list[tuple[str, list[str]]]:
        if not isinstance(payload, dict):
            return []
        data = payload.get("data")
        if not isinstance(data, dict):
            return []
        dtos = data.get("lessonTestQuestionUseInterfaceDtos")
        if not isinstance(dtos, list):
            return []
        results: list[tuple[str, list[str]]] = []
        for dto in dtos:
            if not isinstance(dto, dict):
                continue
            test = (
                dto.get("testQuestion")
                if isinstance(dto.get("testQuestion"), dict)
                else {}
            )
            question_id = (
                test.get("questionId")
                or dto.get("questionId")
                or dto.get("id")
                or ""
            )
            options = test.get("questionOptions") or dto.get("questionOptions") or []
            texts: list[str] = []
            for option in options:
                if not isinstance(option, dict):
                    continue
                if str(option.get("result")) != "1":
                    continue
                raw = (
                    option.get("content")
                    or option.get("optionContent")
                    or option.get("option")
                    or ""
                )
                text = normalize_option_text(raw)
                if text and text not in texts:
                    texts.append(text)
            if texts:
                results.append((str(question_id) if question_id != "" else "", texts))
        return results

    def ingest(self, payload) -> int:
        """解析接口响应并写入缓存, 返回本次缓存的正确答案条数."""
        entries = self._extract(payload)
        now = self._clock()
        added = 0
        with self._lock:
            self._purge(now)
            for question_id, texts in entries:
                scoped = (
                    self._by_question.setdefault(question_id, {})
                    if question_id
                    else None
                )
                for text in texts:
                    for key in {text, strip_option_prefix(text)}:
                        if not key:
                            continue
                        self._global[key] = now + self.ttl
                        if scoped is not None:
                            scoped[key] = now + self.ttl
                    added += 1
        if added:
            logger.debug(f"课中弹题接口已缓存 {added} 条标准答案.")
        return added

    # --------------------------------------------------------------- lookups
    def is_correct(self, text, question_id=None) -> bool:
        target = normalize_option_text(text)
        if not target:
            return False
        keys = [key for key in (target, strip_option_prefix(target)) if key]
        now = self._clock()
        with self._lock:
            self._purge(now)
            table = self._global
            if question_id not in (None, ""):
                scoped = self._by_question.get(str(question_id))
                if scoped:
                    table = scoped
            hit = any(key in table for key in keys)
        if hit:
            self.hit_count += 1
        return hit

    def correct_texts(self) -> list[str]:
        now = self._clock()
        with self._lock:
            self._purge(now)
            return sorted(self._global)

    def _purge(self, now: float) -> None:
        for key in [key for key, expiry in self._global.items() if expiry <= now]:
            del self._global[key]
        for question_id in list(self._by_question):
            table = self._by_question[question_id]
            for key in [key for key, expiry in table.items() if expiry <= now]:
                del table[key]
            if not table:
                del self._by_question[question_id]