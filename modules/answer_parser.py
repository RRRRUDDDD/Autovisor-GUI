# encoding=utf-8
"""跨答题引擎共享的题型枚举与模型回复解析器（```answer``` 块 / JSON / 字母 / 填空）。"""
from __future__ import annotations

import ast
import json
import re
from enum import IntEnum

from modules.html_utils import extract_plain_text
from modules.text_utils import normalize_text, strip_think_tags

_ANSWER_BLOCK_RE = re.compile(r"```answer\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_LIST_RE = re.compile(r"\[.*\]", re.DOTALL)
_LETTER_TOKEN_RE = re.compile(r"^[A-H](?:[A-H])*$", re.IGNORECASE)
_LETTER_ANSWER_RE = re.compile(
    r"(?:答案|正确选项|选择|应选|选)\s*(?:是|为)?\s*[:：]?\s*([A-Ha-h]{1,8})"
)
_FILL_RE = re.compile(r"FILL\s*[:：]\s*(.+)", re.IGNORECASE | re.DOTALL)
_LETTERS = "ABCDEFGH"


class QuestionType(IntEnum):
    SINGLE = 1
    MULTIPLE = 2
    FILL = 3
    JUDGE = 14


QUESTION_TYPE_NAMES: dict[int, str] = {
    int(QuestionType.SINGLE): "单选题",
    int(QuestionType.MULTIPLE): "多选题",
    int(QuestionType.FILL): "填空题",
    int(QuestionType.JUDGE): "判断题",
}


def answer_separator(question_type) -> str:
    """填空题多空用 '/' 拼接，其余题型沿用 '#@#'。"""
    try:
        return "/" if int(question_type) == QuestionType.FILL else "#@#"
    except (TypeError, ValueError):
        return "#@#"


def extract_answer_block(text: str | None) -> str:
    """去除思考标签后提取 ```answer ... ``` 代码块内容；无代码块时返回清理后原文。"""
    cleaned = strip_think_tags(text)
    if not cleaned:
        return ""
    match = _ANSWER_BLOCK_RE.search(cleaned)
    return match.group(1).strip() if match else cleaned


def parse_json_list(text: str | None, *, allow_literal_eval: bool = False) -> list | None:
    """从 ```answer``` 块或裸文本中解析 JSON 列表（可选回退 ast.literal_eval）。"""
    raw = strip_think_tags(text)
    if not raw:
        return None
    block = _ANSWER_BLOCK_RE.search(raw)
    candidates = [block.group(1).strip()] if block else []
    candidates.append(raw)
    for candidate in candidates:
        for snippet in (candidate,):
            data = _try_parse_list(snippet, allow_literal_eval=allow_literal_eval)
            if isinstance(data, list):
                return data
            match = _LIST_RE.search(snippet)
            if match:
                data = _try_parse_list(
                    match.group(0), allow_literal_eval=allow_literal_eval
                )
                if isinstance(data, list):
                    return data
    return None


def _try_parse_list(snippet: str, *, allow_literal_eval: bool = False):
    try:
        return json.loads(snippet)
    except (ValueError, TypeError):
        if allow_literal_eval:
            try:
                return ast.literal_eval(snippet)
            except (ValueError, SyntaxError):
                return None
        return None


def parse_answer_ids(text: str | None) -> list:
    """从 ```answer``` 代码块或裸 JSON/列表文本中解析出选项 ID / 填空值列表。"""
    data = parse_json_list(text, allow_literal_eval=True)
    if not isinstance(data, list):
        return []
    ids = []
    for item in data:
        if isinstance(item, dict) and item.get("id") is not None:
            ids.append(item["id"])
        elif isinstance(item, (int, str)) and str(item).strip():
            ids.append(item)
    return ids


def _plain_html(html_text) -> str:
    text = extract_plain_text(html_text)
    return text or re.sub(r"<[^>]+>", " ", str(html_text or "")).strip()


def parse_homework_answer(text: str, question: dict) -> tuple[list, list]:
    """解析作业/考试模型回复，返回 (选项 ID 列表, 填空文本列表)。"""
    data = parse_json_list(text, allow_literal_eval=False)
    if data is None:
        return [], []
    options = question.get("options") or []
    by_id = {str(option["id"]): str(option["id"]) for option in options}
    by_text = {_plain_html(option.get("content")): str(option["id"]) for option in options}
    if question.get("type") == QuestionType.FILL:
        values = []
        for item in data:
            if isinstance(item, dict):
                item = item.get("content") or item.get("text") or item.get("answer")
            cleaned = str(item or "").strip()
            if cleaned:
                values.append(cleaned)
        return [], values

    selected: list[str] = []
    for item in data:
        value = item
        if isinstance(item, dict):
            value = item.get("id") or item.get("content") or item.get("answer")
        token = str(value or "").strip()
        if not token:
            continue
        if token in by_id:
            target = by_id[token]
        elif token in by_text:
            target = by_text[token]
        elif re.fullmatch(r"[A-Ha-h]+", token) and len(token) <= len(options):
            for char in token.upper():
                idx = _LETTERS.index(char)
                if idx < len(options):
                    ident = str(options[idx]["id"])
                    if ident not in selected:
                        selected.append(ident)
            continue
        elif token.isdigit() and 0 < int(token) <= len(options):
            target = str(options[int(token) - 1]["id"])
        else:
            target = None
        if target and target not in selected:
            selected.append(target)
    return selected, []


def letters_to_indexes(letters: str, option_count: int = 0) -> list[int]:
    """将选项字母串（如 'ABD'）转换为去重后的 0-based 下标列表。"""
    idxs: list[int] = []
    for ch in (letters or "").upper():
        idx = ord(ch) - ord("A")
        if idx < 0 or idx > 7 or idx in idxs:
            continue
        if option_count and idx >= option_count:
            continue
        idxs.append(idx)
    return idxs


def parse_vision_answer(answer: str, option_count: int = 0) -> tuple[list[int], str]:
    """解析视觉模型回复，返回 (选项下标列表, 填空文本)。"""
    text = strip_think_tags(str(answer or ""), drop_unclosed=False).strip()
    if not text:
        return [], ""

    fill = _FILL_RE.search(text)
    if fill:
        value = fill.group(1).strip().splitlines()[0].strip().strip("`\"'。；; ")
        return [], value

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for cand in [text, *reversed(lines)]:
        compact = re.sub(r"[\s,，、和/|+]+", "", cand).strip("。；;.!！?？:：()（）[]【】")
        if _LETTER_TOKEN_RE.match(compact):
            idxs = letters_to_indexes(compact, option_count)
            if idxs:
                return idxs, ""

    match = _LETTER_ANSWER_RE.search(text)
    if match:
        idxs = letters_to_indexes(match.group(1), option_count)
        if idxs:
            return idxs, ""
    return [], ""


def parse_reference_answer_text(value: str | None) -> dict:
    """把「参考答案：B」/「参考答案：错」/「参考答案：A、B、C」解析成答案字典。"""
    raw = normalize_text(value)
    raw = raw.replace("参考答案", "").replace("：", " ").replace(":", " ").strip()
    cleaned = re.sub(r"[\s、，,;；/]+", "", raw)
    if cleaned and re.fullmatch(r"[A-Za-z]+", cleaned):
        return {"letters": [letter.upper() for letter in cleaned]}
    return {"text": raw} if raw else {}
