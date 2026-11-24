# encoding=utf-8
"""知到作业 API 答题 worker: 答题 -> 提交 -> 查错 -> 官方解析 -> 重做达标."""
from __future__ import annotations

import json
import random
import re
import time

from modules import llm
from modules.answer_store import AiQuestionStore, option_hash
from modules.html_utils import extract_plain_text, has_meaningful_image
from modules.logger import Logger
from modules.question_bank import QuestionBankClient
from modules.zhidao_homework_api import (
    QUESTION_TYPE_NAMES,
    check_time_window,
    correct_option_ids,
    extract_exam_questions,
    extract_homework_list,
    extract_score,
    format_student_answer,
)
from modules.zhs_api import ZhsApiError

logger = Logger()

_ANSWER_BLOCK_RE = re.compile(r"```answer\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_THINK_RE = re.compile(
    r"<think\b[^>]*>.*?</thi" + "nk>",
    re.DOTALL | re.IGNORECASE,
)
_THINK_TAG_RE = re.compile(r"</?think[^>]*>", re.IGNORECASE)
_LIST_RE = re.compile(r"\[.*\]", re.DOTALL)
_LETTERS = "ABCDEFGH"

ROLLBACK_RETRIES = 3
AUTO_MAX_ROUNDS = 10
_CAPTCHA_HINTS = ("滑块", "验证码", "captcha", "slider")
_RESET_HINTS = ("已提交", "不能继续答题", "已交卷", "试卷已提交")


def _plain(html_text) -> str:
    text = extract_plain_text(html_text)
    return text or re.sub(r"<[^>]+>", " ", str(html_text or "")).strip()


def question_has_image(question: dict) -> bool:
    if has_meaningful_image(question.get("content")):
        return True
    return any(
        has_meaningful_image(option.get("content"))
        for option in question.get("options") or []
    )


def _load_json_list(text: str):
    raw = _THINK_RE.sub("", text or "")
    raw = _THINK_TAG_RE.sub("", raw)
    block = _ANSWER_BLOCK_RE.search(raw)
    candidates = [block.group(1).strip()] if block else []
    candidates.append(raw)
    for candidate in candidates:
        try:
            data = json.loads(candidate)
            if isinstance(data, list):
                return data
        except (ValueError, TypeError):
            pass
        match = _LIST_RE.search(candidate)
        if match:
            try:
                data = json.loads(match.group(0))
                if isinstance(data, list):
                    return data
            except (ValueError, TypeError):
                continue
    return None


def parse_homework_answer(text: str, question: dict) -> tuple[list, list]:
    """解析模型回复, 返回 (选项 ID 列表, 填空文本列表)."""
    data = _load_json_list(text)
    if data is None:
        return [], []
    options = question.get("options") or []
    by_id = {str(option["id"]): str(option["id"]) for option in options}
    by_text = {_plain(option.get("content")): str(option["id"]) for option in options}
    if question.get("type") == 3:
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
            target = None
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


def _wrong_combos(row: dict | None) -> list[list[str]]:
    if not row:
        return []
    history = row.get("wrong_options") or []
    combos = []
    for combo in history:
        if isinstance(combo, list):
            cleaned = [str(item) for item in combo if str(item).strip()]
            if cleaned:
                combos.append(cleaned)
    return combos


def _split_user_answer(text, question_type: int) -> list[str]:
    raw = str(text or "")
    separator = "/" if question_type == 3 else ","
    parts = [item.strip() for item in raw.split(separator)]
    if question_type == 3 and len(parts) <= 1 and "," in raw:
        parts = [item.strip() for item in raw.split(",")]
    return [item for item in parts if item]


def _source_dict(payload) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    rt = payload.get("rt")
    sources = []
    if isinstance(rt, dict):
        if isinstance(rt.get("examBase"), dict):
            sources.append(rt["examBase"])
        sources.append(rt)
    sources.append(payload)
    return sources


def payload_needs_reset(payload) -> bool:
    """doHomework 返回已提交/不可继续作答时, 需要先 saveCourseTran 重置."""
    message = " ".join(
        str(source.get(key) or "")
        for source in _source_dict(payload)
        for key in ("message", "msg")
    )
    if any(hint in message for hint in _RESET_HINTS):
        return True
    for source in _source_dict(payload):
        try:
            if int(source.get("state")) == 4:
                return True
        except (TypeError, ValueError):
            continue
    return False


def payload_total_score(payload, default: float = 100.0) -> float:
    for source in _source_dict(payload):
        raw = source.get("totalScore")
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return default


