# encoding=utf-8
"""智慧树 AI 课程计分考试的接口版实现（核心上下文 + 兼容导出门面）.

流程: 取卷 -> 逐题作答 -> 保存答案 -> 交卷 -> 回读正确答案写回题库.
未命中题库时优先用智慧树 AI 生成答案, 失败再退到 data/llm.json 配置的自定义大模型,
最后随机选, 因此第一轮即使有错题, 后续轮次也能命中题库.

接口客户端与题库位于 ``modules/exam_api.py``，AI 答题通道位于 ``modules/zhidao_ai.py``，
运行器与复习页接管位于 ``modules/ai_exam_runner.py``；本模块保留全部对外导出符号。
"""
from __future__ import annotations

import random
import threading
import time
from abc import ABC, abstractmethod

import requests

from modules import llm
from modules.answer_parser import (
    QUESTION_TYPE_NAMES,  # noqa: F401
    answer_separator,
    parse_answer_ids,  # noqa: F401
)
from modules.answer_service import LlmCircuitBreaker, QuestionBankHelper
from modules.exam_api import (  # noqa: F401
    AI_EXAM_PAPER,
    AnswerBank,
    CaptchaRequiredError,
    DEFAULT_PROMPT_CHAR_LIMIT,
    EXAM_BASE,
    EXAM_OPEN,
    EXAM_QUESTION,
    EXAM_SAVE_ANSWER,
    EXAM_SHEET,
    EXAM_SUBMIT,
    EXAM_USED_TIME,
    ExamApi,
    ExamApiError,
    FORMAL_ANSWER_SHEET,
    FORMAL_OPEN_DETAIL,
    FORMAL_SAVE_BATCH,
    FORMAL_START_PULL,
    FORMAL_TASK_LIST,
    KNOWLEDGE_RESOURCE_LIST,
    MATERIAL_TRUNCATED_MARK,
    MAX_EXAM_ATTEMPTS,
    SessionExpiredError,
    SessionStatus,
    _brief_html,
    _format_answer_choices,
    _trim_reference_materials,
    as_number,
    build_answer_prompt,
    build_fill_prompt,
    build_session,
    is_flagged,
    is_free_exam,
    normalize_free_exam,
    parse_review_exam_url,
    select_random_answers,
)
from modules.html_utils import extract_plain_text
from modules.logger import Logger
from modules.question_bank import QuestionBankClient  # noqa: F401
from modules.zhidao_ai import (  # noqa: F401
    DeepSeekAnswerer,
    LlmAnswerer,
    ZHIDAO_AI_CHAT,
    ZHIDAO_AI_HEADERS,
    ZhidaoAnswerer,
)

logger = Logger()


