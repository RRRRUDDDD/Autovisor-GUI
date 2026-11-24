# -*- coding: utf-8 -*-
"""测验/弹题专用大模型作答与选项匹配辅助（从 modules/llm.py 拆离至测验领域）。"""
from __future__ import annotations

import asyncio
import json
import re

from modules.llm_client import chat_sync
from modules.llm_config import _resolve_facade, enabled, provider_label
from modules.logger import Logger
from modules.text_utils import (
    brief as _text_brief,
    compact_text as _norm,
    similarity as _text_similarity,
    strip_think_tags as _strip_think_tags,
)

logger = Logger()


def _log():
    return _resolve_facade("logger", logger)


def _brief(text: str, limit: int = 80) -> str:
    """将题干/选项/回答折叠成单行摘要，避免多行文本破坏日志格式。"""
    return _text_brief(text, limit=limit)


def _sim(a: str, b: str) -> float:
    return _text_similarity(a, b, compact=True)


def _build_option_prompt(question_text: str, options: list) -> str:
    opt_lines = "\n".join(f"{i}. {o}" for i, o in enumerate(options))
    return (
        "你是网课弹题助手。下面是一道弹题的题目和选项，直接给出正确答案。\n"
        "只输出正确选项的完整原文，不要解释，不要加标点以外的任何内容。\n\n"
        f"题目：{question_text}\n\n选项：\n{opt_lines}"
    )


def _ask_llmSync(question_text: str, options: list) -> str:
    fn = _resolve_facade("chat_sync", chat_sync)
    return fn(_build_option_prompt(question_text, options), json_mode=False)


async def _ask_llm(question_text: str, options: list) -> str:
    return await asyncio.to_thread(_ask_llmSync, question_text, options)


def to_indexes(answer, options: list) -> list:
    """把各种形态的答案统一成选项下标数组; 填空/无法解析返回空表以外的处理见调用方。"""
    if answer is None:
        return []
    if (
        isinstance(answer, list)
        and answer
        and all(isinstance(x, int) and not isinstance(x, bool) for x in answer)
    ):
        seen: list[int] = []
        for x in answer:
            if 0 <= x < len(options) and x not in seen:
                seen.append(x)
        return seen
    items = answer if isinstance(answer, list) else [answer]
    items = [str(x).strip() for x in items if str(x).strip()]
    idxs = []
    letters = "ABCDEFGH"
    norm_options = [_norm(o) for o in options]
    for item in items:
        if re.fullmatch(r"[A-Ha-h]+", item) and len(item) <= max(len(options), 1):
            for c in item.upper():
                idx = letters.index(c)
                if 0 <= idx < len(options) and idx not in idxs:
                    idxs.append(idx)
            continue
        n_item = _norm(item)
        hit = -1
        # 1. 优先全等匹配，避免短选项子串误命中
        for i, n_o in enumerate(norm_options):
            if n_o and n_o == n_item:
                hit = i
                break
        # 2. 其次唯一子串匹配（限制最短公共长度 >= 2，避免单字选项误匹配）
        if hit < 0 and len(n_item) >= 2:
            sub_hits = [
                i
                for i, n_o in enumerate(norm_options)
                if len(n_o) >= 2 and (n_o in n_item or n_item in n_o)
            ]
            if len(sub_hits) == 1:
                hit = sub_hits[0]
        if hit < 0:
            m = re.match(r"^([A-Ha-h])[\.、．:：]", item)
            if m:
                hit = letters.index(m.group(1).upper())
        if hit < 0 and item.isdigit():
            n = int(item)
            hit = n - 1 if 1 <= n <= len(options) else n
        if 0 <= hit < len(options) and hit not in idxs:
            idxs.append(hit)
    return idxs


