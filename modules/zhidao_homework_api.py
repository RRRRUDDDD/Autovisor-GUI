# encoding=utf-8
"""知到共享课作业加密 API 封装.

所有请求走 ``homework`` 策略 (studentexam-api), 调用前确保 CAS 考试会话可用.
接口字段依据 docs/api_contracts.md 与 zhs/api/operations/zhidao-homework/*.yaml.
"""
from __future__ import annotations

import json

from modules.url_router import parse_exam_url

HOMEWORK_BASE = "https://studentexam-api.zhihuishu.com/studentExam/gateway/t/v1"

GET_STUDENT_HOMEWORK = f"{HOMEWORK_BASE}/student/getStudentHomework"
SAVE_COURSE_TRAN = f"{HOMEWORK_BASE}/student/saveCourseTran"
DO_HOMEWORK = f"{HOMEWORK_BASE}/student/doHomework"
SAVE_STUDENT_ANSWER = f"{HOMEWORK_BASE}/answer/saveStudentAnswer"
SUBMIT_HOMEWORK = f"{HOMEWORK_BASE}/answer/submit"
LOOK_HOMEWORK = f"{HOMEWORK_BASE}/student/lookHomework"
GET_STU_ANSWER_INFO = f"{HOMEWORK_BASE}/answer/getStuAnswerInfo"

QUESTION_TYPE_NAMES = {1: "single", 2: "multiple", 3: "fill", 14: "judge"}