class AiExamBase(ABC):
    """AI 智慧课程考试/作业的模板方法基类.

    公共流程: 载入题库 -> 打开试卷 -> 读取答题卡 -> 逐题作答 -> 收尾.
    子类通过 _fetch_sheet_items / _on_answered / _flush_pending / _finish 定制差异.
    """

    exam_label = "接口考试"
    # 是否允许跨 exam_id 检索同课程题库 (仅综合考试开启)
    _cross_exam_search = False
    question_bank_scope = ""

    def __init__(
        self,
        api: ExamApi,
        course_id,
        knowledge_id,
        exam_test_id,
        exam_paper_id,
        op_extra: dict | None = None,
        answer_bank: AnswerBank | None = None,
        question_bank=None,
        cancel_event: threading.Event | None = None,
    ):
        self.api = api
        self.course_id = course_id
        self.knowledge_id = knowledge_id
        self.exam_test_id = exam_test_id
        self.exam_paper_id = exam_paper_id
        self.op_extra = op_extra or {}
        self.answer_bank = answer_bank or AnswerBank(course_id)
        self._bank_helper = QuestionBankHelper(
            question_bank,
            proxy=getattr(api, "proxy", ""),
            scope=self.question_bank_scope,
        )
        self.cancel_event = cancel_event
        self.reference_materials: list[dict] = []
        self.sheet_content: list | None = None
        self._stopped = threading.Event()
        self._heartbeat: threading.Thread | None = None
        self._heartbeat_lock = threading.Lock()
        self._failed_saves = 0
        clone_fn = getattr(api, "clone_session", None)
        api_session = getattr(api, "session", None)
        if callable(clone_fn) and isinstance(api_session, requests.Session):
            ai_session = clone_fn()
            self.zhidao_answerer = ZhidaoAnswerer(
                ai_session, lock=None, owns_session=True
            )
        else:
            self.zhidao_answerer = ZhidaoAnswerer(
                api_session, lock=None, owns_session=False
            )
        self.deepseek_answerer = DeepSeekAnswerer(getattr(api, "proxy", ""))
        self._last_ai_provider = ""
        self._llm_breaker = LlmCircuitBreaker()

    @property
    def question_bank(self):
        return self._bank_helper.client

    @question_bank.setter
    def question_bank(self, value) -> None:
        self._bank_helper.client = value

    @property
    def _question_bank_ready(self) -> bool:
        return self._bank_helper.ready

    @_question_bank_ready.setter
    def _question_bank_ready(self, value: bool) -> None:
        self._bank_helper.ready = bool(value)

    @property
    def _owns_question_bank(self) -> bool:
        return self._bank_helper.owns_client

    @_owns_question_bank.setter
    def _owns_question_bank(self, value: bool) -> None:
        self._bank_helper.owns_client = bool(value)

    @property
    def _llm_disabled(self) -> bool:
        return self._llm_breaker.disabled

    @_llm_disabled.setter
    def _llm_disabled(self, value: bool) -> None:
        self._llm_breaker.disabled = bool(value)

    @property
    def _llm_warned(self) -> bool:
        return self._llm_breaker.warned

    @_llm_warned.setter
    def _llm_warned(self, value: bool) -> None:
        self._llm_breaker.warned = bool(value)

    def close(self) -> None:
        self._stop_heartbeat()
        close_ai = getattr(self.zhidao_answerer, "close", None)
        if callable(close_ai):
            try:
                close_ai()
            except Exception:
                pass
        self._bank_helper.close()

    def __enter__(self) -> "AiExamBase":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _is_cancelled(self) -> bool:
        return self.cancel_event is not None and self.cancel_event.is_set()

    def _sleep_or_cancel(self, seconds: float) -> bool:
        if self.cancel_event is not None:
            return self.cancel_event.wait(seconds)
        time.sleep(seconds)
        return False

    def _ensure_heartbeat(self) -> None:
        """启动考试计时心跳线程；已有存活心跳线程时绝不重复拉起。"""
        with self._heartbeat_lock:
            if self._heartbeat is not None and self._heartbeat.is_alive():
                return
            self._stopped.clear()
            self._heartbeat = threading.Thread(
                target=self._update_used_time,
                daemon=True,
                name=f"exam-heartbeat-{self.exam_test_id}",
            )
            self._heartbeat.start()

    def _stop_heartbeat(self, timeout: float = 2.0) -> None:
        self._stopped.set()
        with self._heartbeat_lock:
            hb = self._heartbeat
        if hb is not None and hb.is_alive() and hb is not threading.current_thread():
            hb.join(timeout=timeout)
        with self._heartbeat_lock:
            if self._heartbeat is hb and (hb is None or not hb.is_alive()):
                self._heartbeat = None

    def open_exam(self, tried: int = 0) -> None:
        data = {
            "examTestId": self.exam_test_id,
            "examPaperId": self.exam_paper_id,
            "courseId": self.course_id,
        }
        max_attempts = 4
        for attempt in range(max(0, tried), max_attempts):
            try:
                self.api.query(EXAM_OPEN, data)
                break
            except Exception as exc:
                if attempt + 1 >= max_attempts or self._is_cancelled():
                    raise
                logger.warn(f"打开考试失败, 重试第 {attempt + 1} 次: {exc}")
                self._sleep_or_cancel(min(2.0, 0.5 * (2 ** (attempt - max(0, tried)))))
        self._ensure_heartbeat()

    def _update_used_time(self, heartbeat_seconds: int = 10) -> None:
        data = {
            "examTestId": self.exam_test_id,
            "examPaperId": self.exam_paper_id,
            "heartbeatTime": heartbeat_seconds,
        }
        while not self._stopped.is_set() and not self._is_cancelled():
            try:
                self.api.query(EXAM_USED_TIME, data)
            except Exception as exc:
                logger.debug(f"考试计时心跳失败: {exc}")
            self._stopped.wait(heartbeat_seconds)

    def get_sheet_content(self, tried: int = 0) -> list:
        if self.sheet_content is not None:
            return self.sheet_content
        data = {
            "examTestId": self.exam_test_id,
            "examPaperId": self.exam_paper_id,
        }
        max_attempts = 4
        for attempt in range(max(0, tried), max_attempts):
            try:
                ret = self.api.query(EXAM_SHEET, data, method="GET")
                self.sheet_content = ret["data"]["partSheetVos"][0]["questionSheetVos"]
                return self.sheet_content
            except Exception as exc:
                if attempt + 1 >= max_attempts or self._is_cancelled():
                    raise
                logger.warn(f"读取试卷失败, 重试第 {attempt + 1} 次: {exc}")
                self._sleep_or_cancel(min(2.0, 0.5 * (2 ** (attempt - max(0, tried)))))
        return self.sheet_content or []

    def get_question_content(self, question_id, version: int, tried: int = 0) -> dict | None:
        data = {
            "examTestId": self.exam_test_id,
            "examPaperId": self.exam_paper_id,
            "questionId": question_id,
            "version": version,
        }
        max_attempts = 4
        for attempt in range(max(0, tried), max_attempts):
            try:
                ret = self.api.query(EXAM_QUESTION, data, method="GET")
                return ret.get("data")
            except Exception as exc:
                if attempt + 1 >= max_attempts or self._is_cancelled():
                    logger.error(f"读取题目失败: {question_id} {exc}")
                    return None
                logger.debug(f"读取题目失败, 重试第 {attempt + 1} 次: {exc}")
                self._sleep_or_cancel(min(1.5, 0.3 * (2 ** (attempt - max(0, tried)))))
        return None

    def save_answer(self, question_id, answers: list, question_type=None) -> bool:
        if not answers:
            return False
        data = {
            "recruitId": self.course_id,
            "examTestId": self.exam_test_id,
            "examPaperId": self.exam_paper_id,
            "questionId": question_id,
            "dataVos": None,
            "answer": answer_separator(question_type).join(
                str(answer) for answer in answers
            ),
        }
        try:
            self.api.query(EXAM_SAVE_ANSWER, data)
        except Exception as exc:
            logger.error(f"保存答案失败: {question_id} {exc}")
            return False
        return True

    def submit_exam(self, tried: int = 0) -> None:
        """非幂等交卷接口：禁用 urllib3 POST 重试，绝不盲目递归重发。"""
        data = {
            "examTestId": self.exam_test_id,
            "courseId": self.course_id,
            "courseType": 8,
            "examPaperId": self.exam_paper_id,
            "aiKnlowledgeId": self.knowledge_id,
        }
        try:
            if isinstance(self.api, ExamApi):
                self.api.query(EXAM_SUBMIT, data, allow_retry=False)
            else:
                self.api.query(EXAM_SUBMIT, data)
        finally:
            self._stopped.set()

    def _get_question_bank(self):
        """懒加载外部题库客户端; 未配置 token 时返回 None."""
        return self._bank_helper.get()

    def _bank_result(self, question: dict, question_type, choices: list[dict]):
        title = extract_plain_text(question.get("content", ""))
        options = [extract_plain_text(choice.get("content", "")) for choice in choices]
        return self._bank_helper.query_result(title, options, question_type)

    def _bank_reference(self, question: dict, question_type, choices: list[dict]) -> str:
        result = self._bank_result(question, question_type, choices)
        return result.answer if result is not None else ""

    def _build_prompt_extra(
        self, question: dict, question_type, choices: list[dict]
    ) -> dict:
        extra = dict(self.op_extra)
        if (
            type(self)._bank_reference is AiExamBase._bank_reference
            and "_bank_reference" not in self.__dict__
        ):
            result = self._bank_result(question, question_type, choices)
            if result is not None and result.answer:
                extra["题库参考"] = result.answer
                extra["题库AI生成"] = result.ai
        else:
            reference = self._bank_reference(question, question_type, choices)
            if reference:
                extra["题库参考"] = reference
        return extra

    def _generate_answer(self, question_type: int, question: dict, choices: list[dict]) -> list:
        extra = self._build_prompt_extra(question, question_type, choices)
        prompt = build_answer_prompt(
            question_type,
            question.get("content", ""),
            choices,
            self.reference_materials,
            extra,
            max_chars=self.op_extra.get("promptMaxChars"),
        )
        by_text = {str(choice["id"]): choice["id"] for choice in choices}
        try:
            name, ids = self._llm_breaker.run_fallback_chain(
                [
                    ("智慧树AI", lambda: self.zhidao_answerer.answer(prompt), False),
                    (llm.provider_label(), lambda: self.deepseek_answerer.answer(prompt), True),
                ],
                validate=lambda raw: [
                    by_text[str(item)] for item in (raw or []) if str(item) in by_text
                ],
                fail_log_prefix="答题",
            )
        except ValueError:
            raise ValueError("AI 未给出可用的选项 id")
        self._last_ai_provider = name
        logger.debug(f"{name} 给出有效选项: {ids}")
        return ids

    def _generate_fill_answer(self, question: dict) -> list:
        extra = self._build_prompt_extra(question, 3, [])
        prompt = build_fill_prompt(
            question.get("content", ""),
            self.reference_materials,
            extra,
            max_chars=self.op_extra.get("promptMaxChars"),
        )
        try:
            name, cleaned = self._llm_breaker.run_fallback_chain(
                [
                    ("智慧树AI", lambda: self.zhidao_answerer.answer(prompt), False),
                    (llm.provider_label(), lambda: self.deepseek_answerer.answer(prompt), True),
                ],
                validate=lambda raw: [
                    str(item).strip() for item in (raw or []) if str(item).strip()
                ],
                fail_log_prefix="填空作答",
            )
        except ValueError:
            raise ValueError("AI 未给出有效的填空答案")
        self._last_ai_provider = name
        return cleaned

    def _cached_answer(
        self, question: dict, question_id, version: int, valid_ids: set
    ) -> list | None:
        """三级缓存: 当前题库快照 -> SQLite 当前 exam_id -> 跨考试课程检索."""
        is_fill = question.get("questionType") == 3

        def normalize(record):
            if not record or not record.get("answer"):
                return None
            raw_answer = str(record["answer"])
            if is_fill:
                sep = "#@#" if "#@#" in raw_answer else "/"
                values = [item.strip() for item in raw_answer.split(sep) if item.strip()]
                return values or None
            ids = [item for item in raw_answer.split("#@#") if item]
            if not ids:
                return None
            if valid_ids and not all(item in valid_ids for item in ids):
                return None
            return ids

        ids = normalize(self.answer_bank.get(question_id, version))
        if ids:
            return ids
        try:
            row = self.answer_bank.store.get_ai_question(
                self.course_id, self.exam_test_id, question_id, version
            )
        except Exception as exc:
            logger.debug(f"SQLite 题库查询失败: {exc}")
            row = None
        ids = normalize(row)
        if ids:
            return ids
        if not self._cross_exam_search or (not is_fill and not valid_ids):
            return None
        text = extract_plain_text(question.get("content", ""))
        if len(text) < 8:
            return None
        try:
            hit = self.answer_bank.search_in_course(
                text, exclude_exam_id=self.exam_test_id
            )
        except Exception as exc:
            logger.debug(f"跨考试题库检索失败: {exc}")
            return None
        ids = normalize(hit)
        if ids:
            logger.debug(
                f"题目 {question_id} 跨考试题库命中({hit.get('match')}): {ids}"
            )
        return ids

    @staticmethod
    def _build_readback_record(content: dict) -> dict | None:
        """构造回读入库记录；填空题提取清洗后的答案文本并以 '/' 拼接."""
        correct = [
            option
            for option in content.get("optionVos") or []
            if is_flagged(option.get("isCorrect"))
        ]
        if content.get("questionType") == 3:
            cleaned = [
                text
                for option in correct
                for text in [extract_plain_text(option.get("content", ""))]
                if text
            ]
            if not cleaned:
                user_answers = content.get("userAnswerVo")
                if isinstance(user_answers, dict):
                    user_answers = [user_answers]
                elif not isinstance(user_answers, list):
                    user_answers = []
                cleaned = [
                    text
                    for item in user_answers
                    if isinstance(item, dict) and is_flagged(item.get("isCorrect"))
                    for text in [extract_plain_text(item.get("answer", ""))]
                    if text
                ]
            if not cleaned:
                return None
            answer_str = "/".join(cleaned)
            answer_content_str = "\n".join(cleaned)
        else:
            if not correct:
                return None
            answer_str = "#@#".join(str(option["id"]) for option in correct)
            answer_content_str = "\n".join(
                option.get("content", "") for option in correct
            )
        return {
            "question": content.get("content", ""),
            "answer": answer_str,
            "answer_content": answer_content_str,
            "questionDict": content,
        }

    def get_question_answer(self, question: dict) -> tuple[list, str]:
        question_id = question.get("id") or question.get("questionId")
        if question_id is None:
            raise ValueError("题目缺少 id")
        question_type = question.get("questionType")
        version = question.get("version", 1)
        choices = [
            {"id": option["id"], "content": option.get("content", "")}
            for option in question.get("optionVos") or []
            if "id" in option
        ]
        valid_ids = {str(choice["id"]) for choice in choices}

        cached_ids = self._cached_answer(question, question_id, version, valid_ids)
        if cached_ids:
            self._sleep_or_cancel(random.uniform(1.5, 3.0))
            return cached_ids, "题库"

        if question_type == 3:
            try:
                return self._generate_fill_answer(question), "AI"
            except Exception as exc:
                logger.warn(f"题目 {question_id} 填空作答失败: {exc}")
                return [], "填空"
        if not choices:
            raise ValueError(f"题目 {question_id} 没有可选项")
        if len(choices) < 2:
            return [choices[0]["id"]], "唯一选项"

        try:
            return self._generate_answer(question_type, question, choices), "AI"
        except Exception as exc:
            logger.warn(f"题目 {question_id} AI 作答失败, 改为随机: {exc}")
        self._sleep_or_cancel(random.uniform(1.5, 3.0))
        if question_type == 2:
            return select_random_answers(choices, 2), "随机"
        return select_random_answers(choices, 1), "随机"

    # --------------------------------------------------------- template hooks
    def _open(self) -> None:
        self.open_exam()

    @abstractmethod
    def _fetch_sheet_items(self) -> list:
        """读取答题卡并展开为题目列表 (questionId/version)."""

    def _flush_pending(self) -> bool:
        """把暂存答案写入服务端; 逐题保存的子类无需实现."""
        return True

    @abstractmethod
    def _finish(self, submit: bool, sheets: list):
        """答题完成后的收尾, 返回 (success, correct, total)."""

    def _on_answered(
        self, item: dict, content: dict, answers: list, source: str, index: int, total: int
    ) -> bool:
        saved = self.save_answer(
            item["questionId"],
            answers,
            question_type=content.get("questionType", item.get("questionType")),
        )
        if not saved:
            self._failed_saves += 1
        self._log_answer(item, content, answers, source, index, total, saved)
        return saved

    def _log_answer(
        self,
        item: dict,
        content: dict,
        answers: list,
        source: str,
        index: int,
        total: int,
        saved: bool,
    ) -> None:
        source_label = (
            f"AI:{self._last_ai_provider}"
            if source == "AI" and self._last_ai_provider
            else source
        )
        q_brief = _brief_html(content.get("content", ""), 48)
        ans_text = _format_answer_choices(content, answers)
        logger.info(
            f"  [接口答题 {index}/{total}] 题{item['questionId']}({source_label}): "
            f"{q_brief} -> {ans_text}" + ("" if saved else " [保存失败]")
        )
        logger.event(
            "接口答题",
            序号=f"{index}/{total}",
            题目=item["questionId"],
            题干=_brief_html(content.get("content", ""), 80),
            方式=source_label,
            答案=ans_text,
            选项ID=",".join(str(a) for a in answers),
            保存成功=saved,
        )

    # ------------------------------------------------------------ template run
    def start(
        self, reference_materials: list[dict] | None = None, submit: bool = True
    ) -> tuple[bool, int, int]:
        """执行一次完整作答: 取卷 -> 答题 -> 收尾 (交卷/批量提交)."""
        self.reference_materials = reference_materials or []
        self._failed_saves = 0
        self._stopped.clear()
        try:
            if self._is_cancelled():
                logger.warn(f"检测到取消信号, 跳过{self.exam_label}作答.")
                return False, 0, 0
            self.answer_bank.load(self.exam_test_id)
            self._open()
            sheets = self._fetch_sheet_items()
            if not sheets:
                raise ValueError("试卷没有题目")

            total = len(sheets)
            logger.info(f"开始{self.exam_label}作答: 试卷 {self.exam_paper_id}, 共 {total} 题.")
            logger.event(f"{self.exam_label}开始", 试卷=self.exam_paper_id, 题目=total)
            self._answer_questions(sheets)
            flushed = self._flush_pending()
            if self._is_cancelled():
                logger.warn("任务已取消, 放弃自动交卷并保留已保存答案.")
                submit = False
            elif submit and (not flushed or getattr(self, "_failed_saves", 0)):
                logger.warn("存在未成功保存的答案, 为避免丢失作答放弃自动交卷.")
                logger.event(
                    "接口考试",
                    结果="放弃自动交卷",
                    失败题数=getattr(self, "_failed_saves", 0),
                )
                submit = False
            return self._finish(submit, sheets)
        finally:
            self._stop_heartbeat()
            self._log_bank_stats()
            self.close()

    def _log_bank_stats(self) -> None:
        bank = self.question_bank
        if bank is None:
            return
        try:
            bank.log_stats()
        except Exception as exc:
            logger.debug(f"外部题库统计输出失败: {exc}")

    def _answer_questions(self, sheets: list) -> None:
        total = len(sheets)
        for index, item in enumerate(sheets, 1):
            if self._is_cancelled():
                logger.warn(f"检测到取消信号, 已在第 {index}/{total} 题前提前退出接口答题.")
                break
            content = self.get_question_content(
                item["questionId"], item.get("version", 1)
            )
            if not content:
                continue
            content["version"] = item.get("version", 1)
            self._last_ai_provider = ""
            try:
                answers, source = self.get_question_answer(content)
            except Exception as exc:
                logger.error(f"题目 {item['questionId']} 未作答: {exc}")
                logger.event(
                    "接口答题",
                    序号=f"{index}/{total}",
                    题目=item["questionId"],
                    结果="未作答",
                    错误=logger.summarize_exception(exc),
                )
                continue
            self._on_answered(item, content, answers, source, index, total)


