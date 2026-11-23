# encoding=utf-8
"""知到作业 API 答题 worker: 答题 -> 提交 -> 查错 -> 官方解析 -> 重做达标."""
from __future__ import annotations

import json
import random
import re
import time

from modules import llm
from modules.answer_store import AiQuestionStore
from modules.html_utils import extract_plain_text, has_meaningful_image
from modules.logger import Logger
from modules.zhidao_homework_api import (
    QUESTION_TYPE_NAMES,
    build_student_answer_item,
    extract_exam_questions,
    extract_score,
    format_student_answer,
    parse_review,
)

logger = Logger()

_ANSWER_BLOCK_RE = re.compile(r"```answer\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_THINK_RE = re.compile(r"<think\b[^>]*>.*?", re.DOTALL | re.IGNORECASE)
_THINK_TAG_RE = re.compile(r"</?think[^>]*>", re.IGNORECASE)
_LIST_RE = re.compile(r"\[.*\]", re.DOTALL)
_LETTERS = "ABCDEFGH"

ROLLBACK_RETRIES = 3
_CAPTCHA_HINTS = ("滑块", "验证码", "captcha", "slider")


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


def build_homework_prompt(
    question: dict,
    *,
    wrong_combos: list[list[str]] | None = None,
    analysis: str = "",
    excluded: list[str] | None = None,
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
            "、".join(
                option_map.get(item, {}).get("content") or item for item in combo
            )
            for combo in wrong_combos
        ]
        lines.append("【错误选择方式】历史提交中这些组合已被判错：" + " | ".join(rendered))
    if analysis:
        lines.append("【历史AI解析】" + _plain(analysis))
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
        store=None,
        answerer=None,
        analysis_api=None,
        auto_submit=False,
        target_score=60.0,
        max_rounds=3,
        sleep=time.sleep,
    ):
        self.api = api
        self.course_id = str(course_id)
        self.course_name = str(course_name or "")
        self.store = store or AiQuestionStore()
        self.answerer = answerer
        self.analysis_api = analysis_api
        self.auto_submit = bool(auto_submit)
        self.target_score = float(target_score or 0)
        self.max_rounds = max(1, int(max_rounds or 1))
        self.sleep = sleep
        self._ai_user_id = None
        self._recruit_id = ""

    # ------------------------------------------------------------- answering
    def _ask(self, prompt: str) -> str:
        if self.answerer is not None:
            return self.answerer(prompt)
        if not llm.enabled():
            raise ValueError("未配置可用的大模型接口")
        return llm.chat_sync(prompt, json_mode=False, stream=True)

    def _answer_question(self, question: dict, row: dict | None) -> tuple[list, str]:
        qtype = question.get("type", 1)
        options = question.get("options") or []
        combos = _wrong_combos(row)
        analysis = (row or {}).get("ai_analysis") or ""
        valid_ids = {str(option["id"]) for option in options}

        cached = [str(item) for item in (row or {}).get("correct_options") or []]
        if cached and all(item in valid_ids for item in cached):
            return cached, "题库"

        if not options and qtype != 3:
            raise ValueError(f"题目 {question.get('eid')} 没有可选项")

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
                question, wrong_combos=combos, analysis=analysis, excluded=excluded
            )
            excluded_set = set(excluded)
            for attempt in range(ROLLBACK_RETRIES):
                ids, _ = parse_homework_answer(self._ask(prompt), question)
                ids = [item for item in ids if item in valid_ids]
                if ids and not any(item in excluded_set for item in ids):
                    return ids[:1], "AI"
                prompt += "\n【重要】你上次给出的答案已被判定错误，请重新选择。"
            # 全被排除或多次无效: 从剩余候选中兜底
            pool = candidates or options
            return [str(random.choice(pool)["id"])], "随机"

        prompt = build_homework_prompt(question, wrong_combos=combos, analysis=analysis)
        wrong_signatures = {tuple(sorted(combo)) for combo in combos}
        last: tuple[list, str] = ([], "")
        for attempt in range(ROLLBACK_RETRIES):
            ids, fills = parse_homework_answer(self._ask(prompt), question)
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
        if qtype == 3:
            raise ValueError(f"题目 {question.get('eid')} 未获得有效填空答案")
        pool = options
        count = min(2, len(pool)) if qtype == 2 else 1
        return [str(item["id"]) for item in random.sample(pool, count)], "随机"

    # ---------------------------------------------------------------- storage
    def _exam_key(self, exam_id, homework_id) -> str:
        return str(exam_id or homework_id or "")

    def _remember_question(self, question: dict, exam_key: str) -> dict | None:
        self.store.save_options_by_eid(
            self.course_id,
            exam_key,
            question["eid"],
            question.get("options") or [],
            question_type=str(question.get("type", 1)),
            content=question.get("content", ""),
            course_name=self.course_name,
        )
        return self.store.get_by_eid(self.course_id, question["eid"], exam_key)

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
    ) -> None:
        answer = format_student_answer(question.get("type", 1), values)
        item = build_student_answer_item(
            eid=question["eid"],
            question_type=question.get("type", 1),
            answer=answer,
            exam_id=exam_id,
            exam_type="",
        )
        response = self.api.save_student_answer(
            recruit_id=recruit_id,
            course_id=course_id,
            homework_id=homework_id,
            stu_exam_id=stu_exam_id,
            answer_item=item,
        )
        message = str((response or {}).get("message") or (response or {}).get("msg") or "")
        if any(hint in message.lower() for hint in _CAPTCHA_HINTS):
            from modules.zhs_api import CaptchaRequiredError

            raise CaptchaRequiredError(f"保存答案触发人机验证: {message}")

    # ---------------------------------------------------------------- review
    def _review(self, *, recruit_id, course_id, homework_id, stu_exam_id) -> dict:
        try:
            looked = self.api.look_homework(
                recruit_id=recruit_id,
                course_id=course_id,
                homework_id=homework_id,
                stu_exam_id=stu_exam_id,
            )
        except Exception as exc:
            logger.debug(f"lookHomework 失败: {exc}")
            looked = {}
        try:
            info = self.api.get_stu_answer_info(
                recruit_id=recruit_id,
                course_id=course_id,
                homework_id=homework_id,
                stu_exam_id=stu_exam_id,
            )
        except Exception as exc:
            logger.debug(f"getStuAnswerInfo 失败: {exc}")
            info = {}
        return parse_review(looked, info)

    def _analysis_text(self, question: dict) -> str:
        if self.analysis_api is None:
            return ""
        if self._ai_user_id is None:
            self._ai_user_id = self.analysis_api.get_user_id(
                self.course_id, self._recruit_id
            )
        return self.analysis_api.analyze(
            question,
            user_id=self._ai_user_id,
            course_id=self.course_id,
            recruit_id=self._recruit_id,
        )

    def _apply_review(self, questions: list[dict], review: dict, exam_key: str) -> None:
        for question in questions:
            eid = question["eid"]
            info = review.get(eid)
            if not info:
                continue
            row = self.store.get_by_eid(self.course_id, eid, exam_key) or {}
            if info.get("correct"):
                correct = info.get("correct_options") or row.get("correct_options") or []
                self.store.mark_correct(
                    self.course_id, exam_key, eid=eid, correct_options=correct
                )
                continue
            wrong = [
                item
                for item in str(info.get("user_answer") or "").split(",")
                if item.strip()
            ]
            if wrong:
                self.store.record_wrong(
                    self.course_id, exam_key, eid=eid, wrong_options=wrong
                )
            if self.analysis_api is not None and not row.get("ai_analysis"):
                text = self._analysis_text(question)
                if text:
                    self.store.mark_correct(
                        self.course_id,
                        exam_key,
                        eid=eid,
                        correct_options=row.get("correct_options") or [],
                        ai_analysis=text,
                        auto_clear_wrong=False,
                    )

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
        self._recruit_id = str(recruit_id or "")
        exam_key = self._exam_key(exam_id, homework_id)
        stats = {
            "answered": 0,
            "submitted": False,
            "score": None,
            "rounds": 0,
            "needs_vision": False,
            "target_reached": False,
            "is_marking": 0,
        }
        payload = self.api.do_homework(
            recruit_id=recruit_id,
            course_id=course_id,
            homework_id=homework_id,
            stu_exam_id=stu_exam_id,
            school_id=school_id,
        )
        questions = extract_exam_questions(payload)
        if not questions:
            return stats

        if any(question_has_image(question) for question in questions):
            logger.warn("作业含图片题, 回退视觉模型逐题作答.")
            stats["needs_vision"] = True
            return stats

        for round_index in range(self.max_rounds):
            stats["rounds"] = round_index + 1
            answered = 0
            for question in questions:
                row = self._remember_question(question, exam_key)
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
                    exam_id=exam_id,
                )
                answered += 1
                logger.info(
                    f"作业作答 {question['eid']}({source}): {question.get('content', '')[:40]}"
                )
            stats["answered"] = answered

            if not self.auto_submit:
                logger.info("作业答案已保存, 等待用户手动提交.")
                return stats

            submitted = self.api.submit(
                recruit_id=recruit_id,
                course_id=course_id,
                homework_id=homework_id,
                stu_exam_id=stu_exam_id,
                achieve_count=answered,
            )
            stats["submitted"] = True
            stats["score"] = extract_score(submitted)
            if stats["score"] is not None and stats["score"] >= self.target_score:
                stats["target_reached"] = True
                logger.info(f"作业得分 {stats['score']} 已达标, 停止重做.")
                return stats

            review = self._review(
                recruit_id=recruit_id,
                course_id=course_id,
                homework_id=homework_id,
                stu_exam_id=stu_exam_id,
            )
            self._apply_review(questions, review, exam_key)

            if round_index + 1 >= self.max_rounds:
                break
            # 同步递增内存快照, 避免服务端重做计数未刷新导致死循环
            stats["is_marking"] += 1
            self.api.save_course_tran(
                recruit_id=recruit_id,
                course_id=course_id,
                homework_id=homework_id,
                stu_exam_id=stu_exam_id,
            )
            payload = self.api.do_homework(
                recruit_id=recruit_id,
                course_id=course_id,
                homework_id=homework_id,
                stu_exam_id=stu_exam_id,
                school_id=school_id,
            )
            refreshed = extract_exam_questions(payload)
            if not refreshed:
                break
            questions = refreshed
            logger.info(f"作业未达标({stats['score']}), 开始第 {round_index + 2} 轮重做.")

        return stats