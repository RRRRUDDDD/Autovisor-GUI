# -*- coding: utf-8 -*-
"""智慧树作业 / 考试页面自动作答（图片题）。

作业卷的题干与选项常被渲染成图片, DOM 文本为空, 文本大模型无法解析.
这里改为逐题截取题目区域 -> 交给配置的视觉模型 -> 按 DOM 顺序点选 -> 点【下一题】保存.

页面: onlineexamh5new.zhihuishu.com/stuExamWeb.html#/webExamList/dohomework/...
提交策略跟随 data/llm.json 的 auto_submit: 默认只作答并暂存, 不提交.
"""
import asyncio
import hashlib
import re
import time

from modules import llm
from modules.logger import Logger

logger = Logger()

_QUESTION_SELECTOR = ".examPaper_subject"
_OPTION_SELECTOR = ".nodeLab"
_NEXT_TEXTS = ("下一题", "下一页", "下一个", "下一步")
_SAVE_TEXTS = ("暂存作业", "暂存试卷", "保存作业", "保存")
_SUBMIT_TEXTS = ("提交作业", "提交试卷", "交卷", "提交")
_CONFIRM_TEXTS = ("确定", "确认", "确认提交", "确定提交", "继续提交", "确认交卷")

VISION_PROMPT = """你是一名网课答题助手。图片是智慧树作业/考试的一道题，包含题干和选项。
请直接给出正确答案，只输出一个结果:
- 单选题: 输出一个选项字母，如 B
- 多选题: 输出全部正确选项字母，如 ABD
- 判断题: 按从上到下的顺序把两个选项当作 A、B，输出正确的一个
- 填空题: 输出「FILL: 答案原文」
选项按图片中从上到下的顺序依次为 A、B、C、D、E、F...，不要输出解释或多余文字。"""

_THINK_RE = re.compile(r"<think\b[^>]*>.*?</thi" + "nk>", re.IGNORECASE | re.DOTALL)
_THINK_TAG_RE = re.compile(r"</?think[^>]*>", re.IGNORECASE)
_LETTER_TOKEN_RE = re.compile(r"^[A-H](?:[A-H])*$", re.IGNORECASE)
_LETTER_ANSWER_RE = re.compile(
    r"(?:答案|正确选项|选择|应选|选)\s*(?:是|为)?\s*[:：]?\s*([A-Ha-h]{1,8})"
)
_FILL_RE = re.compile(r"FILL\s*[:：]\s*(.+)", re.IGNORECASE | re.DOTALL)

JS_SIGNATURE = r"""
el => {
  const ids = [...el.querySelectorAll("input,textarea")]
    .map(i => (i.name || "") + ":" + (i.value || ""))
    .filter(Boolean)
    .join("|");
  if (ids) return ids;
  const mark = el.getAttribute("data-id") || el.getAttribute("data-questionid")
    || el.getAttribute("data-question-id") || "";
  if (mark) return mark;
  const text = (el.innerText || "").trim();
  if (text) return text.slice(0, 160);
  const imgs = [...el.querySelectorAll("img")].map(i => i.getAttribute("src") || "").join("|");
  return imgs.slice(0, 240);
}
"""

JS_QUESTION_TYPE = r"""
el => {
  const types = [...el.querySelectorAll("input")].map(i => (i.type || "").toLowerCase());
  if (types.includes("checkbox")) return "multiple";
  if (types.includes("radio")) return "single";
  if (el.querySelector("textarea, input[type=text], [contenteditable=true]")) return "fill";
  const text = el.innerText || "";
  if (/多选/.test(text)) return "multiple";
  if (/判断/.test(text)) return "judge";
  if (/填空|简答|问答/.test(text)) return "fill";
  if (/单选/.test(text)) return "single";
  return "choice";
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

JS_HAS_ANSWER = r"""
el => {
  const inputs = [...el.querySelectorAll("input")];
  if (inputs.some(i => i.checked)) return true;
  if (inputs.some(i => (i.value || "").trim() && i.type !== "radio" && i.type !== "checkbox")) return true;
  const box = el.querySelector("textarea, [contenteditable=true]");
  if (box && ((box.value || "").trim() || (box.innerText || "").trim())) return true;
  return [...el.querySelectorAll(".nodeLab")].some(node => {
    const cls = (node.className || "") + " " + ((node.parentElement && node.parentElement.className) || "");
    return /(^|\s)(active|checked|selected|is-active|is-checked|is-selected)(\s|$)/.test(cls);
  });
}
"""

JS_FILL = r"""
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