def payload_is_marking(payload) -> int | None:
    for source in _source_dict(payload):
        for key in ("isMarking", "markingCount"):
            raw = source.get(key)
            if raw is None:
                continue
            try:
                return int(raw)
            except (TypeError, ValueError):
                continue
    return None


def payload_max_redo(payload) -> int | None:
    for source in _source_dict(payload):
        for key in ("maxRedo", "maxRedoNum", "redoLimit", "backNum"):
            raw = source.get(key)
            if raw is None:
                continue
            try:
                value = int(raw)
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
    return None


def should_redo_submitted(
    item: dict, *, target_score: float, now=None
) -> bool:
    """判断一份已提交作业 (flag=2) 是否未达标且仍允许重做."""
    if not isinstance(item, dict) or target_score <= 0:
        return False
    raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
    start_time = item.get("startTime") or raw.get("startTime")
    end_date = item.get("endDate") or raw.get("endDate")
    in_window, _ = check_time_window(start_time, end_date, now=now)
    if not in_window:
        return False

    score_raw = item.get("score") if item.get("score") is not None else raw.get("score")
    if score_raw is None:
        return False
    try:
        score = float(score_raw)
    except (TypeError, ValueError):
        return False

    total_raw = (
        item.get("totalScore")
        if item.get("totalScore") is not None
        else raw.get("totalScore")
    )
    total = 0.0
    if total_raw is not None:
        try:
            total = float(total_raw)
        except (TypeError, ValueError):
            total = 0.0
    score_percent = (score / total * 100.0) if total > 0 else score
    if score_percent >= target_score:
        return False

    limit_raw = item.get("backNum")
    if limit_raw is None:
        for key in ("backNum", "maxRedo", "maxRedoNum", "redoLimit"):
            if raw.get(key) is not None:
                limit_raw = raw.get(key)
                break
    marking_raw = item.get("isMarking")
    if marking_raw is None:
        marking_raw = raw.get("isMarking")
    if limit_raw is not None:
        try:
            limit = int(limit_raw)
        except (TypeError, ValueError):
            limit = None
        if limit is not None:
            if limit <= 0:
                return False
            try:
                marking = int(marking_raw or 0)
            except (TypeError, ValueError):
                marking = 0
            if marking >= limit:
                return False
    return True


def answer_info_by_question_id(payload) -> dict[str, dict]:
    """把 getStuAnswerInfo 的条目按数字 questionId 建索引."""
    if not isinstance(payload, dict):
        return {}
    entries = payload.get("rt")
    if isinstance(entries, dict):
        entries = (
            entries.get("questionDtos")
            or entries.get("stuAnswerInfos")
            or entries.get("questions")
            or entries.get("answerInfos")
        )
    result: dict[str, dict] = {}
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("questionId") or entry.get("id") or entry.get("eid") or "")
        if key:
            result[key] = entry
    return result


def _entry_flag(entry: dict | None, *keys) -> bool:
    if not isinstance(entry, dict):
        return False
    for key in keys:
        value = entry.get(key)
        if value is not None:
            return str(value).strip().lower() in {"1", "true", "yes", "y"}
    return False


def _entry_answer(entry: dict | None) -> str:
    if not isinstance(entry, dict):
        return ""
    user = entry.get("userAnswerVo")
    if isinstance(user, dict):
        value = user.get("answer")
    else:
        value = entry.get("userAnswer") or entry.get("stuAnswer") or entry.get("answer")
    if isinstance(value, list):
        return ",".join(str(item) for item in value)
    return str(value or "")


def build_homework_prompt(
    question: dict,
    *,
    wrong_combos: list[list[str]] | None = None,
    analysis: str = "",
    excluded: list[str] | None = None,
    bank_reference: str = "",
) -> str:
    qtype = question.get("type", 1)
    type_name = QUESTION_TYPE_NAMES.get(qtype, "single")
    option_map = {str(option["id"]): option for option in question.get("options") or []}
    lines = [
        "你是网课作业答题助手，请给出正确答案。",
        f"题型：{type_name}",
        f"题目：{_plain(question.get('content'))}",
    ]
    if question.get("options"):
        lines.append("选项：")
        for idx, option in enumerate(question["options"]):
            letter = _LETTERS[idx] if idx < len(_LETTERS) else str(idx + 1)
            lines.append(f"{letter}. {_plain(option.get('content'))} (id={option['id']})")
    if excluded:
        texts = [
            f"{ident}:{_plain(option_map[ident].get('content'))}"
            for ident in excluded
            if ident in option_map
        ]
        if texts:
            lines.append("【排除选项】以下选项已被判定错误，不要选择：" + "；".join(texts))
    if wrong_combos:
        rendered = [
            "、".join(_plain(option_map.get(item, {}).get("content")) or item for item in combo)
            for combo in wrong_combos
        ]
        lines.append("【错误选择方式】历史提交中这些组合已被判错：" + " | ".join(rendered))
    if analysis:
        lines.append("【历史AI解析】" + _plain(analysis))
    if bank_reference:
        lines.append(
            "【第三方题库参考】以下内容来自外部题库, 仅供参考, 请自行判断正确性："
            + _plain(bank_reference)
        )
    if qtype == 3:
        lines.append(
            '只输出 markdown 代码块：```answer\n["答案一", "答案二"]\n```，不要解释。'
        )
    else:
        lines.append(
            '只输出 markdown 代码块：```answer\n[{"id": 选项id}]\n```，不要解释。'
        )
    return "\n".join(lines)


