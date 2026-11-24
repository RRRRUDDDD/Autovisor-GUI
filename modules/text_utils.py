# encoding=utf-8
"""跨答题引擎共享的文本归一化、摘要、思考标签清理、缓存键与相似度工具。"""
from __future__ import annotations

import difflib
import re

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_THINK_BLOCK_RE = re.compile(
    r"<think\b[^>]*>.*?</thi" + "nk>",
    re.DOTALL | re.IGNORECASE,
)
_THINK_UNCLOSED_RE = re.compile(r"<think\b[^>]*>.*$", re.DOTALL | re.IGNORECASE)
_THINK_TAG_RE = re.compile(r"</?think[^>]*>", re.IGNORECASE)
_PERCENT_LABELED_RE = re.compile(r"学习进度\s*(\d+(?:\.\d+)?)\s*%")
_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")


def normalize_text(value: str | None) -> str:
    """折叠连续空白并去掉首尾空格。"""
    return " ".join(str(value or "").split())


def compact_text(value: str | None) -> str:
    """移除全部普通空白与全角空格。"""
    return re.sub(r"[\s\u3000]+", "", str(value or ""))


def brief(value: str | None, limit: int = 60, *, strip_html: bool = False) -> str:
    """将题干/选项/回答折叠成单行摘要，可选移除 HTML 标签。"""
    raw = str(value or "")
    if strip_html:
        raw = _HTML_TAG_RE.sub(" ", raw)
    cleaned = " ".join(raw.split())
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[:limit]}..."


def strip_think_tags(text: str | None, *, drop_unclosed: bool = True) -> str:
    """移除推理模型输出的 <think>...</think> 及残留标签；drop_unclosed=True 时同时截断未闭合的 <think>...。"""
    cleaned = _THINK_BLOCK_RE.sub("", str(text or ""))
    if drop_unclosed:
        cleaned = _THINK_UNCLOSED_RE.sub("", cleaned)
    cleaned = _THINK_TAG_RE.sub("", cleaned)
    return cleaned.strip()


def cache_key(value: str | None) -> str:
    """题干精确归一化键：去空白，统一全角括号，去掉尾部空括号。"""
    text = compact_text(value)
    text = text.replace("（", "(").replace("）", ")")
    while text.endswith("()"):
        text = text[:-2]
    return text


def loose_key(value: str | None) -> str:
    """宽松键：只保留中英文与数字并转小写，供模糊匹配（去除下划线填空符与标点差异）。"""
    text = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", str(value or ""))
    return text.lower()


def similarity(a: str | None, b: str | None, *, compact: bool = False) -> float:
    """计算两段文本的 SequenceMatcher 相似度（可选先去空白）。"""
    left = compact_text(a) if compact else str(a or "")
    right = compact_text(b) if compact else str(b or "")
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    return difflib.SequenceMatcher(None, left, right).ratio()


def parse_percent(
    value: str | None,
    *,
    default: int | None = None,
    require_symbol: bool = False,
) -> int | None:
    """解析百分比文本并钳位至 [0, 100]。

    - require_symbol=True: 仅识别带 % 的数值（如 '学习进度 85.5%' / '85%'）。
    - require_symbol=False: 先尝试带 % 的数值，再兜底提取纯数字（如掌握度卡片）。
    """
    text = str(value or "")
    matched = _PERCENT_LABELED_RE.search(text) or _PERCENT_RE.search(text)
    if matched:
        try:
            num = float(matched.group(1))
        except (TypeError, ValueError, OverflowError):
            return default
        return max(0, min(int(num), 100))
    if require_symbol:
        return default
    digits = "".join(ch for ch in text if ch.isdigit())
    if not digits:
        return default
    return max(0, min(int(digits), 100))