class AiHomeworkCtx(AiExamBase):
    """AI 知识点作业: 逐题保存, 交卷后核对成绩并写回题库."""

    _cross_exam_search = False
    question_bank_scope = "ai_homework"

    def _fetch_sheet_items(self) -> list:
        return self.get_sheet_content()

    def _finish(self, submit: bool, sheets: list) -> tuple[bool, int, int]:
        total = len(sheets)
        if submit:
            self.submit_exam()
            logger.info(f"接口考试试卷 {self.exam_paper_id} 已提交, 正在核对成绩并更新题库...")

        correct_questions = 0
        if submit:
            for item in sheets:
                version = item.get("version", 1)
                content = self.get_question_content(item["questionId"], version)
                if not content:
                    continue
                result = content.get("userAnswerVo")
                if isinstance(result, list):
                    result = result[0] if result else None
                if not isinstance(result, dict):
                    logger.warn(f"题目 {item['questionId']} 没有作答结果.")
                    continue
                correct = [
                    option
                    for option in content.get("optionVos") or []
                    if is_flagged(option.get("isCorrect"))
                ]
                correct_preview = "; ".join(
                    _brief_html(opt.get("content", ""), 36) or str(opt.get("id"))
                    for opt in correct
                )
                if is_flagged(result.get("isCorrect")):
                    correct_questions += 1
                    logger.debug(f"题目 {item['questionId']} 回答正确.")
                else:
                    logger.info(
                        f"题目 {item['questionId']} 答错, 题库已更新"
                        + (f" (正确答案: {correct_preview})." if correct_preview else ".")
                    )
                record = self._build_readback_record(content)
                if not record:
                    continue
                self.answer_bank.set(item["questionId"], version, record)
        else:
            logger.info("答案已保存, 未自动交卷, 请在浏览器中手动提交.")
        self.answer_bank.save(self.exam_test_id)
        success = correct_questions == total
        logger.info(
            f"接口考试完成: 试卷 {self.exam_paper_id}, 得分 {correct_questions}/{total}"
            + (" (全对)" if success else "")
        )
        logger.event(
            "接口考试结果",
            试卷=self.exam_paper_id,
            得分=f"{correct_questions}/{total}",
            全对=success,
        )
        return success, correct_questions, total


