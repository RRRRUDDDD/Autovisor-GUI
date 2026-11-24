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
# 数字序号只在明确的分隔符后剥离; "1." 必须跟空白, 避免把 "1.5" 剥成 "5".
_OPTION_PREFIX_RE = re.compile(
    r"^\s*(?:[A-Ha-h]\s*[.、．:：)）]|[（(]\s*[A-Ha-h]\s*[)）]|\d+\s*[、．)）:：]|\d+\.\s+)\s*"
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
        self._records: list[dict] = []
        self._pending_tasks: set[asyncio.Task] = set()
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
        for task in list(self._pending_tasks):
            task.cancel()
        self._pending_tasks.clear()

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
        # 事件循环只保存 Task 弱引用, 必须自行持有强引用直到完成.
        task = loop.create_task(self._consume(response))
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

    async def _consume(self, response) -> None:
        try:
            payload = await response.json()
        except Exception as exc:
            logger.debug(f"弹题接口响应解析失败: {exc}")
            return
        self.ingest(payload)

    # --------------------------------------------------------------- parsing
    @classmethod
    def _extract(cls, payload) -> list[dict]:
        if not isinstance(payload, dict):
            return []
        data = payload.get("data")
        if not isinstance(data, dict):
            return []
        dtos = data.get("lessonTestQuestionUseInterfaceDtos")
        if not isinstance(dtos, list):
            return []
        records: list[dict] = []
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
            title = normalize_option_text(test.get("name") or dto.get("name") or "")
            options = test.get("questionOptions") or dto.get("questionOptions") or []
            all_texts: list[str] = []
            correct: list[str] = []
            for option in options:
                if not isinstance(option, dict):
                    continue
                raw = (
                    option.get("content")
                    or option.get("optionContent")
                    or option.get("option")
                    or ""
                )
                text = normalize_option_text(raw)
                if not text:
                    continue
                if text not in all_texts:
                    all_texts.append(text)
                if str(option.get("result")) == "1" and text not in correct:
                    correct.append(text)
            if correct:
                records.append(
                    {
                        "question_id": str(question_id) if question_id != "" else "",
                        "title": title,
                        "options": tuple(all_texts),
                        "correct": tuple(correct),
                    }
                )
        return records

    def ingest(self, payload) -> int:
        """解析接口响应并写入缓存, 返回本次缓存的正确答案条数."""
        entries = self._extract(payload)
        now = self._clock()
        added = 0
        with self._lock:
            self._purge(now)
            for entry in entries:
                question_id = entry["question_id"]
                scoped = (
                    self._by_question.setdefault(question_id, {})
                    if question_id
                    else None
                )
                correct_keys: set[str] = set()
                for text in entry["correct"]:
                    for key in {text, strip_option_prefix(text)}:
                        if not key:
                            continue
                        correct_keys.add(key)
                        self._global[key] = now + self.ttl
                        if scoped is not None:
                            scoped[key] = now + self.ttl
                added += len(entry["correct"])
                option_keys = frozenset(
                    key
                    for key in (
                        strip_option_prefix(text) or text
                        for text in entry["options"]
                    )
                    if key
                )
                record = {
                    "question_id": question_id,
                    "title": entry["title"],
                    "option_keys": option_keys,
                    "correct": frozenset(correct_keys),
                    "expires_at": now + self.ttl,
                }
                if question_id:
                    self._records = [
                        item
                        for item in self._records
                        if item["question_id"] != question_id
                    ]
                self._records.append(record)
        if added:
            logger.debug(f"课中弹题接口已缓存 {added} 条标准答案.")
        return added

    # ------------------------------------------------------------ matching
    @staticmethod
    def _correct_indices(record: dict, dom_keys: list[str]) -> list[int]:
        return [
            index
            for index, key in enumerate(dom_keys)
            if key in record["correct"]
        ]

    @staticmethod
    def _resolve(records: list[dict], dom_keys: list[str]):
        """单条命中返回下标; 多条命中且答案冲突时返回空列表安全回退."""
        results = []
        for record in records:
            indices = PopupAnswerInterceptor._correct_indices(record, dom_keys)
            if indices:
                results.append(tuple(indices))
        if not results:
            return None
        first = results[0]
        if all(item == first for item in results):
            return list(first)
        return []

    @staticmethod
    def _title_matches(record_title: str, target: str) -> int:
        """2 为精确匹配, 1 为包含匹配, 0 为不匹配."""
        if not record_title or not target:
            return 0
        if record_title == target:
            return 2
        if target in record_title or record_title in target:
            return 1
        return 0

    def match_choices(
        self,
        option_texts: list[str],
        *,
        question_id: str = "",
        question_title: str = "",
    ) -> list[int]:
        """按题号/题干/选项集合匹配当前弹题, 返回应点击的选项下标.

        同一弹窗可能包含多道题, 只有唯一或答案一致的记录才会返回下标;
        答案冲突时返回空列表, 交由 AI 兜底, 绝不跨题污染.
        """
        dom_keys = [
            strip_option_prefix(normalize_option_text(text)) or normalize_option_text(text)
            for text in option_texts
        ]
        if not dom_keys:
            return []
        now = self._clock()
        with self._lock:
            self._purge(now)
            records = list(self._records)
        if not records:
            return []

        result: list[int] | None = None
        if question_id not in (None, ""):
            matched = [
                record
                for record in records
                if record["question_id"] == str(question_id)
            ]
            result = self._resolve(matched, dom_keys)

        if result is None:
            title = normalize_option_text(question_title)
            if title:
                exact = [
                    record
                    for record in records
                    if self._title_matches(record["title"], title) == 2
                ]
                result = self._resolve(exact, dom_keys)
                if result is None:
                    dom_set = set(dom_keys)
                    contained = [
                        record
                        for record in records
                        if self._title_matches(record["title"], title) == 1
                        and dom_set <= record["option_keys"]
                    ]
                    result = self._resolve(contained, dom_keys)

        if result is None:
            dom_set = set(dom_keys)
            subset = [record for record in records if dom_set <= record["option_keys"]]
            result = self._resolve(subset, dom_keys)

        matched_indices = result or []
        if matched_indices:
            with self._lock:
                self.hit_count += 1
        return matched_indices

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
        self._records = [
            record for record in self._records if record["expires_at"] > now
        ]
        for key in [key for key, expiry in self._global.items() if expiry <= now]:
            del self._global[key]
        for question_id in list(self._by_question):
            table = self._by_question[question_id]
            for key in [key for key, expiry in table.items() if expiry <= now]:
                del table[key]
            if not table:
                del self._by_question[question_id]