def is_homework_url(url: str) -> bool:
    """是否为智慧树作业/考试的答题页（列表页、学习报告不算）。"""
    low = (url or "").lower()
    if "zhihuishu.com" not in low or "onlineexamh5" not in low:
        return False
    from modules.url_router import parse_exam_url

    return parse_exam_url(url).get("kind") in ("dohomework", "doexam")


def is_non_quiz_exam_page(url: str) -> bool:
    """在线考试域名下的非答题页（作业列表、考试列表、学习报告），无需文本解析。"""
    low = (url or "").lower()
    if "onlineexamh5" not in low and "stulearnreportnew" not in low:
        return False
    if is_homework_url(low):
        return False
    return "stulearnreportnew" in low or "webexamlist" in low


def _strip_think(text: str) -> str:
    cleaned = _THINK_RE.sub("", text or "")
    return _THINK_TAG_RE.sub("", cleaned)


def _letters_to_indexes(letters: str, option_count: int = 0) -> list[int]:
    idxs = []
    for ch in (letters or "").upper():
        idx = ord(ch) - ord("A")
        if idx < 0 or idx > 7 or idx in idxs:
            continue
        if option_count and idx >= option_count:
            continue
        idxs.append(idx)
    return idxs


def parse_vision_answer(answer: str, option_count: int = 0) -> tuple[list[int], str]:
    """解析视觉模型回复, 返回 (选项下标列表, 填空文本)。

    兼容 "B" / "ABD" / "答案：A、C" / "正确答案是 B" / "FILL: xxx" 等形态。
    """
    text = _strip_think(str(answer or "")).strip()
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
            idxs = _letters_to_indexes(compact, option_count)
            if idxs:
                return idxs, ""

    match = _LETTER_ANSWER_RE.search(text)
    if match:
        idxs = _letters_to_indexes(match.group(1), option_count)
        if idxs:
            return idxs, ""
    return [], ""


async def _visible_question(page):
    handles = await page.query_selector_all(_QUESTION_SELECTOR)
    for handle in handles:
        try:
            box = await handle.bounding_box()
        except Exception:
            box = None
        if box and box.get("width", 0) > 20 and box.get("height", 0) > 20:
            return handle
    return None