# 兼容旧引用: 所有原有 ExamCtx 调用方与测试无需改动.
ExamCtx = AiHomeworkCtx


class AiFormalExamCtx(AiExamBase):
    """AI 智慧课综合/单元测试: 任务列表 -> 拉卷 -> 批量保存 -> 按配置交卷 -> 回读."""

    _cross_exam_search = True
    question_bank_scope = "ai_exam"

    def __init__(
        self,
        api: ExamApi,
        course_id,
        knowledge_id="",
        exam_test_id=None,
        exam_paper_id=None,
        op_extra: dict | None = None,
        answer_bank: AnswerBank | None = None,
        *,
        class_id="",
        task_id="",
        student_id="",
        save_nums: int = 5,
        auto_submit: bool = False,
        question_bank=None,
        cancel_event: threading.Event | None = None,
    ):
        super().__init__(
            api,
            course_id,
            knowledge_id,
            exam_test_id,
            exam_paper_id,
            op_extra,
            answer_bank,
            question_bank,
            cancel_event=cancel_event,
        )
        self.class_id = class_id or ""
        self.task_id = task_id or ""
        self.student_id = student_id or ""
        self.save_nums = max(1, int(save_nums or 1))
        self.auto_submit = bool(auto_submit)
        self._pending: list[dict] = []
        self._answered: dict[str, dict] = {}
        self._failed_saves = 0

    @classmethod
    def from_task(
        cls,
        api: ExamApi,
        task: dict,
        *,
        save_nums: int = 5,
        auto_submit: bool = False,
        op_extra: dict | None = None,
        answer_bank: AnswerBank | None = None,
        question_bank=None,
        cancel_event: threading.Event | None = None,
    ) -> "AiFormalExamCtx":
        def pick(*keys):
            for key in keys:
                value = task.get(key)
                if value not in (None, ""):
                    return value
            return ""

        return cls(
            api,
            pick("courseId", "recruitId"),
            pick("knowledgeId"),
            exam_test_id=pick("examTestId"),
            exam_paper_id=pick("examPaperId", "examId"),
            op_extra=op_extra,
            answer_bank=answer_bank,
            class_id=pick("classId"),
            task_id=pick("taskId", "id"),
            student_id=pick("studentId", "userId"),
            save_nums=save_nums,
            auto_submit=auto_submit,
            question_bank=question_bank,
            cancel_event=cancel_event,
        )

    def start(
        self, reference_materials: list[dict] | None = None, submit: bool | None = None
    ) -> tuple[bool, int, int]:
        resolved = self.auto_submit if submit is None else bool(submit)
        return super().start(reference_materials, resolved)

    # ------------------------------------------------------------------ 任务
    def scan_pending_tasks(self) -> list[dict]:
        """扫描未完成的 AI 课程考试任务 (taskType=1, status=0)."""
        ret = self.api.query_strategy(
            "ai_task",
            FORMAL_TASK_LIST,
            {
                "courseId": self.course_id,
                "taskType": 1,
                "taskName": "",
                "status": 0,
            },
        )
        payload = ret.get("data") if isinstance(ret, dict) else None
        if isinstance(payload, list):
            tasks = payload
        elif isinstance(payload, dict):
            tasks = None
            for key in ("taskList", "list", "records", "tasks"):
                if isinstance(payload.get(key), list):
                    tasks = payload[key]
                    break
            tasks = tasks or []
        else:
            tasks = []
        return [
            task
            for task in tasks
            if isinstance(task, dict)
            and not isinstance(task.get("taskType"), bool)
            and str(task.get("taskType")) == "1"
        ]

    # ---------------------------------------------------------------- 开考
    def start_pull_paper(self) -> dict:
        data = {
            "courseId": self.course_id,
            "classId": self.class_id,
            "examTestId": self.exam_test_id,
            "pageNumber": 1,
            "pageSize": 10,
        }
        return self.api.query_strategy("ai_task", FORMAL_START_PULL, data)

    def open_exam(self, tried: int = 0) -> None:
        """综合考试 openExam 不传 examPaperId, 且必须先拉卷."""
        data = {"examTestId": self.exam_test_id, "courseId": self.course_id}
        max_attempts = 4
        for attempt in range(max(0, tried), max_attempts):
            try:
                self.api.query_strategy("ai_exam", EXAM_OPEN, data)
                break
            except Exception as exc:
                if attempt + 1 >= max_attempts or self._is_cancelled():
                    raise
                logger.warn(f"打开考试失败, 重试第 {attempt + 1} 次: {exc}")
                self._sleep_or_cancel(min(2.0, 0.5 * (2 ** (attempt - max(0, tried)))))
        self._ensure_heartbeat()

    def _open(self) -> None:
        self.start_pull_paper()
        self.open_exam()

    def get_sheet_content(self, tried: int = 0) -> list:
        """答题卡可能有多份 partSheetVos, 必须全部展开."""
        if self.sheet_content is not None:
            return self.sheet_content
        data = {"examTestId": self.exam_test_id, "examPaperId": self.exam_paper_id}
        max_attempts = 4
        for attempt in range(max(0, tried), max_attempts):
            try:
                ret = self.api.query_strategy(
                    "ai_exam", FORMAL_ANSWER_SHEET, data, method="GET"
                )
                parts = ret["data"]["partSheetVos"] or []
                items: list = []
                for part in parts:
                    items.extend((part or {}).get("questionSheetVos") or [])
                self.sheet_content = items
                return self.sheet_content
            except Exception as exc:
                if attempt + 1 >= max_attempts or self._is_cancelled():
                    raise
                logger.warn(f"读取答题卡失败, 重试第 {attempt + 1} 次: {exc}")
                self._sleep_or_cancel(min(2.0, 0.5 * (2 ** (attempt - max(0, tried)))))
        return self.sheet_content or []

    def _fetch_sheet_items(self) -> list:
        return self.get_sheet_content()

    # -------------------------------------------------------------- 批量保存
    def _on_answered(
        self, item: dict, content: dict, answers: list, source: str, index: int, total: int
    ) -> bool:
        question_type = content.get(
            "questionType", item.get("questionType", 1)
        )
        entry = {
            "recruitId": self.course_id,
            "examTestId": self.exam_test_id,
            "examPaperId": self.exam_paper_id,
            "questionId": item["questionId"],
            "answer": answer_separator(question_type).join(
                str(answer) for answer in answers
            ),
            "questionType": question_type,
            "dataVos": None,
        }
        self._pending.append(entry)
        self._answered[str(item["questionId"])] = entry
        self._log_answer(item, content, answers, source, index, total, True)
        if len(self._pending) >= self.save_nums:
            self._flush_pending()
        return True

    def _flush_pending(self) -> bool:
        failed: list[dict] = []
        while self._pending:
            batch = self._pending[: self.save_nums]
            del self._pending[: self.save_nums]
            if self.save_batch_answer(batch):
                continue
            logger.warn(
                f"批量保存 {len(batch)} 题答案失败, 已从待提交队列移除并保留告警."
            )
            failed.extend(batch)
            self._failed_saves += len(batch)
            for entry in batch:
                self._answered.pop(str(entry.get("questionId")), None)
        if failed:
            logger.event(
                "接口答题",
                结果="批量保存失败",
                失败题数=len(failed),
            )
            return False
        return True

    def save_batch_answer(self, answer_list: list[dict]) -> bool:
        if not answer_list:
            return False
        data = {
            "examTestId": self.exam_test_id,
            "examPaperId": self.exam_paper_id,
            "recruitId": self.course_id,
            "answerList": list(answer_list),
        }
        try:
            self.api.query_strategy("ai_exam", FORMAL_SAVE_BATCH, data)
        except Exception as exc:
            logger.error(f"批量保存答案失败: {exc}")
            return False
        logger.debug(f"批量保存 {len(answer_list)} 题答案.")
        return True

    # ------------------------------------------------------------------ 交卷
    def submit_exam(self, tried: int = 0) -> bool:
        """非幂等交卷接口：禁用 urllib3 POST 重试，绝不盲目递归重发。"""
        data = {
            "examTestId": self.exam_test_id,
            "recruitId": str(self.course_id),
            "examPaperId": self.exam_paper_id,
        }
        try:
            self.api.query_strategy("ai_exam_submit", EXAM_SUBMIT, data)
        finally:
            self._stopped.set()
        return True

    def open_exam_detail(self) -> dict:
        data = {
            "classId": self.class_id,
            "courseId": self.course_id,
            "examTestId": self.exam_test_id,
            "examPaperId": self.exam_paper_id,
            "examId": self.exam_paper_id,
            "studentId": self.student_id,
            "taskId": self.task_id,
            "taskType": None,
        }
        try:
            ret = self.api.query_strategy("ai_task", FORMAL_OPEN_DETAIL, data)
        except Exception as exc:
            logger.debug(f"openExamDetail 失败: {exc}")
            return {}
        payload = ret.get("data") if isinstance(ret, dict) else None
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _detail_allows_lookup(detail: dict) -> bool:
        return is_flagged(detail.get("isLookAnswer")) or is_flagged(
            detail.get("isAllowShowDetail")
        )

    def _readback_answers(self, sheets: list) -> int:
        written = 0
        for item in sheets:
            version = item.get("version", 1)
            content = self.get_question_content(item["questionId"], version)
            if not content:
                continue
            record = self._build_readback_record(content)
            if not record:
                continue
            self.answer_bank.set(item["questionId"], version, record)
            written += 1
        self.answer_bank.save(self.exam_test_id)
        return written

    def _finish(self, submit: bool, sheets: list) -> tuple[bool, int, int]:
        total = len(sheets)
        submitted = False
        if submit:
            self.submit_exam()
            submitted = True
            logger.info(f"综合考试试卷 {self.exam_paper_id} 已交卷, 正在检查答案回读权限...")
            detail = self.open_exam_detail()
            if detail and self._detail_allows_lookup(detail):
                written = self._readback_answers(sheets)
                logger.info(f"已回读并写回 {written} 题正确答案到题库.")
            else:
                logger.info("当前考试不允许查看答案, 跳过回读.")
        else:
            logger.info("答案已分批保存, 未自动交卷, 请在浏览器中手动提交.")
        answered = len(self._answered)
        logger.info(
            f"接口考试完成: 试卷 {self.exam_paper_id}, 已答 {answered}/{total}"
            + (" (已交卷)" if submitted else "")
        )
        logger.event(
            "接口考试结果",
            试卷=self.exam_paper_id,
            已答=f"{answered}/{total}",
            交卷=submitted,
        )
        return submitted, answered, total


# 延后导入避免循环依赖，同时保持对外全量导出
from modules.ai_exam_runner import ExamRunner, answer_review_exam_page  # noqa: E402, F401
