# encoding=utf-8
"""题干/选项 HTML 的纯文本提取与图片检测 (标准库实现)."""
from __future__ import annotations

import re
from html.parser import HTMLParser

_SPACER_IMG_PREFIX = "data:image/gif;base64,r0lgod"
_BLOCK_TAGS = {
    "br", "p", "div", "li", "tr", "ul", "ol", "table", "section", "article",
    "h1", "h2", "h3", "h4", "h5", "h6", "blockquote",
}
_SKIP_TAGS = {"script", "style", "noscript", "template"}
_WS_RE = re.compile(r"[\s\u3000\xa0]+")


class _TextCollector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0
        self.has_img = False

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "img":
            attrs = dict(attrs)
            src = str(attrs.get("src") or "").strip()
            if src and not src.lower().startswith(_SPACER_IMG_PREFIX):
                self.has_img = True
        if tag in _BLOCK_TAGS:
            self._chunks.append(" ")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        if tag in _BLOCK_TAGS:
            self._chunks.append(" ")

    def handle_data(self, data):
        if self._skip_depth:
            return
        self._chunks.append(data)

    def text(self) -> str:
        return "".join(self._chunks)


def _parse(html_str: str) -> _TextCollector:
    parser = _TextCollector()
    try:
        parser.feed(str(html_str or ""))
        parser.close()
    except Exception:
        # 破损 HTML 也要尽力返回已解析的部分
        pass
    return parser


def extract_plain_text(html_str) -> str:
    """剥离标签、解码实体、折叠空白, 返回可直接交给文本模型的题干."""
    if not html_str:
        return ""
    raw = html_str if isinstance(html_str, str) else str(html_str)
    text = _parse(raw).text()
    return _WS_RE.sub(" ", text).strip()


def has_meaningful_image(html_str) -> bool:
    """题干或选项 HTML 中是否含有效图片, 用于决定是否回退视觉模型."""
    if not html_str:
        return False
    return _parse(html_str).has_img