async def _wait_question(page, timeout: float = 30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            handle = await _visible_question(page)
        except Exception:
            handle = None
        if handle is not None:
            return handle
        await page.wait_for_timeout(500)
    return None


async def _signature(handle) -> str:
    try:
        return str(await handle.evaluate(JS_SIGNATURE) or "")
    except Exception:
        return ""


async def _question_type(handle) -> str:
    try:
        return str(await handle.evaluate(JS_QUESTION_TYPE) or "choice")
    except Exception:
        return "choice"


async def _has_answer(handle) -> bool:
    try:
        return bool(await handle.evaluate(JS_HAS_ANSWER))
    except Exception:
        return False


async def _visible_options(handle) -> list:
    handles = await handle.query_selector_all(_OPTION_SELECTOR)
    visible = []
    for item in handles:
        try:
            box = await item.bounding_box()
        except Exception:
            box = None
        if box and box.get("width", 0) > 0 and box.get("height", 0) > 0:
            visible.append(item)
    if visible:
        return visible
    for selector in ("label", "input[type=radio]", "input[type=checkbox]"):
        candidates = await handle.query_selector_all(selector)
        visible = []
        for item in candidates:
            try:
                box = await item.bounding_box()
            except Exception:
                box = None
            if box and box.get("width", 0) > 0 and box.get("height", 0) > 0:
                visible.append(item)
        if visible:
            return visible
    return []


async def _click_option(handle) -> bool:
    try:
        if await handle.evaluate(JS_OPTION_CHECKED):
            return True
    except Exception:
        pass
    try:
        await handle.scroll_into_view_if_needed(timeout=2000)
    except Exception:
        pass
    try:
        await handle.click(timeout=4000)
    except Exception:
        try:
            if not await handle.evaluate(JS_CLICK_OPTION):
                return False
        except Exception:
            return False
    await asyncio.sleep(0.2)
    try:
        return bool(await handle.evaluate(JS_OPTION_CHECKED))
    except Exception:
        return True


async def _apply_choice(options: list, idxs: list[int], allow_uncheck: bool = False) -> int:
    """按目标下标点选; 多选时取消多余的旧选项。返回成功选中的目标个数。"""
    matched = 0
    for pos, option in enumerate(options):
        if pos in idxs:
            if await _click_option(option):
                matched += 1
        elif allow_uncheck:
            try:
                if await option.evaluate(JS_OPTION_CHECKED):
                    await option.click(timeout=3000)
                    await asyncio.sleep(0.1)
            except Exception:
                pass
    return matched


async def _fill_question(handle, value: str) -> bool:
    try:
        return bool(await handle.evaluate(JS_FILL, value))
    except Exception:
        return False


async def _click_text(page, texts, max_len: int = 12) -> bool:
    try:
        return bool(await page.evaluate(JS_CLICK_TEXT, [list(texts), max_len]))
    except Exception:
        return False


async def _question_total(page) -> int:
    try:
        return int(
            await page.evaluate(
                """() => {
                  const m = (document.body.innerText || "").match(/总题数[:：]\\s*(\\d+)/);
                  if (m) return parseInt(m[1], 10);
                  return document.querySelectorAll(".answerCard_list > *").length || 0;
                }"""
            )
        )
    except Exception:
        return 0


async def _confirm(page) -> bool:
    clicked = False
    for _ in range(3):
        if not await _click_text(page, _CONFIRM_TEXTS):
            break
        clicked = True
        await page.wait_for_timeout(800)
    return clicked


async def _goto_next(page, current_sig: str | None = None) -> tuple[bool, bool]:
    """点【下一题】并等待题目切换。

    返回 (是否已切换, 是否存在下一题按钮)。按钮不存在表示已到最后一题;
    按钮存在但题目不变通常是有题未作答, 调用方应停止且避免自动提交。
    """
    had_button = False
    for _ in range(3):
        if not await _click_text(page, _NEXT_TEXTS):
            return False, had_button
        had_button = True
        for _ in range(8):
            await page.wait_for_timeout(400)
            question = await _visible_question(page)
            if question is None:
                continue
            if current_sig is None:
                return True, True
            if await _signature(question) != current_sig:
                return True, True
    return False, True


async def _save_or_submit(page, auto_submit: bool) -> None:
    if auto_submit:
        if not await _click_text(page, _SUBMIT_TEXTS):
            logger.warn("作业/考试页未找到提交按钮, 请手动提交.")
            logger.event("作业提交", 结果="未找到按钮")
            return
        await page.wait_for_timeout(1200)
        await _confirm(page)
        logger.info("作业/考试页已点击提交并处理确认弹窗.")
        logger.event("作业提交", 结果="已提交")
        return
    if await _click_text(page, _SAVE_TEXTS):
        await page.wait_for_timeout(800)
        await _confirm(page)
        logger.info("作业/考试答案已暂存, 请核对后自行提交.")
        logger.event("作业提交", 结果="已暂存")
    else:
        logger.info("作业/考试作答完成, 请核对后自行提交.")
        logger.event("作业提交", 结果="未提交")


_API_KIND_FLAGS = {
    "dohomework": "enableHomeworkApi",
    "doexam": "enableFormalExamApi",
}


def _api_enabled(config, kind: str) -> bool:
    attr = _API_KIND_FLAGS.get(kind)
    if not attr:
        return False
    if config is None:
        return True
    return bool(getattr(config, attr, True))


def _captcha_modules():
    try:
        import numpy as np
        import cv2
    except Exception:
        return None
    return [np, cv2]


async def _settle_captcha(page, config) -> bool:
    """作业接口触发人机验证: 显示窗口, 自动/手动处理后再决定是否重试."""
    from modules.slider import captcha_visible, slider_verify
    from modules.window_control import display_window, hide_window, should_hide_window

    hidden = should_hide_window(config)
    if hidden:
        await display_window(page)
    logger.warn("接口作答触发人机验证, 请在浏览器中完成滑块/验证码 ...", shift=True)
    logger.event("作业接口作答", 结果="需要人机验证")
    logger.event("人工介入", 状态="需要", 原因="作业接口触发人机验证")
    solved = False
    modules = _captcha_modules() if getattr(config, "enableAutoCaptcha", True) else None
    try:
        if modules:
            solved = bool(
                await slider_verify(page, modules, appear_timeout_ms=6000, max_attempts=3)
            )
        if not solved:
            deadline = time.monotonic() + 120
            seen = False
            while time.monotonic() < deadline:
                visible = await captcha_visible(page)
                seen = seen or visible
                if seen and not visible:
                    solved = True
                    break
                await page.wait_for_timeout(1000)
    finally:
        if hidden:
            await hide_window(page)
    logger.event("人工介入", 状态="已解除" if solved else "超时", 原因="作业接口人机验证")
    return solved


async def _try_api_with_captcha(page, config, params: dict) -> dict:
    from modules.zhs_api import CaptchaRequiredError

    last_error = None
    for attempt in range(2):
        try:
            return await _try_zhidao_api(page, config, params)
        except CaptchaRequiredError as exc:
            last_error = exc
            logger.warn(f"接口作答触发人机验证: {exc}")
            logger.event(
                "作业接口作答",
                结果="人机验证",
                错误=logger.summarize_exception(exc),
            )
            if attempt == 0 and await _settle_captcha(page, config):
                logger.info("人机验证已处理, 重试接口作答 ...")
                continue
            break
    raise last_error


async def _try_zhidao_api(page, config, params: dict) -> dict:
    """用浏览器 Cookie 构造加密 API 客户端并同步作答, 不修改页面."""
    from modules.ai_analysis_api import AiAnalysisApi
    from modules.answer_store import AiQuestionStore
    from modules.zhidao_exam_api import ZhidaoExamApi
    from modules.zhidao_exam_worker import ZhidaoExamWorker
    from modules.zhidao_homework_api import ZhidaoHomeworkApi
    from modules.zhidao_homework_worker import ZhidaoHomeworkWorker
    from modules.zhs_api import ZhsApiClient

    cookies = await page.context.cookies()
    try:
        user_agent = await page.evaluate("navigator.userAgent")
    except Exception:
        user_agent = ""
    proxy = str(getattr(config, "proxy", "") or "").strip()
    client = ZhsApiClient(cookies, proxy=proxy, user_agent=user_agent)
    auto_submit = bool(llm.conf().get("auto_submit", False))
    store = AiQuestionStore()
    course_id = params.get("courseId", "")

    if params["kind"] == "dohomework":
        threshold = getattr(config, "homeworkThreshold", 100)
        max_redo = getattr(config, "homeworkMaxRedo", 3)
        worker = ZhidaoHomeworkWorker(
            ZhidaoHomeworkApi(client),
            course_id=course_id,
            store=store,
            analysis_api=AiAnalysisApi(session=client.session, proxy=proxy),
            auto_submit=auto_submit,
            target_score=float(threshold or 0),
            max_rounds=int(max_redo or 0),
        )
        return await asyncio.to_thread(
            worker.run,
            recruit_id=params.get("recruitId", ""),
            course_id=course_id,
            homework_id=params.get("examId", ""),
            stu_exam_id=params.get("stuExamId", ""),
            exam_id=params.get("examId", ""),
            school_id=params.get("schoolId", ""),
        )

    worker = ZhidaoExamWorker(
        ZhidaoExamApi(client),
        course_id=course_id,
        store=store,
        auto_submit=auto_submit,
    )
    return await asyncio.to_thread(
        worker.run,
        recruit_id=params.get("recruitId", ""),
        course_id=course_id,
        exam_id=params.get("examId", ""),
        student_exam_id=params.get("stuExamId", ""),
        school_id=params.get("schoolId", ""),
    )


async def auto_answer_homework(page, config=None) -> dict:
    """对当前智慧树作业/考试页作答; 接口明文优先, 视觉模型兜底。"""
    stats = {"ok": 0, "fill": 0, "fail": 0}

    try:
        from modules.zhidao_homework_api import parse_exam_page_url

        params = parse_exam_page_url(getattr(page, "url", ""))
    except Exception:
        params = {}
    if params and _api_enabled(config, params["kind"]):
        logger.info("检测到知到作业/考试页, 优先使用加密 API 明文作答 ...")
        logger.event("作业接口作答", 地址=getattr(page, "url", ""), 类型=params["kind"])
        try:
            api_stats = await _try_api_with_captcha(page, config, params)
        except Exception as exc:
            logger.warn(f"接口作答不可用, 回退视觉模型: {exc}")
            logger.event(
                "作业接口作答",
                结果="回退视觉",
                错误=logger.summarize_exception(exc),
            )
        else:
            if not api_stats.get("needs_vision"):
                logger.info(
                    "接口作答完成: 作答 {answered} 题, 已交卷={submitted}, 得分={score}".format(
                        answered=api_stats.get("answered", 0),
                        submitted=api_stats.get("submitted", False),
                        score=api_stats.get("score"),
                    )
                )
                logger.event("作业接口作答汇总", **api_stats)
                return api_stats
            logger.info("接口检测到图片题, 回退视觉模型逐题作答.")
            answered = int(api_stats.get("answered", 0) or 0)
            if answered:
                try:
                    await page.reload(wait_until="domcontentloaded", timeout=30000)
                except Exception as exc:
                    logger.debug(f"回退视觉前刷新页面失败: {exc}")

    if not llm.vision_enabled():
        logger.warn(
            "检测到作业/考试页, 但未配置视觉模型, 无法识别图片题干. "
            "请在设置的大模型卡片填写「视觉模型」（如 gpt-4o / Qwen2.5-VL / glm-4v）, 已跳过."
        )
        logger.event("作业答题", 结果="未配置视觉模型")
        return stats

    try:
        await page.wait_for_load_state("domcontentloaded", timeout=20000)
    except Exception:
        pass

    question = await _wait_question(page, timeout=30)
    if question is None:
        logger.warn("作业/考试页面未找到题目区域, 已跳过自动作答.")
        logger.event("作业答题", 结果="未找到题目")
        return stats

    auto_submit = bool(llm.conf().get("auto_submit", False))
    proxy = str(getattr(config, "proxy", "") or "").strip() or None
    logger.info(f"检测到作业/考试页面, 使用视觉模型逐题作答 ({llm.vision_label()}) ...")
    logger.event("作业答题", 视觉模型=llm.vision_label(), 自动提交=auto_submit)
    total = await _question_total(page)

    seen = set()
    stuck = False
    for index in range(1, 201):
        question = await _visible_question(page)
        if question is None:
            break
        dom_sig = await _signature(question)
        if dom_sig:
            if dom_sig in seen:
                logger.info("题目未切换（可能已到最后一题）, 停止作答.")
                break
            seen.add(dom_sig)
        if await _has_answer(question):
            if not dom_sig:
                seen.add(f"answered:{index}")
            logger.info(f"第{index}题 已有答案（接口已保存）, 跳过视觉识别.")
            logger.event("作业答题", 题号=index, 结果="已作答跳过")
            advanced, had_next = await _goto_next(page, dom_sig or None)
            if advanced:
                continue
            if had_next and not (total and len(seen) >= total):
                stuck = True
                logger.warn("点击下一题后题目未切换, 停止作答.")
            break
        qtype = await _question_type(question)
        options = await _visible_options(question)
        try:
            shot = await question.screenshot(type="png", timeout=8000)
        except Exception as exc:
            stats["fail"] += 1
            logger.warn(f"第{index}题截图失败: {exc}")
            logger.event("作业答题", 题号=index, 结果="截图失败", 错误=logger.summarize_exception(exc))
            advanced, had_next = await _goto_next(page, dom_sig or None)
            if advanced:
                continue
            if had_next and not (total and len(seen) >= total):
                stuck = True
                logger.warn("点击下一题后题目未切换, 停止作答.")
            break
        if not dom_sig:
            sig = "img:" + hashlib.sha1(shot).hexdigest()[:16]
            if sig in seen:
                logger.info("题目未切换（可能已到最后一题）, 停止作答.")
                break
            seen.add(sig)

        try:
            reply = await asyncio.to_thread(
                llm.chat_vision_sync, shot, VISION_PROMPT, "image/png", None, proxy
            )
        except Exception as exc:
            stats["fail"] += 1
            logger.warn(f"第{index}题视觉模型请求失败: {exc}")
            logger.event("作业答题", 题号=index, 结果="请求失败", 错误=logger.summarize_exception(exc))
            advanced, had_next = await _goto_next(page, dom_sig or None)
            if advanced:
                continue
            if had_next and not (total and len(seen) >= total):
                stuck = True
                logger.warn("点击下一题后题目未切换, 停止作答.")
            break

        idxs, fill_text = parse_vision_answer(reply, len(options))
        if fill_text or (qtype == "fill" and not options):
            if fill_text and await _fill_question(question, fill_text):
                stats["fill"] += 1
                logger.info(f"第{index}题 已填空 -> 「{_brief(fill_text)}」")
                logger.event("作业答题", 题号=index, 题型="fill", 答案=_brief(fill_text), 结果="已填空")
            else:
                stats["fail"] += 1
                logger.warn(f"第{index}题 填空失败（视觉模型回复: {_brief(reply)}）")
                logger.event("作业答题", 题号=index, 题型="fill", 结果="填空失败", 回答=_brief(reply))
        elif idxs:
            matched = await _apply_choice(options, idxs, allow_uncheck=(qtype == "multiple"))
            answer = ",".join(chr(65 + i) for i in idxs)
            if matched == len(idxs):
                stats["ok"] += 1
                logger.info(f"第{index}题 已作答 -> {answer}")
                logger.event("作业答题", 题号=index, 题型=qtype, 答案=answer, 结果="已作答")
            else:
                stats["fail"] += 1
                logger.warn(f"第{index}题 选项点击失败（期望 {answer}, 成功 {matched}/{len(idxs)}）")
                logger.event("作业答题", 题号=index, 题型=qtype, 答案=answer, 结果="点击失败")
        else:
            stats["fail"] += 1
            logger.warn(f"第{index}题 视觉模型未给出可解析答案: {_brief(reply, 60)}")
            logger.event("作业答题", 题号=index, 题型=qtype, 结果="无法解析", 回答=_brief(reply))

        advanced, had_next = await _goto_next(page, dom_sig or None)
        if advanced:
            continue
        if had_next and not (total and len(seen) >= total):
            stuck = True
            logger.warn("点击下一题后题目未切换(可能有题未完成), 停止作答.")
        else:
            logger.info("已到最后一题, 结束逐题作答." if not total else f"已完成全部 {total} 题, 结束作答.")
        break

    logger.info(
        f"作业/考试作答完成: 选择 {stats['ok']} 题, 填空 {stats['fill']} 题, 失败 {stats['fail']} 题"
    )
    if stats["fail"]:
        logger.warn(f"有 {stats['fail']} 题未能作答, 提交前请人工核对.")
    if stuck and auto_submit:
        logger.warn("检测到翻页停滞, 为避免误提交, 本次只暂存答案, 请人工检查后手动提交.")
        await _save_or_submit(page, False)
    else:
        await _save_or_submit(page, auto_submit)
    logger.event("作业答题汇总", **stats, 自动提交=auto_submit, 已提交=auto_submit and not stuck)
    return stats


def _brief(text: str, limit: int = 60) -> str:
    cleaned = " ".join(str(text or "").split())
    return cleaned if len(cleaned) <= limit else f"{cleaned[:limit]}..."