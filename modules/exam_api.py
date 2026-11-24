# encoding=utf-8
"""智慧树 AI 课程考试接口客户端、题库访问封装与通用提示词辅助。"""
from __future__ import annotations

import json
import random
import re
from pathlib import Path
from urllib.parse import unquote

from modules import zhidao_crypto, zhs_api
from modules.answer_parser import QUESTION_TYPE_NAMES
from modules.answer_store import AiQuestionStore
from modules.logger import Logger
from modules.text_utils import brief as _text_brief
from modules.utils import get_runtime_path
from modules.zhs_api import (  # noqa: F401  兼容旧的导入路径
    CaptchaRequiredError,
    SessionExpiredError,
    SessionStatus,
    build_session,
)

logger = Logger()

EXAM_BASE = "https://studentexamtest.zhihuishu.com/gateway/t/v1"
KNOWLEDGE_RESOURCE_LIST = (
    "https://kg-ai-run.zhihuishu.com/run/gateway/t/stu/resources/list-knowledge-resource"
)
EXAM_OPEN = f"{EXAM_BASE}/exam/user/openExam"
EXAM_USED_TIME = f"{EXAM_BASE}/exam/user/updateUserUsedTime"
EXAM_SHEET = f"{EXAM_BASE}/exam/user/getExamSheetInfo"
EXAM_QUESTION = f"{EXAM_BASE}/question/getExamQuestionInfo"
EXAM_SAVE_ANSWER = f"{EXAM_BASE}/answer/saveAnswer"
EXAM_SUBMIT = f"{EXAM_BASE}/exam/user/submit"
AI_EXAM_PAPER = "https://kg-ai-run.zhihuishu.com/run/gateway/t/stu/exam/questions-paper"
FORMAL_TASK_LIST = (
    "https://kg-run-student.zhihuishu.com/student/gateway/t/task/taskList"
)
FORMAL_START_PULL = (
    "https://kg-ai-run.zhihuishu.com/run/gateway/t/task/exam/start-pull-paper"
)
FORMAL_OPEN_DETAIL = (
    "https://kg-ai-run.zhihuishu.com/run/gateway/t/task/exam/openExamDetail"
)
FORMAL_ANSWER_SHEET = f"{EXAM_BASE}/exam/user/getAnswerSheetInformation"
FORMAL_SAVE_BATCH = f"{EXAM_BASE}/answer/saveBatchAnswer"

MAX_EXAM_ATTEMPTS = 6
DEFAULT_PROMPT_CHAR_LIMIT = 28000
MATERIAL_TRUNCATED_MARK = "\n…[参考资料已截断]"

# 兼容旧名称: 智慧树接口异常统一由 modules.zhs_api.ZhsApiError 表示.
ExamApiError = zhs_api.ZhsApiError


def is_flagged(value) -> bool:
    return str(value).strip().lower() in {"1", "true"}