def _match_option(answer: str, option_texts: list) -> tuple[int, float]:
    """将模型回复匹配到最佳选项下标, 匹配失败返回 (-1, best_score)."""
    cleaned = _strip_think_tags(answer or "")
    if not cleaned:
        return -1, 0.0
    best, best_score = -1, 0.0
    for idx, t in enumerate(option_texts):
        if not t:
            continue
        score = _sim(cleaned, t)
        if score > best_score:
            best, best_score = idx, score
    if best >= 0 and best_score >= 0.5:
        return best, best_score
    # 兼容回复为选项字母/序号(如 "B" / "B. xxx" / "正确答案是 B")或包含唯一选项全文的情况
    idxs = to_indexes(cleaned, option_texts)
    if len(idxs) == 1 and 0 <= idxs[0] < len(option_texts):
        return idxs[0], max(best_score, 0.8)
    letter_match = re.search(
        r"(?:正确答案|答案|选项|选择|选)\s*(?:是|为|:|：)?\s*([A-Ha-h])(?![A-Za-z])",
        cleaned,
    )
    if letter_match:
        letter_idx = "ABCDEFGH".index(letter_match.group(1).upper())
        if 0 <= letter_idx < len(option_texts):
            return letter_idx, max(best_score, 0.8)
    n_ans = _norm(cleaned)
    contained = [
        idx
        for idx, t in enumerate(option_texts)
        if len(_norm(t)) >= 2 and _norm(t) in n_ans
    ]
    if len(contained) == 1:
        return contained[0], max(best_score, 0.75)
    return -1, best_score


async def _ask_option_with_fallback(
    question_text: str,
    option_texts: list,
    answerer=None,
    *,
    event_name: str,
    q_brief: str,
) -> tuple[int, float, str, str]:
    """优先调用智慧树 AI (answerer), 失败或未匹配时回退自定义大模型."""
    is_enabled = _resolve_facade("enabled", enabled)
    get_label = _resolve_facade("provider_label", provider_label)
    ask_fn = _resolve_facade("_ask_llm", _ask_llm)
    log = _log()

    prompt = _build_option_prompt(question_text, option_texts)

    if answerer is not None:
        try:
            answer = await asyncio.to_thread(answerer, prompt)
            best, best_score = _match_option(answer, option_texts)
            if best >= 0:
                return best, best_score, "智慧树AI", answer
            log.debug(f"智慧树 AI 答案未能匹配选项(回答: {_brief(answer, 50)}), 准备回退自定义大模型.")
            if not is_enabled():
                log.warn(f"智慧树 AI 答案未能匹配选项(回答: {answer[:50]}), 使用默认选择.", shift=True)
                log.event(
                    event_name,
                    方式="智慧树AI",
                    题目=q_brief,
                    回答=_brief(answer, 80),
                    结果="未匹配",
                )
                return -1, best_score, "智慧树AI", answer
        except Exception as exc:
            log.debug(f"智慧树 AI 答题请求失败, 准备回退自定义大模型: {exc}")
            if not is_enabled():
                log.warn(f"智慧树 AI 答题请求失败: {exc}, 使用默认选择.", shift=True)
                log.event(
                    event_name,
                    方式="智慧树AI",
                    题目=q_brief,
                    结果="请求失败",
                    错误=log.summarize_exception(exc),
                )
                return -1, 0.0, "智慧树AI", ""

    if not is_enabled():
        return -1, 0.0, "", ""

    label = get_label()
    try:
        answer = await ask_fn(question_text, option_texts)
    except Exception as exc:
        log.warn(f"大模型答题请求失败: {exc}, 回退默认行为.", shift=True)
        log.event(
            event_name,
            方式=label,
            题目=q_brief,
            结果="请求失败",
            错误=log.summarize_exception(exc),
        )
        return -1, 0.0, label, ""

    best, best_score = _match_option(answer, option_texts)
    if best < 0:
        log.warn(f"大模型答案未能匹配选项(回答: {answer[:50]}), 回退默认行为.", shift=True)
        log.event(
            event_name,
            方式=label,
            题目=q_brief,
            回答=_brief(answer, 80),
            结果="未匹配",
        )
        return -1, best_score, label, answer
    return best, best_score, label, answer


def _parse_quiz_questions(answer_text: str) -> list:
    """从模型回复里取出 questions 数组, 容忍 ```json 围栏和前后含大括号的说明."""
    raw = _strip_think_tags(answer_text or "")
    if not raw:
        return []
    candidates: list[str] = []
    block = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL | re.IGNORECASE)
    if block:
        candidates.append(block.group(1).strip())
    candidates.append(raw)

    decoder = json.JSONDecoder()
    for text in candidates:
        pos = 0
        while True:
            start = text.find("{", pos)
            if start < 0:
                break
            try:
                data, end = decoder.raw_decode(text, start)
            except (ValueError, TypeError):
                pos = start + 1
                continue
            if isinstance(data, dict) and isinstance(data.get("questions"), list):
                return data["questions"]
            pos = max(end, start + 1)
    return []


