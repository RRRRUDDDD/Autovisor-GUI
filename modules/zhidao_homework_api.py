# encoding=utf-8
"""知到共享课作业加密 API 封装.

所有请求走 ``homework`` 策略 (studentexam-api), 调用前确保 CAS 考试会话可用.
接口字段依据 docs/api_contracts.md 与 docs/plans/zhs-inspired-improvements.md.
"""
from __future__ import annotations

import json

from modules.url_router import parse_exam_url

HOMEWORK_BASE = "https://studentexam-api.zhihuishu.com/studentExam/gateway/t/v1"

GET_STUDENT_HOMEWORK = f"{HOMEWORK_BASE}/student/getStudentHomework"
SAVE_COURSE_TRAN = f"{HOMEWORK_BASE}/student/saveCourseTran"
DO_HOMEWORK = f"{HOMEWORK_BASE}/student/doHomework"
SAVE_STUDENT_ANSWER = f"{HOMEWORK_BASE}/student/saveStudentAnswer"
SUBMIT_HOMEWORK = f"{HOMEWORK_BASE}/student/submit"
LOOK_HOMEWORK = f"{HOMEWORK_BASE}/student/lookHomework"
GET_STU_ANSWER_INFO = f"{HOMEWORK_BASE}/student/getStuAnswerInfo"

QUESTION_TYPE_NAMES = {1: "single", 2: "multiple", 3: "fill", 14: "judge"}


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
    """把 doHomework/doExam 的 questionDtos 归一化为答题所需结构."""
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
        "eid": str(dto.get("eid") or dto.get("questionId") or dto.get("id") or ""),
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
            dtos = [part] if part.get("eid") or part.get("questionId") else []
        for dto in dtos or []:
            if isinstance(dto, dict):
                normalized = normalize_question(dto)
                if normalized["eid"]:
                    questions.append(normalized)
    return questions


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


def parse_review(payload, answer_info=None) -> dict[str, dict]:
    """合并 lookHomework 与 getStuAnswerInfo, 返回 {eid: {correct, correct_options, user_answer}}."""
    review: dict[str, dict] = {}
    for question in extract_exam_questions(payload):
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
        review[question["eid"]] = {
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
        eid = str(entry.get("eid") or entry.get("questionId") or entry.get("id") or "")
        if not eid:
            continue
        item = review.setdefault(
            eid, {"correct": False, "correct_options": [], "user_answer": ""}
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
    exam_id: str = "",
    exam_type="",
) -> dict:
    """构造 saveStudentAnswer 的单题 answer_item (作业 examType="")."""
    return {
        "questionId": str(eid),
        "examId": str(exam_id),
        "questionType": int(question_type),
        "answer": str(answer),
        "examType": exam_type,
        "fromType": 3,
    }


class ZhidaoHomeworkApi:
    """知到作业接口封装; 所有调用自动确保 exam SSO 会话."""

    def __init__(self, client):
        self.client = client

    def _call(self, url: str, data: dict) -> dict:
        self.client.ensure_exam_sso()
        return self.client.query("homework", url, data)

    def get_student_homework(self, flag: int = 1) -> dict:
        return self._call(GET_STUDENT_HOMEWORK, {"flag": int(flag)})

    def save_course_tran(
        self,
        *,
        recruit_id,
        course_id,
        homework_id,
        stu_exam_id,
        state: int = 0,
    ) -> dict:
        return self._call(
            SAVE_COURSE_TRAN,
            {
                "recruitId": str(recruit_id),
                "courseId": str(course_id),
                "homeworkId": str(homework_id),
                "stuExamId": str(stu_exam_id),
                "state": int(state),
            },
        )

    def do_homework(
        self,
        *,
        recruit_id,
        course_id,
        homework_id,
        stu_exam_id,
        school_id="",
    ) -> dict:
        return self._call(
            DO_HOMEWORK,
            {
                "recruitId": str(recruit_id),
                "courseId": str(course_id),
                "homeworkId": str(homework_id),
                "stuExamId": str(stu_exam_id),
                "schoolId": str(school_id or ""),
                "deviceId": "",
            },
        )

    def save_student_answer(
        self,
        *,
        recruit_id,
        course_id,
        homework_id,
        stu_exam_id,
        answer_item: dict,
    ) -> dict:
        return self._call(
            SAVE_STUDENT_ANSWER,
            {
                "recruitId": str(recruit_id),
                "courseId": str(course_id),
                "homeworkId": str(homework_id),
                "stuExamId": str(stu_exam_id),
                "stuExamAnswer": json.dumps([answer_item], ensure_ascii=False),
            },
        )

    def submit(
        self,
        *,
        recruit_id,
        course_id,
        homework_id,
        stu_exam_id,
        achieve_count: int = 0,
    ) -> dict:
        return self._call(
            SUBMIT_HOMEWORK,
            {
                "recruitId": str(recruit_id),
                "courseId": str(course_id),
                "homeworkId": str(homework_id),
                "stuExamId": str(stu_exam_id),
                "achieveCount": int(achieve_count),
            },
        )

    def look_homework(
        self,
        *,
        recruit_id,
        course_id,
        homework_id,
        stu_exam_id,
    ) -> dict:
        return self._call(
            LOOK_HOMEWORK,
            {
                "recruitId": str(recruit_id),
                "courseId": str(course_id),
                "homeworkId": str(homework_id),
                "stuExamId": str(stu_exam_id),
            },
        )

    def get_stu_answer_info(
        self,
        *,
        recruit_id,
        course_id,
        homework_id,
        stu_exam_id,
    ) -> dict:
        return self._call(
            GET_STU_ANSWER_INFO,
            {
                "recruitId": str(recruit_id),
                "courseId": str(course_id),
                "homeworkId": str(homework_id),
                "stuExamId": str(stu_exam_id),
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