class ZhidaoHomeworkWorker:
    """知到作业答题 worker, 可注入 answerer 与 AI 解析源便于离线测试."""

    def __init__(
        self,
        api,
        *,
        course_id,
        course_name="",
        recruit_id="",
        school_id="",
        store=None,
        answerer=None,
        analysis_api=None,
        auto_submit=False,
        target_score=60.0,
        max_rounds=3,
        sleep=time.sleep,
        question_bank=None,
    ):
        self.api = api
        self.course_id = str(course_id)
        self.course_name = str(course_name or "")
        self.school_id = str(school_id or "")
        self.store = store or AiQuestionStore()
        self.answerer = answerer
        self.analysis_api = analysis_api
        self.auto_submit = bool(auto_submit)
        self.target_score = float(target_score or 0)
        # 0 表示按服务端 maxRedo / 保底 AUTO_MAX_ROUNDS 轮
        self.max_rounds = int(max_rounds or 0)
        self.sleep = sleep
        self.question_bank = question_bank
        self._question_bank_ready = question_bank is not None
        self._ai_user_id = None
        self._recruit_id = str(recruit_id or "")
        self._llm_disabled = False
        self._llm_warned = False

    # ------------------------------------------------------------- answering
    def _get_question_bank(self):
        if self._question_bank_ready:
            return self.question_bank
        self._question_bank_ready = True
        try:
            api_client = getattr(self.api, "client", None)
            proxy = getattr(api_client, "proxy", "") if api_client is not None else ""
            self.question_bank = QuestionBankClient.from_config(proxy=proxy)
        except Exception as exc:
            logger.debug(f"初始化外部题库失败: {exc}")
            self.question_bank = None
        return self.question_bank

    def _bank_reference(self, question: dict) -> str:
        bank = self._get_question_bank()
        if bank is None or not bank.enabled():
            return ""
        try:
            title = _plain(question.get("content"))
            options = [_plain(option.get("content")) for option in question.get("options") or []]
            return bank.query(title, options, question.get("type", 1)) or ""
        except Exception as exc:
            logger.debug(f"外部题库查询降级: {exc}")
            return ""

    def _log_bank_stats(self) -> None:
        if self.question_bank is None:
            return
        try:
            self.question_bank.log_stats()
        except Exception as exc:
            logger.debug(f"外部题库统计输出失败: {exc}")

    def _ask(self, prompt: str) -> str:
        if self.answerer is not None:
            return self.answerer(prompt)
        if not llm.enabled():
            raise ValueError("未配置可用的大模型接口")
        return llm.chat_sync(prompt, json_mode=False, stream=True)

    def _disable_llm(self, exc) -> None:
        self._llm_disabled = True
        if not self._llm_warned:
            self._llm_warned = True
            logger.warn(
                f"大模型不可用({exc}), 本卷后续题目将跳过 LLM 并降级处理.",
                shift=True,
            )
            logger.event("作业作答", 结果="LLM 已禁用", 错误=logger.summarize_exception(exc))

    def _fallback_values(self, question: dict, options: list | None = None) -> list[str]:
        qtype = question.get("type", 1)
        pool = options if options is not None else question.get("options") or []
        if qtype == 3:
            raise ValueError(f"题目 {question.get('eid')} 未获得有效填空答案")
        if not pool:
            raise ValueError(f"题目 {question.get('eid')} 没有可选项")
        count = min(2, len(pool)) if qtype == 2 else 1
        return [str(item["id"]) for item in random.sample(pool, count)]

    def _answer_question(self, question: dict, row: dict | None) -> tuple[list, str]:
        qtype = question.get("type", 1)
        options = question.get("options") or []
        combos = _wrong_combos(row)
        analysis = (row or {}).get("ai_analysis") or ""
        valid_ids = {str(option["id"]) for option in options}

        cached = [str(item) for item in (row or {}).get("correct_options") or []]
        if cached:
            if qtype == 3:
                fills = [item for item in cached if item.strip()]
                if fills:
                    return fills, "题库"
            elif all(item in valid_ids for item in cached):
                return cached, "题库"

        if not options and qtype != 3:
            raise ValueError(f"题目 {question.get('eid')} 没有可选项")
        if self._llm_disabled:
            return self._fallback_values(question), "随机"
        bank_reference = self._bank_reference(question)

        if qtype in (1, 14):
            excluded = []
            for combo in combos:
                excluded.extend(item for item in combo if item not in excluded)
            candidates = [option for option in options if str(option["id"]) not in excluded]
            if len(candidates) == 1:
                return [str(candidates[0]["id"])], "排除法"
            if not candidates:
                candidates = list(options)
            prompt = build_homework_prompt(
                question,
                wrong_combos=combos,
                analysis=analysis,
                excluded=excluded,
                bank_reference=bank_reference,
            )
            excluded_set = set(excluded)
            for _attempt in range(ROLLBACK_RETRIES):
                try:
                    reply = self._ask(prompt)
                except llm.NonRetryableLLMError as exc:
                    self._disable_llm(exc)
                    return self._fallback_values(question, candidates), "随机"
                ids, _ = parse_homework_answer(reply, question)
                ids = [item for item in ids if item in valid_ids]
                if ids and not any(item in excluded_set for item in ids):
                    return ids[:1], "AI"
                prompt += "\n【重要】你上次给出的答案已被判定错误，请重新选择。"
            pool = candidates or options
            return [str(random.choice(pool)["id"])], "随机"

        prompt = build_homework_prompt(
            question,
            wrong_combos=combos,
            analysis=analysis,
            bank_reference=bank_reference,
        )
        if qtype == 3:
            wrong_signatures = {tuple(combo) for combo in combos}
        else:
            wrong_signatures = {tuple(sorted(combo)) for combo in combos}
        last: tuple[list, str] = ([], "")
        for _attempt in range(ROLLBACK_RETRIES):
            try:
                reply = self._ask(prompt)
            except llm.NonRetryableLLMError as exc:
                self._disable_llm(exc)
                return self._fallback_values(question), "随机"
            ids, fills = parse_homework_answer(reply, question)
            if qtype == 3:
                fills = [item for item in fills if item.strip()]
                if not fills:
                    prompt += "\n【重要】答案不能为空，请重新作答。"
                    continue
                last = (fills, "AI")
                if tuple(fills) not in wrong_signatures:
                    return last
            else:
                ids = [item for item in ids if item in valid_ids]
                if not ids:
                    prompt += "\n【重要】你上次没有给出有效选项，请重新作答。"
                    continue
                last = (ids, "AI")
                if tuple(sorted(ids)) not in wrong_signatures:
                    return last
            prompt += "\n【重要】该答案组合已被判定错误，请换一个答案。"
        if last[0]:
            return last
        return self._fallback_values(question), "随机"

    # ---------------------------------------------------------------- storage
    def _exam_key(self, exam_id, homework_id) -> str:
        return str(exam_id or homework_id or "")

    def _storage_course(self, course_id=None) -> str:
        return str(course_id or self.course_id)

    def _remember_question(
        self, question: dict, exam_key: str, course_id=None
    ) -> dict | None:
        course = self._storage_course(course_id)
        eid = question.get("eid")
        if eid:
            self.store.save_options_by_eid(
                course,
                exam_key,
                eid,
                question.get("options") or [],
                question_type=str(question.get("type", 1)),
                content=_plain(question.get("content")),
                course_name=self.course_name,
            )
            row = self.store.get_by_eid(course, eid, exam_key)
            if row:
                return row
        question_id = question.get("question_id")
        if question_id:
            self.store.save_options_by_id(
                course,
                exam_key,
                question_id,
                question.get("options") or [],
                question_type=str(question.get("type", 1)),
                content=_plain(question.get("content")),
                course_name=self.course_name,
            )
            return self.store.get_by_id(course, question_id, exam_key)
        return None

    def _find_row(self, question: dict, exam_key: str, course_id=None) -> dict | None:
        """eid -> 选项哈希 -> 题干, 同卷优先并支持同课程跨测验题库兜底."""
        course = self._storage_course(course_id)
        same_exam_row = None
        eid = question.get("eid")
        if eid:
            same_exam_row = self.store.get_by_eid(course, eid, exam_key)
        hashed = option_hash(question.get("options") or [])
        if same_exam_row is None and hashed:
            same_exam_row = self.store.find_by_hash(course, hashed, exam_key)
        content = _plain(question.get("content"))
        if same_exam_row is None and content:
            same_exam_row = self.store.find_by_content(course, content, exam_key)

        if same_exam_row is not None and (
            same_exam_row.get("correct_options") or same_exam_row.get("ai_analysis")
        ):
            return same_exam_row

        cross_row = None
        if hashed:
            cand = self.store.find_by_hash(course, hashed, exam_id=None)
            if cand and (cand.get("correct_options") or cand.get("ai_analysis")):
                cross_row = cand
        if cross_row is None and content:
            cand = self.store.find_by_content(course, content, exam_id=None)
            if cand and (cand.get("correct_options") or cand.get("ai_analysis")):
                cross_row = cand

        if cross_row is None:
            return same_exam_row

        # 跨测验兜底仅借用 correct_options 与 ai_analysis，不把旧测验的 wrong_options / question_id 混入当前卷
        cross_hint = {
            "correct_options": list(cross_row.get("correct_options") or []),
            "ai_analysis": cross_row.get("ai_analysis") or "",
            "wrong_options": [],
        }
        if same_exam_row is None:
            return cross_hint
        merged = dict(same_exam_row)
        if not merged.get("correct_options") and cross_hint["correct_options"]:
            merged["correct_options"] = cross_hint["correct_options"]
        if not merged.get("ai_analysis") and cross_hint["ai_analysis"]:
            merged["ai_analysis"] = cross_hint["ai_analysis"]
        return merged

    def _save_answer(
        self,
        *,
        question: dict,
        values: list,
        recruit_id,
        course_id,
        homework_id,
        stu_exam_id,
        exam_id,
        school_id="",
    ) -> None:
        answer = format_student_answer(question.get("type", 1), values)
        response = self.api.save_student_answer(
            recruit_id=recruit_id,
            exam_id=exam_id,
            stu_exam_id=stu_exam_id,
            school_id=school_id,
            eid=question["eid"],
            question_type=question.get("type", 1),
            answer=answer,
            exam_type="",
        )
        message = str((response or {}).get("message") or (response or {}).get("msg") or "")
        if any(hint in message.lower() for hint in _CAPTCHA_HINTS):
            from modules.zhs_api import CaptchaRequiredError

            raise CaptchaRequiredError(f"保存答案触发人机验证: {message}")

    # ---------------------------------------------------------------- review
    def _look_homework(self, *, recruit_id, course_id, stu_exam_id, exam_id, school_id):
        try:
            return self.api.look_homework(
                recruit_id=recruit_id,
                student_exam_id=stu_exam_id,
                exam_id=exam_id,
                school_id=school_id,
                course_id=course_id,
            )
        except Exception as exc:
            logger.debug(f"lookHomework 失败: {exc}")
            return {}

    def _review(
        self,
        *,
        recruit_id,
        course_id,
        homework_id,
        stu_exam_id,
        exam_key,
        school_id,
        questions,
    ) -> dict:
        """回读已提交作业: 用数字 question_id 桥接 eid, 再查对错."""
        course = self._storage_course(course_id)
        looked = extract_exam_questions(
            self._look_homework(
                recruit_id=recruit_id,
                course_id=course,
                stu_exam_id=stu_exam_id,
                exam_id=exam_key,
                school_id=school_id,
            )
        )
        qid_by_eid: dict[str, str] = {}
        question_ids: list[str] = []
        for item in looked:
            question_id = item.get("question_id")
            if not question_id:
                continue
            self.store.save_options_by_id(
                course,
                exam_key,
                question_id,
                item.get("options") or [],
                question_type=str(item.get("type", 1)),
                content=_plain(item.get("content")),
                course_name=self.course_name,
            )
            row = self.store.get_by_id(course, question_id, exam_key)
            if row and row.get("eid"):
                qid_by_eid[str(row["eid"])] = question_id
            if question_id not in question_ids:
                question_ids.append(question_id)
        if not question_ids:
            for item in questions:
                question_id = item.get("question_id")
                if question_id and question_id not in question_ids:
                    question_ids.append(question_id)

        info_payload = {}
        if question_ids:
            try:
                info_payload = self.api.get_stu_answer_info(
                    recruit_id=recruit_id,
                    stu_exam_id=stu_exam_id,
                    exam_id=exam_key,
                    school_id=school_id,
                    course_id=course,
                    question_ids=question_ids,
                )
            except Exception as exc:
                logger.debug(f"getStuAnswerInfo 失败: {exc}")
        return {
            "looked": looked,
            "info": answer_info_by_question_id(info_payload),
            "qid_by_eid": qid_by_eid,
        }

    def _analysis_text(self, question_id) -> str:
        if self.analysis_api is None or not str(question_id or "").strip():
            return ""
        if self._ai_user_id is None:
            self._ai_user_id = self.analysis_api.get_user_id(
                self.course_id, self._recruit_id
            )
        return self.analysis_api.analyze(
            question_id=question_id,
            user_id=self._ai_user_id,
            course_id=self.course_id,
            recruit_id=self._recruit_id,
        )

    def _apply_review(
        self,
        questions: list[dict],
        review: dict,
        exam_key: str,
        *,
        course_id=None,
    ) -> None:
        course = self._storage_course(course_id)
        looked = {item.get("question_id"): item for item in review.get("looked") or []}
        info = review.get("info") or {}
        qid_by_eid = review.get("qid_by_eid") or {}
        for question in questions:
            eid = question.get("eid")
            question_id = (
                qid_by_eid.get(str(eid))
                or question.get("question_id")
                or ""
            )
            row = None
            if eid:
                row = self.store.get_by_eid(course, eid, exam_key)
            if not question_id and row:
                question_id = str(row.get("question_id") or "")
            if not question_id:
                bridged = self._find_row(question, exam_key, course)
                if bridged:
                    question_id = str(bridged.get("question_id") or "")
                    row = bridged
            if not question_id:
                continue
            looked_item = looked.get(question_id)
            entry = info.get(question_id) or (info.get(eid) if eid else None)
            correct = _entry_flag(entry, "isRight", "isCorrect", "correct")
            if not correct and looked_item is not None:
                correct = _entry_flag(looked_item.get("raw"), "isRight", "isCorrect")
            answer_text = _entry_answer(entry) or (
                _entry_answer(looked_item.get("raw")) if looked_item else ""
            )
            correct_options = []
            if looked_item is not None:
                correct_options = correct_option_ids(looked_item.get("raw") or {})
            if not correct_options and row:
                correct_options = row.get("correct_options") or []

            if correct:
                self.store.mark_correct(
                    course,
                    exam_key,
                    question_id=question_id,
                    correct_options=correct_options,
                    auto_clear_wrong=True,
                )
                continue

            qtype = question.get("type", 1)
            wrong = _split_user_answer(answer_text, qtype)
            if wrong:
                self.store.record_wrong(
                    course,
                    exam_key,
                    question_id=question_id,
                    wrong_options=wrong,
                    preserve_order=(qtype == 3),
                )
            if self.analysis_api is None:
                continue
            row = row or self.store.get_by_id(course, question_id, exam_key) or {}
            if row.get("ai_analysis"):
                continue
            text = self._analysis_text(question_id)
            if text:
                self.store.mark_correct(
                    course,
                    exam_key,
                    question_id=question_id,
                    correct_options=row.get("correct_options") or [],
                    ai_analysis=text,
                    auto_clear_wrong=False,
                )

    # ------------------------------------------------------------------- scan
    def scan(
        self,
        flag: int = 1,
        *,
        recruit_id="",
        course_id="",
        page_size: int = 100,
        max_pages: int = 20,
    ) -> list[dict]:
        """分页拉取作业列表, flag=1 未提交, flag=2 已提交未达标."""
        effective_recruit = str(recruit_id or self._recruit_id or "")
        effective_course = str(course_id or self.course_id or "")
        items: list[dict] = []
        seen: set[tuple[str, str]] = set()
        page_num = 0
        for _ in range(max_pages):
            payload = self.api.get_student_homework(
                recruit_id=effective_recruit,
                course_id=effective_course,
                flag=flag,
                page_size=page_size,
                page_num=page_num,
            )
            page = extract_homework_list(payload)
            for item in page["items"]:
                key = (item.get("studentExamId", ""), item.get("examId", ""))
                if key in seen:
                    continue
                seen.add(key)
                items.append(item)
            next_page = page.get("next")
            if next_page is None or next_page <= page_num:
                break
            page_num = next_page
        return items

    def run_pending(
        self,
        *,
        recruit_id="",
        course_id="",
        school_id="",
        flag: int = 1,
        now=None,
    ) -> list[dict]:
        """扫描指定状态的作业并逐份作答, 返回每份作业的统计."""
        effective_recruit = str(recruit_id or self._recruit_id or "")
        if effective_recruit and not self._recruit_id:
            self._recruit_id = effective_recruit
        results = []
        for item in self.scan(
            flag=flag, recruit_id=effective_recruit, course_id=course_id
        ):
            exam_id = item.get("examId", "")
            stu_exam_id = item.get("studentExamId", "")
            exam_name = item.get("examName", "")
            item_recruit = item.get("recruitId") or effective_recruit
            item_course = item.get("courseId") or course_id or self.course_id
            item_school = (
                item.get("schoolId")
                or (item.get("raw") or {}).get("schoolId")
                or school_id
                or self.school_id
            )
            raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
            in_window, reason = check_time_window(
                item.get("startTime") or raw.get("startTime"),
                item.get("endDate") or raw.get("endDate"),
                now=now,
            )
            if not in_window:
                label = "尚未开放" if reason == "not_started" else "已截止"
                logger.info(f"作业「{exam_name or exam_id}」{label}, 跳过.")
                results.append(
                    {
                        "homework_id": exam_id,
                        "exam_name": exam_name,
                        "student_exam_id": stu_exam_id,
                        "recruit_id": item_recruit,
                        "course_id": item_course,
                        "school_id": str(item_school or ""),
                        "skipped": True,
                        "reason": reason,
                        "answered": 0,
                        "submitted": False,
                    }
                )
                continue
            stats = self.run(
                recruit_id=item_recruit,
                course_id=item_course,
                homework_id=exam_id,
                stu_exam_id=stu_exam_id,
                exam_id=exam_id,
                school_id=str(item_school or ""),
            )
            stats["homework_id"] = exam_id
            stats["exam_name"] = exam_name
            stats["student_exam_id"] = stu_exam_id
            stats["recruit_id"] = item_recruit
            stats["course_id"] = item_course
            stats["school_id"] = str(item_school or "")
            results.append(stats)
        return results

    # ------------------------------------------------------------------- run
    def run(
        self,
        *,
        recruit_id,
        course_id,
        homework_id,
        stu_exam_id,
        exam_id="",
        school_id="",
    ) -> dict:
        try:
            return self._run(
                recruit_id=recruit_id,
                course_id=course_id,
                homework_id=homework_id,
                stu_exam_id=stu_exam_id,
                exam_id=exam_id,
                school_id=school_id,
            )
        finally:
            self._log_bank_stats()

    def _open_homework(
        self,
        *,
        recruit_id,
        course_id,
        exam_key,
        stu_exam_id,
        school_id,
        allow_reset=True,
    ):
        try:
            payload = self.api.do_homework(
                recruit_id=recruit_id,
                exam_id=exam_key,
                student_exam_id=stu_exam_id,
                school_id=school_id,
                course_id=course_id,
            )
        except ZhsApiError as exc:
            if not allow_reset or not any(hint in str(exc) for hint in _RESET_HINTS):
                raise
            logger.warn("作业已提交或不可继续作答, 先重置状态再重试...")
            self.api.save_course_tran(
                recruit_id=recruit_id,
                exam_id=exam_key,
                course_id=course_id,
            )
            return self.api.do_homework(
                recruit_id=recruit_id,
                exam_id=exam_key,
                student_exam_id=stu_exam_id,
                school_id=school_id,
                course_id=course_id,
            )
        if allow_reset and payload_needs_reset(payload):
            logger.warn("作业已提交或不可继续作答, 先重置状态再重试...")
            self.api.save_course_tran(
                recruit_id=recruit_id,
                exam_id=exam_key,
                course_id=course_id,
            )
            payload = self.api.do_homework(
                recruit_id=recruit_id,
                exam_id=exam_key,
                student_exam_id=stu_exam_id,
                school_id=school_id,
                course_id=course_id,
            )
        return payload

    def _round_limit(self, payload) -> int:
        if self.max_rounds > 0:
            return self.max_rounds
        return payload_max_redo(payload) or AUTO_MAX_ROUNDS

    @staticmethod
    def _merge_row(remembered: dict | None, existing: dict | None) -> dict | None:
        if remembered is None:
            return existing
        if existing is None:
            return remembered
        same_record = (
            remembered.get("id") is not None
            and remembered.get("id") == existing.get("id")
        )
        merged = dict(remembered)
        if not merged.get("correct_options") and existing.get("correct_options"):
            merged["correct_options"] = list(existing["correct_options"])
        if not merged.get("ai_analysis") and existing.get("ai_analysis"):
            merged["ai_analysis"] = existing["ai_analysis"]
        if not same_record:
            if not merged.get("question_id") and existing.get("question_id"):
                merged["question_id"] = existing["question_id"]
            existing_wrong = _wrong_combos(existing)
            if existing_wrong:
                combined = _wrong_combos(merged)
                for combo in existing_wrong:
                    if combo not in combined:
                        combined.append(combo)
                merged["wrong_options"] = combined
        return merged

    def _run(
        self,
        *,
        recruit_id,
        course_id,
        homework_id,
        stu_exam_id,
        exam_id="",
        school_id="",
    ) -> dict:
        self._recruit_id = str(recruit_id or "")
        exam_key = self._exam_key(exam_id, homework_id)
        stats = {
            "answered": 0,
            "submitted": False,
            "score": None,
            "score_percent": None,
            "rounds": 0,
            "needs_vision": False,
            "target_reached": False,
            "is_marking": 0,
        }
        payload = self._open_homework(
            recruit_id=recruit_id,
            course_id=course_id,
            exam_key=exam_key,
            stu_exam_id=stu_exam_id,
            school_id=school_id,
        )
        questions = extract_exam_questions(payload)
        if not questions:
            return stats

        text_questions = [q for q in questions if not question_has_image(q)]
        if len(text_questions) < len(questions):
            stats["needs_vision"] = True
            stats["image_questions"] = len(questions) - len(text_questions)
            logger.warn(
                f"作业含 {stats['image_questions']} 道图片题, 先接口保存其余 "
                f"{len(text_questions)} 道纯文本题, 图片题回退视觉模型."
            )
            questions = text_questions
        if not questions:
            return stats

        limit = self._round_limit(payload)
        while True:
            stats["rounds"] += 1
            answered = 0
            for question in questions:
                existing = self._find_row(question, exam_key, course_id)
                remembered = self._remember_question(question, exam_key, course_id)
                row = self._merge_row(remembered, existing)
                values, source = self._answer_question(question, row)
                if source in ("题库", "随机", "排除法"):
                    self.sleep(random.uniform(1.5, 3.0))
                self._save_answer(
                    question=question,
                    values=values,
                    recruit_id=recruit_id,
                    course_id=course_id,
                    homework_id=homework_id,
                    stu_exam_id=stu_exam_id,
                    exam_id=exam_key,
                    school_id=school_id,
                )
                answered += 1
                logger.info(
                    f"作业作答 {question['eid']}({source}): {question.get('content', '')[:40]}"
                )
            stats["answered"] = answered

            if not self.auto_submit:
                logger.info("作业答案已保存, 等待用户手动提交.")
                return stats
            if stats["needs_vision"]:
                logger.info("存在图片题, 本次不自动提交; 等待视觉流程补答后再处理.")
                return stats

            submitted = self.api.submit(
                recruit_id=recruit_id,
                exam_id=exam_key,
                stu_exam_id=stu_exam_id,
                achieve_count=answered,
            )
            stats["submitted"] = True
            stats["score"] = extract_score(submitted)
            total_score = payload_total_score(payload)
            if stats["score"] is not None and total_score > 0:
                stats["score_percent"] = stats["score"] / total_score * 100
            else:
                stats["score_percent"] = None
            if stats["score_percent"] is not None:
                reached = stats["score_percent"] >= self.target_score
            elif stats["score"] is not None:
                reached = stats["score"] >= self.target_score
            else:
                reached = False
            if reached:
                stats["target_reached"] = True
                logger.info(
                    f"作业得分 {stats['score']}/{total_score:g} 已达标, 停止重做."
                )
                return stats

            review = self._review(
                recruit_id=recruit_id,
                course_id=course_id,
                homework_id=homework_id,
                stu_exam_id=stu_exam_id,
                exam_key=exam_key,
                school_id=school_id,
                questions=questions,
            )
            self._apply_review(questions, review, exam_key, course_id=course_id)

            if stats["rounds"] >= limit:
                break
            marking = payload_is_marking(payload)
            if marking is None:
                stats["is_marking"] = stats["rounds"]
            else:
                stats["is_marking"] = marking
                server_limit = payload_max_redo(payload) or limit
                if self.max_rounds == 0 and marking >= server_limit:
                    logger.warn("作业重做次数已达上限, 停止重做.")
                    break
            self.api.save_course_tran(
                recruit_id=recruit_id,
                exam_id=exam_key,
                course_id=course_id,
            )
            payload = self._open_homework(
                recruit_id=recruit_id,
                course_id=course_id,
                exam_key=exam_key,
                stu_exam_id=stu_exam_id,
                school_id=school_id,
                allow_reset=False,
            )
            refreshed = extract_exam_questions(payload)
            if not refreshed:
                break
            questions = [q for q in refreshed if not question_has_image(q)]
            if not questions:
                break
            logger.info(f"作业未达标({stats['score']}), 开始第 {stats['rounds'] + 1} 轮重做.")

        return stats