def _quiz_prompt(text: str) -> str:
    return f"""下面是一个在线测验页面的文本。提取其中所有测验题目（忽略导航栏、菜单、按钮、页脚等无关文本）并作答。

只输出一个 JSON 对象，结构严格如下：
{{"questions": [
  {{"index": 1,
    "question": "题干原文",
    "options": ["选项原文1", "选项原文2"],
    "type": "single|multiple|judge|fill",
    "answer": [0],
    "confidence": "high|medium|low",
    "reason": "一句话理由"}}]}}

规则:
1. answer 对选择/判断题是选项下标数组（从 0 开始），单选长度 1、多选长度 >=1；填空题 options 留空数组、answer 是 ["答案文本"]。
2. option 必须抄页面上的选项原文，不要带 "A."、"B." 之类字母前缀，不要改写。
3. 判断题（对/错、正确/错误）按两个选项处理。
4. 没把握也要给最可能的答案，confidence 填 low。
5. 页面文本可能乱序，靠题号与选项结构还原顺序。

页面文本:
<<<
{text}
>>>"""


async def solve_quiz_page(text: str, answerer=None) -> list:
    """整页抽取测验题目并作答, 返回归一化后的题目列表.

    answerer: 可选同步函数 prompt -> 原始回复文本, 会优先尝试(智慧树 AI);
    失败或未给出有效题目时回退到 data/llm.json 配置的大模型.
    """
    is_enabled = _resolve_facade("enabled", enabled)
    get_label = _resolve_facade("provider_label", provider_label)
    chat_fn = _resolve_facade("chat_sync", chat_sync)
    log = _log()

    if not text or len(text.strip()) < 30:
        return []
    prompt = _quiz_prompt(text)
    answer_text = None
    source = ""
    if answerer is not None:
        try:
            answer_text = await asyncio.to_thread(answerer, prompt)
        except Exception as exc:
            log.debug(f"智慧树 AI 整页作答失败, 回退自定义大模型: {exc}")
    raw = _parse_quiz_questions(answer_text)
    if raw:
        source = "智慧树AI"
    else:
        if answerer is not None and answer_text:
            log.debug("智慧树 AI 返回内容未解析出有效题目, 准备回退自定义大模型.")
        if not is_enabled():
            log.warn("未配置可用的自定义大模型, 无法解析当前测验页题目.", shift=True)
            return []
        source = get_label()
        log.debug(f"使用自定义大模型整页作答: {source}")
        try:
            answer_text = await asyncio.to_thread(chat_fn, prompt, True)
        except Exception as exc:
            log.warn(f"大模型整页作答请求失败: {exc}", shift=True)
            log.event(
                "测验题解",
                来源=source,
                结果="请求失败",
                错误=log.summarize_exception(exc),
            )
            return []
        raw = _parse_quiz_questions(answer_text)
        if not raw:
            log.warn("大模型返回的 JSON 解析失败, 本轮跳过.", shift=True)
            log.event(
                "测验题解",
                来源=source,
                结果="解析失败",
                原始回复=_brief(answer_text, 120),
            )
            return []

    questions = []
    for i, q in enumerate(raw):
        if not isinstance(q, dict):
            continue
        question = str(q.get("question") or q.get("题干") or q.get("题目") or "").strip()
        options = [str(o).strip() for o in (q.get("options") or q.get("选项") or []) if str(o).strip()]
        qtype = str(q.get("type") or q.get("题型") or "").lower()
        raw_answer = q.get("answer", q.get("答案"))
        if qtype not in ("single", "multiple", "judge", "fill"):
            qtype = "fill" if not options else ("multiple" if isinstance(raw_answer, list) and len(raw_answer) > 1 else "single")
        if qtype == "fill":
            ans_text = raw_answer[0] if isinstance(raw_answer, list) and raw_answer else raw_answer
            questions.append({
                "index": q.get("index") or i + 1,
                "question": question, "options": [], "type": "fill",
                "answer": [str(ans_text or "").strip()],
                "confidence": str(q.get("confidence") or q.get("置信度") or "medium").lower(),
                "reason": str(q.get("reason") or q.get("理由") or ""),
            })
        else:
            questions.append({
                "index": q.get("index") or i + 1,
                "question": question, "options": options, "type": qtype,
                "answer": to_indexes(raw_answer, options),
                "confidence": str(q.get("confidence") or q.get("置信度") or "medium").lower(),
                "reason": str(q.get("reason") or q.get("理由") or ""),
            })
    if questions:
        log.info(f"题目解析完成({source}): 共识别 {len(questions)} 题.")
        log.event("测验题解", 来源=source, 题数=len(questions))
        for q in questions:
            if q["type"] == "fill":
                ans_summary = _brief((q.get("answer") or [""])[0], 80)
            else:
                opts = q.get("options") or []
                ans_summary = "; ".join(
                    _brief(opts[idx], 40) if isinstance(idx, int) and 0 <= idx < len(opts) else str(idx)
                    for idx in (q.get("answer") or [])
                )
            log.debug(
                f"[题解明细] 题{q.get('index')}({q.get('type')}) "
                f"题目={_brief(q.get('question', ''), 80)} | 答案={ans_summary} | "
                f"置信度={q.get('confidence', '?')} | 理由={_brief(q.get('reason', ''), 80)}"
            )
    return questions