def as_number(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _brief_html(text: str | None, limit: int = 60) -> str:
    """去除 HTML 标签并折叠空白，供答题日志展示题干与选项摘要。"""
    return _text_brief(text, limit=limit, strip_html=True)


def _format_answer_choices(content: dict, answer_ids: list) -> str:
    options = content.get("optionVos") or []
    by_id = {
        str(opt.get("id")): _brief_html(opt.get("content", ""), 36)
        for opt in options
        if "id" in opt
    }
    parts = []
    for aid in answer_ids:
        key = str(aid)
        label = by_id.get(key)
        parts.append(f"{label}({key})" if label else key)
    return "; ".join(parts) or str(answer_ids)


def parse_review_exam_url(url: str | None) -> dict:
    """从浏览器里的复习/模拟考试页提取接口所需参数."""
    match = re.search(
        r"/studentReviewTestOrExam/(\d+)/\d+/\d+/(\d+)/([^/?#]+)", url or ""
    )
    if not match:
        return {}
    params = dict(re.findall(r"[?&]([^=&]+)=([^&]*)", url or ""))
    paper_id = params.get("paperId") or params.get("examPaperId") or ""
    if not paper_id:
        return {}
    return {
        "examTestId": match.group(1),
        "courseId": match.group(2),
        "nodeName": unquote(match.group(3)),
        "paperId": paper_id,
    }


def select_random_answers(choices: list[dict], count: int) -> list:
    count = min(max(count, 1), len(choices))
    picked = random.sample(choices, count)
    return [choice["id"] for choice in picked]


def _trim_reference_materials(materials: list[dict], budget: int) -> str:
    """按字符预算裁剪参考资料, 超出预算的末尾内容直接截断, 保留可读提示."""
    if budget <= 0:
        return ""
    header = "参考资料：\n"
    kept: list[str] = []
    remaining = budget - len(header)
    for item in materials:
        name = str(item.get("name", ""))
        content = str(item.get("content", ""))
        prefix = f"```{name}\n"
        suffix = "\n```"
        block = prefix + content + suffix
        if len(block) <= remaining:
            kept.append(block)
            remaining -= len(block) + 1
            continue
        inner_budget = remaining - len(prefix) - len(suffix) - len(MATERIAL_TRUNCATED_MARK)
        if inner_budget > 0:
            kept.append(prefix + content[:inner_budget] + MATERIAL_TRUNCATED_MARK + suffix)
        break
    if not kept:
        return ""
    return header + "\n".join(kept) + "\n"


def build_answer_prompt(
    question_type: int,
    question: str,
    choices: list[dict],
    reference_materials: list[dict] | None = None,
    extra: dict | None = None,
    max_chars: int | None = None,
) -> str:
    extra = extra or {}
    type_name = QUESTION_TYPE_NAMES.get(question_type, "客观题")
    background = (
        f"假设你是一名学生，正在学习《{extra.get('courseName', '未知课程')}》，"
        "需要严格按照考试要求完成一道题目，否则无法及格。\n"
    )
    theme = f"当前主题：{extra.get('theme', '')}。\n" if extra.get("theme") else ""
    knowledge = (
        f"考察知识点：{extra.get('knowledgePoint', '')}。\n"
        if extra.get("knowledgePoint")
        else ""
    )
    bank = str(extra.get("题库参考") or "").strip()
    bank_section = (
        f"【第三方题库参考】以下内容来自外部题库, 仅供参考, 请自行判断正确性：\n{bank}\n"
        if bank
        else ""
    )
    requirement = f"""本题为{type_name}，请从选项中选择{'最合适的答案' if question_type == 1 else '所有正确的答案'}，
回答放到 markdown 代码块中，例如：
```answer
[{{"id": 652308395, "content": "<p>仅涉及心、肺、肝、肾及脑相关疾病</p>"}}]
```
答案必须是 JSON 列表，单选也要是列表，否则视为无效答案。代码块外可以简短解释依据。
"""
    body = (
        background
        + theme
        + knowledge
        + bank_section
        + requirement
        + f"现在，请听题：\n\n{question}\n\n"
        + f"选项如下：```choices\n{json.dumps(choices, ensure_ascii=False, indent=4)}\n```\n"
    )
    materials = ""
    if reference_materials:
        try:
            limit = int(max_chars) if max_chars else DEFAULT_PROMPT_CHAR_LIMIT
        except (TypeError, ValueError):
            limit = DEFAULT_PROMPT_CHAR_LIMIT
        materials = _trim_reference_materials(reference_materials, limit - len(body))
    return materials + body


class AnswerBank:
    """按课程保存题库：完全以 SQLite (AiQuestionStore) 为单一数据源。

    旧版目录中的 *.json 仅在首次初始化时一次性迁移至 SQLite，不再逐题重写 JSON。
    """

    def __init__(self, course_id, base_dir: str | Path | None = None, store=None):
        self.course_id = str(course_id)
        explicit_dir = base_dir is not None
        self.base_dir = Path(
            base_dir
            if base_dir is not None
            else get_runtime_path("data", "exam_answers", self.course_id)
        )
        self.exam_test_id = None
        self.items: dict[str, dict] = {}
        if store is not None:
            self.store = store
        elif explicit_dir:
            # 显式目录通常来自测试: 数据库与旧 JSON 一次性迁移均隔离在该目录内
            self.store = AiQuestionStore(
                self.base_dir / "questions_bank.db", data_root=self.base_dir
            )
            self.store.migrate_course_json_dir(self.course_id, self.base_dir)
        else:
            self.store = AiQuestionStore()

    @staticmethod
    def _key(question_id, version: int) -> str:
        return str(question_id) if version == 1 else f"{question_id}_{version}"

    @staticmethod
    def _row_to_record(row: dict) -> dict:
        return {
            "version": int(row.get("version") or 1),
            "question": row.get("question_raw") or row.get("question_plain") or "",
            "answer": row.get("answer") or "",
            "answer_content": row.get("answer_content") or "",
            "questionDict": row.get("question_dict") or {},
        }

    def load(self, exam_test_id) -> None:
        self.exam_test_id = exam_test_id
        target_exam = str(exam_test_id or "")
        rows = self.store.load_course_questions(self.course_id)
        merged: dict[str, dict] = {}
        # 先加载同课程历史记录，再用当前 exam_id 的记录覆盖同 key
        for row in rows:
            if str(row.get("exam_id") or "") == target_exam:
                continue
            key = self._key(row.get("question_id", ""), int(row.get("version") or 1))
            merged[key] = self._row_to_record(row)
        for row in rows:
            if str(row.get("exam_id") or "") != target_exam:
                continue
            key = self._key(row.get("question_id", ""), int(row.get("version") or 1))
            merged[key] = self._row_to_record(row)
        self.items = merged

    def get(self, question_id, version: int = 1) -> dict | None:
        key = self._key(question_id, version)
        cached = self.items.get(key)
        if cached is not None:
            return cached
        row = self.store.get_ai_question(
            self.course_id, self.exam_test_id or "", str(question_id), version
        )
        if row is None and self.exam_test_id:
            row = self.store.get_ai_question(
                self.course_id, "", str(question_id), version
            )
        if row is None:
            return None
        record = self._row_to_record(row)
        self.items[key] = record
        return record

    def set(self, question_id, version: int, answer: dict) -> None:
        record = {
            "version": version,
            "question": answer.get("question", ""),
            "answer": answer.get("answer", ""),
            "answer_content": answer.get("answer_content", ""),
            "questionDict": answer.get("questionDict", {}),
        }
        self.items[self._key(question_id, version)] = record
        self.store.upsert_ai_question(
            self.course_id,
            self.exam_test_id or "",
            str(question_id),
            version,
            question_plain=record["question"],
            question_raw=record["question"],
            answer=record["answer"],
            answer_content=record["answer_content"],
            question_dict=record["questionDict"],
        )

    def save(self, exam_test_id=None) -> None:
        """SQLite 在 set() 时已落盘；若延迟绑定 exam_test_id，则按新 exam_id 批量同步一次。"""
        if exam_test_id is not None and str(exam_test_id) != str(self.exam_test_id or ""):
            self.exam_test_id = exam_test_id
            batch = []
            for raw_key, record in self.items.items():
                version = int(record.get("version") or 1)
                suffix = f"_{version}"
                if version != 1 and raw_key.endswith(suffix):
                    qid = raw_key[: -len(suffix)]
                else:
                    qid = raw_key
                batch.append(
                    {
                        "course_id": self.course_id,
                        "exam_id": str(exam_test_id),
                        "question_id": qid,
                        "version": version,
                        "question_plain": record.get("question", ""),
                        "question_raw": record.get("question", ""),
                        "answer": record.get("answer", ""),
                        "answer_content": record.get("answer_content", ""),
                        "question_dict": record.get("questionDict", {}),
                    }
                )
            if batch:
                self.store.upsert_ai_questions_batch(batch)

    def search_in_course(self, question: str, exclude_exam_id=None) -> dict | None:
        """跨 exam_id 检索同课程答案 (精确 -> 宽松 -> 模糊)."""
        return self.store.search_in_course(
            self.course_id, question, exclude_exam_id=exclude_exam_id
        )


class ExamApi:
    """带 Cookie/代理/重试的智慧树考试接口客户端（组合 ZhsApiClient，消除 LSP 签名冲突）。"""

    def __init__(self, cookies, proxy: str = "", user_agent: str = "", *, client=None):
        self.client = (
            client
            if client is not None
            else zhs_api.ZhsApiClient(cookies, proxy, user_agent)
        )

    @property
    def session(self):
        return self.client.session

    @session.setter
    def session(self, value) -> None:
        self.client.session = value

    @property
    def lock(self):
        return self.client.lock

    @property
    def proxy(self) -> str:
        return self.client.proxy

    @property
    def timeout(self):
        return self.client.timeout

    @property
    def user_agent(self) -> str:
        return self.client.user_agent

    def execute(self, *args, **kwargs) -> dict:
        return self.client.execute(*args, **kwargs)

    def clone_session(self):
        return self.client.clone_session()

    def sync_cookies(self, cookies) -> None:
        self.client.sync_cookies(cookies)

    def reset_exam_sso(self) -> None:
        self.client.reset_exam_sso()

    def ensure_service_sso(self, service: str) -> bool:
        return self.client.ensure_service_sso(service)

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "ExamApi":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def query(
        self,
        url: str,
        data: dict,
        key: bytes = zhidao_crypto.EXAM_KEY,
        ok_code: int = 0,
        method: str = "POST",
        content_type: str = "form",
        set_timestamp: bool = True,
        extra_fields: dict | None = None,
        allow_retry: bool = True,
    ) -> dict:
        """兼容旧方法签名: 按调用参数临时构造策略后走统一 execute 流程."""
        strategy = zhs_api.QueryStrategy(
            key=key,
            set_timestamp=set_timestamp,
            content_type=content_type,
            ok_field="code",
            ok_value=ok_code,
            allow_retry=allow_retry,
        )
        return self.client.execute(
            strategy,
            url,
            data,
            method=method,
            extra_fields=extra_fields,
            allow_retry=allow_retry,
        )

    def query_strategy(
        self, strategy_name: str, url: str, data: dict, method: str = "POST", **kwargs
    ) -> dict:
        """按命名策略提交 (供知到作业/考试/题库等新接口使用)."""
        return self.client.query(strategy_name, url, data, method, **kwargs)

    def query_paper(self, course_id, class_id, knowledge_id) -> dict | None:
        data = {
            "scMapId": course_id,
            "courseId": class_id,
            "classId": class_id,
            "knowledgeId": knowledge_id,
        }
        try:
            ret = self.query(
                AI_EXAM_PAPER,
                data,
                key=zhidao_crypto.AI_KEY,
                ok_code=200,
                content_type="json",
            )
        except Exception as exc:
            logger.log_exception("查询考试试卷失败.", exc)
            return None
        return ret.get("data") or None

    def list_knowledge_resources(
        self, course_id, class_id, knowledge_id
    ) -> list[dict]:
        """读取知识点关联资源 (PPT 课件等); 失败时返回空列表."""
        data = {
            "courseId": course_id,
            "classId": class_id,
            "knowledgeId": knowledge_id,
        }
        try:
            ret = self.query_strategy("ai_task", KNOWLEDGE_RESOURCE_LIST, data)
        except Exception as exc:
            logger.log_exception("查询知识点课件资源失败.", exc)
            return []
        payload = ret.get("data") if isinstance(ret, dict) else None
        resources = (
            payload.get("resourceList") if isinstance(payload, dict) else None
        )
        return resources if isinstance(resources, list) else []