def _maybe_int(value, default: int = 0) -> int:
    """契约里部分 ID 要求整数; 传字符串时尽量转换, 失败回退默认值."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_exam_page_url(url: str | None) -> dict:
    """解析作业/考试页 URL 参数, 与 ``modules.url_router`` 共用同一套规则.

    hash 路径形如 ``/webExamList/dohomework/{recruitId}/{stuExamId}/{examId}/{courseId}/{schoolId}``,
    末尾可能还有额外段, 忽略即可.
    """
    return parse_exam_url(url)


def question_type_id(value) -> int:
    if isinstance(value, dict):
        value = value.get("id")
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 1
    return parsed if parsed in QUESTION_TYPE_NAMES else 1


def normalize_question(dto: dict) -> dict:
    """把 doHomework/doExam 的 questionDtos 归一化为答题所需结构.

    ``eid`` 只取加密字段; ``id``/``questionId`` 是 lookHomework 的题目数字 ID,
    两者不能混用 (保存答案用 eid, 查错与 AI 解析用数字 question_id).
    """
    dto = dto or {}
    options = []
    for option in dto.get("questionOptions") or dto.get("optionVos") or []:
        if not isinstance(option, dict) or option.get("id") is None:
            continue
        options.append(
            {
                "id": str(option.get("id")),
                "content": str(option.get("content") or ""),
            }
        )
    qtype = question_type_id(dto.get("questionType"))
    return {
        "eid": str(dto.get("eid") or ""),
        "question_id": str(dto.get("id") or dto.get("questionId") or ""),
        "type": qtype,
        "type_name": QUESTION_TYPE_NAMES.get(qtype, "single"),
        "content": str(dto.get("name") or dto.get("content") or ""),
        "options": options,
        "raw": dto,
    }


def extract_exam_questions(payload) -> list[dict]:
    """从响应中提取并归一化题目; 兼容位置差异较大的历史返回结构."""
    if not isinstance(payload, dict):
        return []
    rt = payload.get("rt")
    if isinstance(rt, list):
        candidates = rt
    elif isinstance(rt, dict):
        exam_base = rt.get("examBase") if isinstance(rt.get("examBase"), dict) else rt
        candidates = exam_base.get("workExamParts")
        if candidates is None:
            candidates = rt.get("questionDtos") or rt.get("questions")
    else:
        candidates = payload.get("questionDtos") or payload.get("questions")
    if isinstance(candidates, dict):
        candidates = [candidates]
    questions: list[dict] = []
    for part in candidates or []:
        if not isinstance(part, dict):
            continue
        dtos = part.get("questionDtos")
        if dtos is None:
            dtos = [part] if part.get("eid") or part.get("questionId") or part.get("id") else []
        for dto in dtos or []:
            if isinstance(dto, dict):
                normalized = normalize_question(dto)
                if normalized["eid"] or normalized["question_id"]:
                    questions.append(normalized)
    return questions


def extract_homework_list(payload) -> dict:
    """解析 getStudentHomework 分页返回: ``{"items": [...], "next": int|None}``.

    列表项 ``id`` 为学生作业 ID (studentExamId), ``examId`` 为作业 ID;
    ``next == -1`` 表示没有下一页.
    """
    result = {"items": [], "next": None}
    if not isinstance(payload, dict):
        return result
    rt = payload.get("rt") if isinstance(payload.get("rt"), dict) else {}
    raw_items = rt.get("studentHomeworkList")
    if isinstance(raw_items, list):
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            result["items"].append(
                {
                    "studentExamId": str(item.get("id") or ""),
                    "examId": str(item.get("examId") or ""),
                    "recruitId": str(item.get("recruitId") or ""),
                    "courseId": str(item.get("courseId") or ""),
                    "examName": str(item.get("examName") or ""),
                    "state": item.get("state"),
                    "isMarking": item.get("isMarking"),
                    "raw": item,
                }
            )
    raw_next = rt.get("next")
    if raw_next is not None:
        try:
            next_page = int(raw_next)
        except (TypeError, ValueError):
            next_page = None
        if next_page is not None and next_page >= 0:
            result["next"] = next_page
    return result


def _flag(value) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def correct_option_ids(dto: dict) -> list[str]:
    """从回读题目中取出服务端标记为正确的选项 ID."""
    ids = []
    for key in ("correctOptions", "correctOptionsIds", "answerIds"):
        raw = dto.get(key)
        if isinstance(raw, list) and raw:
            for item in raw:
                if isinstance(item, dict):
                    item = item.get("id")
                if item is not None:
                    ids.append(str(item))
    for option in dto.get("questionOptions") or dto.get("optionVos") or []:
        if not isinstance(option, dict) or not _flag(
            option.get("isCorrect", option.get("correct"))
        ):
            continue
        if option.get("id") is not None:
            ids.append(str(option.get("id")))
    seen = set()
    ordered = []
    for item in ids:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def _question_key(question: dict) -> str:
    return question.get("eid") or question.get("question_id") or ""


def parse_review(payload, answer_info=None) -> dict[str, dict]:
    """合并 lookHomework 与 getStuAnswerInfo.

    返回 ``{key: {correct, correct_options, user_answer}}``; key 优先用加密 ``eid``,
    lookHomework 这类只有数字 ID 的响应则用 ``question_id``.
    """
    review: dict[str, dict] = {}
    for question in extract_exam_questions(payload):
        key = _question_key(question)
        if not key:
            continue
        dto = question["raw"]
        user = dto.get("userAnswerVo")
        if not isinstance(user, dict):
            user = {}
        correct_flag = dto.get(
            "isCorrect", dto.get("isRight", user.get("isCorrect", user.get("isRight")))
        )
        user_answer = user.get("answer") or dto.get("userAnswer") or dto.get("stuAnswer")
        if isinstance(user_answer, list):
            user_answer = ",".join(str(item) for item in user_answer)
        review[key] = {
            "correct": _flag(correct_flag),
            "correct_options": correct_option_ids(dto),
            "user_answer": str(user_answer or ""),
        }

    entries = answer_info
    if isinstance(entries, dict):
        entries = entries.get("rt")
        if isinstance(entries, dict):
            entries = (
                entries.get("questionDtos")
                or entries.get("stuAnswerInfos")
                or entries.get("questions")
            )
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("eid") or entry.get("questionId") or entry.get("id") or "")
        if not key:
            continue
        item = review.setdefault(
            key, {"correct": False, "correct_options": [], "user_answer": ""}
        )
        item["correct"] = _flag(
            entry.get("isRight", entry.get("isCorrect", entry.get("correct", item["correct"])))
        )
        if entry.get("correctOptions") and not item["correct_options"]:
            item["correct_options"] = correct_option_ids(entry)
        user_answer = entry.get("userAnswer") or entry.get("answer")
        if user_answer and not item["user_answer"]:
            item["user_answer"] = str(user_answer)
    return review


def format_student_answer(question_type: int, values: list) -> str:
    """作业提交答案: 多选题逗号拼接选项 ID, 填空题用 ``/`` 拼接."""
    cleaned = [str(item) for item in values if str(item).strip()]
    if question_type == 3:
        return "/".join(cleaned)
    return ",".join(cleaned)


def build_student_answer_item(
    *,
    eid: str,
    question_type: int,
    answer: str,
    recruit_id="",
    exam_id="",
    stu_exam_id="",
    school_id="",
    exam_type="",
) -> dict:
    """构造 saveStudentAnswer 的单题 answer_item (作业 examType="", 考试 1)."""
    return {
        "examId": str(exam_id),
        "recruitId": str(recruit_id),
        "stuExamId": str(stu_exam_id),
        "eid": str(eid),
        "schoolId": str(school_id or ""),
        "deviceId": "",
        "examType": exam_type,
        "fromType": 3,
        "answer": str(answer),
        "dataIds": "",
        "questionType": int(question_type),
    }


class ZhidaoHomeworkApi:
    """知到作业接口封装; 所有调用自动确保 exam SSO 会话."""

    def __init__(self, client):
        self.client = client

    def _call(self, url: str, data: dict) -> dict:
        self.client.ensure_exam_sso()
        return self.client.query("homework", url, data)

    def get_student_homework(
        self,
        *,
        recruit_id,
        course_id="",
        flag: int = 1,
        page_size: int = 100,
        page_num: int = 0,
    ) -> dict:
        return self._call(
            GET_STUDENT_HOMEWORK,
            {
                "recruitId": str(recruit_id),
                "courseId": _maybe_int(course_id),
                "flag": int(flag),
                "pageSize": int(page_size),
                "pageNum": int(page_num),
            },
        )

    def save_course_tran(self, *, recruit_id, exam_id, course_id="") -> dict:
        return self._call(
            SAVE_COURSE_TRAN,
            {
                "recruitId": str(recruit_id),
                "examId": str(exam_id),
                "description": "",
                "courseId": _maybe_int(course_id),
            },
        )

    def do_homework(
        self,
        *,
        recruit_id,
        exam_id="",
        student_exam_id="",
        school_id="",
        course_id="",
    ) -> dict:
        return self._call(
            DO_HOMEWORK,
            {
                "recruitId": str(recruit_id),
                "examId": str(exam_id),
                "studentExamId": str(student_exam_id),
                "schoolId": str(school_id or ""),
                "courseId": str(course_id),
            },
        )

    def save_student_answer(
        self,
        *,
        recruit_id,
        exam_id="",
        stu_exam_id="",
        school_id="",
        eid="",
        question_type=1,
        answer="",
        exam_type="",
        answer_item: dict | None = None,
    ) -> dict:
        item = answer_item or build_student_answer_item(
            eid=eid,
            question_type=question_type,
            answer=answer,
            recruit_id=recruit_id,
            exam_id=exam_id,
            stu_exam_id=stu_exam_id,
            school_id=school_id,
            exam_type=exam_type,
        )
        return self._call(
            SAVE_STUDENT_ANSWER,
            {
                "stuExamAnswer": json.dumps([item], ensure_ascii=False),
                "recruitId": str(recruit_id),
            },
        )

    def submit(
        self,
        *,
        recruit_id,
        exam_id="",
        stu_exam_id="",
        achieve_count=0,
    ) -> dict:
        return self._call(
            SUBMIT_HOMEWORK,
            {
                "recruitId": str(recruit_id),
                "examId": str(exam_id),
                "stuExamId": str(stu_exam_id),
                "achieveCount": str(achieve_count),
            },
        )

    def look_homework(
        self,
        *,
        recruit_id,
        student_exam_id="",
        exam_id="",
        school_id="",
        course_id="",
    ) -> dict:
        return self._call(
            LOOK_HOMEWORK,
            {
                "recruitId": str(recruit_id),
                "studentExamId": str(student_exam_id),
                "examId": str(exam_id),
                "schoolId": str(school_id or ""),
                "courseId": str(course_id),
            },
        )

    def get_stu_answer_info(
        self,
        *,
        recruit_id,
        stu_exam_id="",
        exam_id="",
        school_id="",
        course_id="",
        question_ids=(),
    ) -> dict:
        if isinstance(question_ids, str):
            joined = question_ids
        else:
            joined = ",".join(str(item) for item in question_ids or [])
        return self._call(
            GET_STU_ANSWER_INFO,
            {
                "recruitId": str(recruit_id),
                "stuExamId": str(stu_exam_id),
                "examId": str(exam_id),
                "schoolId": str(school_id or ""),
                "courseId": str(course_id),
                "questionIds": joined,
            },
        )


def extract_score(payload) -> float | None:
    if not isinstance(payload, dict):
        return None
    rt = payload.get("rt")
    candidates = [payload]
    if isinstance(rt, dict):
        candidates.insert(0, rt)
    for source in candidates:
        for key in ("score", "totalScore", "examScore"):
            if source.get(key) is None:
                continue
            try:
                return float(source[key])
            except (TypeError, ValueError):
                continue
    return None