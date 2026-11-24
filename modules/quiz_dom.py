# encoding=utf-8
"""跨测验/作业模块共享的 Playwright 公共错误判定与 DOM 交互辅助。"""
from __future__ import annotations

import asyncio

import playwright.async_api as _playwright_async_api
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError


def _resolve_target_closed_error():
    """从公开 playwright.async_api 解析 TargetClosedError，避免依赖私有 _impl 导入。"""
    exported = getattr(_playwright_async_api, "TargetClosedError", None)
    if exported is not None:
        return exported
    for sub in PlaywrightError.__subclasses__():
        if sub.__name__ == "TargetClosedError":
            return sub
    return PlaywrightError


TargetClosedError = _resolve_target_closed_error()

__all__ = [
    "PlaywrightError",
    "PlaywrightTimeoutError",
    "TargetClosedError",
    "is_target_closed_error",
    "JS_CLICK_TEXT",
    "JS_FIND_BUTTON",
    "JS_OPTION_CHECKED",
    "JS_CLICK_OPTION",
    "JS_FILL_CONTAINER",
    "click_button_by_text",
    "confirm_dialog",
]


def is_target_closed_error(exc: BaseException) -> bool:
    """判断异常是否为 Playwright 页面/上下文/浏览器已关闭错误（不依赖私有 _impl 导入）。"""
    if not isinstance(exc, PlaywrightError):
        return False
    if type(exc).__name__ == "TargetClosedError":
        return True
    msg = str(exc).lower()
    return (
        "target page, context or browser has been closed" in msg
        or "target closed" in msg
        or "browser has been closed" in msg
    )


JS_CLICK_TEXT = r"""
([texts, maxLen]) => {
  const norm = (s) => (s || "").replace(/[\s\u3000]+/g, "");
  const wanted = texts.map(norm).filter(Boolean);
  if (!wanted.length) return false;
  const els = [...document.querySelectorAll(
    "button, a, [role=button], .el-button, .btn, div, span, li"
  )];
  for (const el of els) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    if (el.disabled || el.getAttribute("aria-disabled") === "true") continue;
    if (/disabled/i.test(el.className || "")) continue;
    const t = norm(el.innerText || el.value || el.getAttribute("title") || "");
    if (!t || t.length > (maxLen || 12)) continue;
    if (!wanted.includes(t)) continue;
    el.click();
    return true;
  }
  return false;
}
"""

JS_FIND_BUTTON = r"""
(texts) => {
  const norm = (s) => (s || "").replace(/[\s\u3000]+/g, "");
  const wanted = texts.map(t => norm(t)).filter(Boolean);
  if (!wanted.length) return false;
  const els = [...document.querySelectorAll(
    "button, a, input, div, span, li, p, img, [role=button], .el-button, .btn"
  )];
  let best = null, bestScore = Infinity;
  for (const el of els) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0 || r.height > 120) continue;
    let t = "";
    if (el.tagName === "INPUT") t = el.value || "";
    if (!t) t = el.innerText || "";
    if (!t) t = el.getAttribute("title") || "";
    if (!t) t = el.getAttribute("aria-label") || "";
    if (!t) t = el.getAttribute("alt") || "";
    t = String(t).trim();
    if (!t || t.length > 12) continue;
    const nt = norm(t);
    if (!wanted.some(w => nt === w || nt.includes(w))) continue;
    const style = getComputedStyle(el);
    const isBtn = ["BUTTON", "A", "INPUT"].includes(el.tagName) || style.cursor === "pointer";
    const score = (isBtn ? 0 : 5) + nt.length;
    if (score < bestScore) { best = el; bestScore = score; }
  }
  if (!best) return false;
  best.click();
  return true;
}
"""

JS_CLICK_OPTION = r"""
el => {
  const input = el.querySelector("input");
  if (input && input.disabled) return false;
  let target = el;
  const label = el.querySelector("label, .label");
  if (label) target = label;
  target.click();
  if (input) {
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }
  return true;
}
"""

JS_OPTION_CHECKED = r"""
el => {
  const cls = (el.className || "") + " " + ((el.parentElement && el.parentElement.className) || "");
  const active = /(^|\s)(active|checked|selected|is-active|is-checked|is-selected)(\s|$)/.test(cls);
  const input = el.querySelector("input");
  if (input) return !!input.checked || active;
  return active;
}
"""

JS_FILL_CONTAINER = r"""
(el, value) => {
  const box = el.querySelector("textarea, input[type=text], [contenteditable=true]");
  if (!box) return false;
  box.focus();
  if (box.isContentEditable) box.innerText = value;
  else box.value = value;
  box.dispatchEvent(new Event("input", { bubbles: true }));
  box.dispatchEvent(new Event("change", { bubbles: true }));
  return true;
}
"""


async def click_button_by_text(
    page,
    texts,
    *,
    max_len: int = 12,
    allow_substring: bool = False,
) -> bool:
    """按可见按钮文本点击匹配控件；allow_substring=False 走精确匹配，True 走最优子串匹配。"""
    try:
        if allow_substring:
            return bool(await page.evaluate(JS_FIND_BUTTON, list(texts)))
        return bool(await page.evaluate(JS_CLICK_TEXT, [list(texts), max_len]))
    except Exception:
        return False


async def confirm_dialog(
    page,
    confirm_texts=("确定", "确认", "确认提交", "确定提交", "继续提交", "确认交卷"),
    *,
    delay_s: float = 0.4,
    allow_substring: bool = False,
) -> bool:
    """短暂等待后点击确认弹窗按钮。"""
    if delay_s > 0:
        await asyncio.sleep(delay_s)
    return await click_button_by_text(
        page, confirm_texts, allow_substring=allow_substring
    )