async def pick_option(dialog_text: str, option_texts: list, answerer=None) -> int:
    """从弹窗文本+选项里选出正确选项下标（优先智慧树 AI，失败回退自定义大模型）。失败/没把握返回 -1。"""
    is_enabled = _resolve_facade("enabled", enabled)
    log = _log()
    if (answerer is None and not is_enabled()) or not option_texts:
        return -1
    if not any(option_texts):
        return -1
    question_text = dialog_text or "请从选项中选出正确答案。"
    q_brief = _brief(question_text, 80)
    best, best_score, label, answer = await _ask_option_with_fallback(
        question_text,
        option_texts,
        answerer=answerer,
        event_name="大模型选题",
        q_brief=q_brief,
    )
    if best >= 0:
        log.info(
            f"大模型选题({label}): 题目「{_brief(dialog_text, 48)}」 -> "
            f"选择「{_brief(option_texts[best], 60)}」(相似度:{round(best_score, 2)})"
        )
        log.event(
            "大模型选题",
            方式=label,
            题目=q_brief,
            回答=_brief(answer, 80),
            选项=_brief(option_texts[best], 80),
            相似度=round(best_score, 2),
        )
        log.debug(f"大模型选题候选选项: {option_texts}")
    return best


async def answer_with_llm(page, ques, choices, answerer=None) -> bool:
    """对一道弹题用 AI 作答（优先智慧树 AI，失败回退自定义大模型）。成功点击返回 True；未启用/失败返回 False。"""
    is_enabled = _resolve_facade("enabled", enabled)
    log = _log()
    if (answerer is None and not is_enabled()) or not choices:
        return False

    try:
        option_texts = [" ".join((await c.inner_text()).split()) for c in choices]
    except Exception:
        return False
    if not option_texts or all(not t for t in option_texts):
        return False

    # 题干：优先 .topic-title，否则取弹窗文本去掉选项行
    question_text = ""
    try:
        title = await page.query_selector(".topic-title")
        if title:
            question_text = await title.inner_text()
    except Exception:
        pass
    if not question_text:
        try:
            dialog = await page.query_selector(".el-dialog")
            if dialog:
                question_text = await dialog.inner_text()
        except Exception:
            pass
    question_text = " ".join((question_text or "").split())
    q_brief = _brief(question_text, 80)

    best, best_score, label, answer = await _ask_option_with_fallback(
        question_text,
        option_texts,
        answerer=answerer,
        event_name="课中答题",
        q_brief=q_brief,
    )
    if best < 0:
        return False

    chosen_text = _brief(option_texts[best], 80)
    log.debug(f"课中弹题选项列表: {option_texts}")
    try:
        await choices[best].click(timeout=2000)
        await page.wait_for_timeout(200)
        log.info(
            f"课中弹题({label}): 题目「{_brief(question_text, 48)}」 -> "
            f"选择「{_brief(option_texts[best], 60)}」(相似度:{round(best_score, 2)})"
        )
        log.event(
            "课中答题",
            方式=label,
            题目=q_brief,
            回答=_brief(answer, 80),
            选项=chosen_text,
            相似度=round(best_score, 2),
            结果="已作答",
        )
        return True
    except Exception as exc:
        log.warn(f"点击大模型选项失败: {exc}, 回退默认行为.", shift=True)
        log.event(
            "课中答题",
            方式=label,
            题目=q_brief,
            选项=chosen_text,
            结果="点击失败",
            错误=log.summarize_exception(exc),
        )
        return False
