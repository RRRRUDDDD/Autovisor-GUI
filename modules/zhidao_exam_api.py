# encoding=utf-8
"""知到正式考试加密 API 封装.

- getStudentFinalExam / doExam / submit 走 ``zhidao_exam`` 策略 (taurusexam-api);
- getSaveAnswerLockResult / getLoginSchoolInfo / saveStudentAnswer 走 ``homework`` 策略
  (studentexam-api), saveStudentAnswer 额外携带明文 ``source=1``.
"""
from __future__ import annotations

import json

from modules.zhidao_homework_api import (
    HOMEWORK_BASE,
    SAVE_STUDENT_ANSWER,
    build_student_answer_item,
    extract_exam_questions,
)

EXAM_BASE = "https://taurusexam-api.zhihuishu.com/taurus/gateway/t/v1"

GET_STUDENT_FINAL_EXAM = f"{EXAM_BASE}/exam/getStudentFinalExam"
DO_EXAM = f"{EXAM_BASE}/exam/doExam"
SUBMIT_EXAM = f"{EXAM_BASE}/exam/submit"

GET_SAVE_ANSWER_LOCK_RESULT = f"{HOMEWORK_BASE}/exam/getSaveAnswerLockResult"
GET_LOGIN_SCHOOL_INFO = f"{HOMEWORK_BASE}/student/getLoginSchoolInfo"


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def normalize_final_exam(item: dict) -> dict:
    item = item or {}
    return {
        "examId": _text(item.get("examId") or item.get("id")),
        "studentExamId": _text(
            item.get("studentExamId") or item.get("stuExamId") or item.get("studentExamid")
        ),
        "recruitId": _text(item.get("recruitId") or item.get("recruitID")),
        "courseId": _text(item.get("courseId")),
        "schoolId": _text(item.get("schoolId")),
        "examName": _text(item.get("examName") or item.get("name")),
        "studentStartTime": _text(item.get("studentStartTime")),
        "progressType": item.get("progressType"),
        "state": item.get("state"),
        "raw": item,
    }


def extract_final_exams(payload) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    rt = payload.get("rt")
    if isinstance(rt, dict):
        rt = rt.get("examList") or rt.get("list") or rt.get("exams") or []
    if not isinstance(rt, list):
        return []
    exams = []
    for item in rt:
        if isinstance(item, dict):
            normalized = normalize_final_exam(item)
            if normalized["examId"] or normalized["studentExamId"]:
                exams.append(normalized)
    return exams


def is_exam_started(exam: dict) -> bool:
    """已开始的判定: studentStartTime 非空, 或 progressType/state 为 2."""
    if _text(exam.get("studentStartTime")):
        return True
    return str(exam.get("progressType")).strip() == "2" or str(
        exam.get("state")
    ).strip() == "2"


def lock_state(payload) -> int | None:
    if not isinstance(payload, dict):
        return None
    rt = payload.get("rt") if isinstance(payload.get("rt"), dict) else payload
    raw = rt.get("state", rt.get("lockState"))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def extract_school_id(payload) -> str:
    if not isinstance(payload, dict):
        return ""
    rt = payload.get("rt") if isinstance(payload.get("rt"), dict) else payload
    return _text(rt.get("schoolId") or rt.get("schoolID"))


def submit_succeeded(payload) -> bool:
    if not isinstance(payload, dict):
        return False
    rt = payload.get("rt") if isinstance(payload.get("rt"), dict) else payload
    return str(rt.get("statu", rt.get("status"))).strip() == "1"


class ZhidaoExamApi:
    """知到正式考试接口封装."""

    def __init__(self, client):
        self.client = client

    def _exam_call(self, url: str, data: dict) -> dict:
        return self.client.query("zhidao_exam", url, data)

    def _homework_call(self, url: str, data: dict, *, extra_fields=None) -> dict:
        self.client.ensure_exam_sso()
        return self.client.query(
            "homework", url, data, extra_fields=extra_fields
        )

    def get_student_final_exam(self, flag: int = 1) -> dict:
        return self._exam_call(GET_STUDENT_FINAL_EXAM, {"flag": int(flag)})

    def get_save_answer_lock_result(
        self, *, recruit_id, course_id, exam_id, student_exam_id
    ) -> dict:
        return self._homework_call(
            GET_SAVE_ANSWER_LOCK_RESULT,
            {
                "recruitId": str(recruit_id),
                "courseId": str(course_id),
                "examId": str(exam_id),
                "studentExamId": str(student_exam_id),
            },
        )

    def get_login_school_info(self) -> dict:
        return self._homework_call(GET_LOGIN_SCHOOL_INFO, {})

    def do_exam(
        self,
        *,
        recruit_id,
        exam_id,
        student_exam_id,
        school_id="",
        course_id="",
    ) -> dict:
        payload = {
            "recruitId": str(recruit_id),
            "examId": str(exam_id),
            "studentExamId": str(student_exam_id),
            "schoolId": str(school_id or ""),
            "courseId": str(course_id),
            "deviceId": "",
        }
        return self._exam_call(DO_EXAM, payload)

    def save_student_answer(
        self,
        *,
        recruit_id,
        course_id,
        exam_id,
        student_exam_id,
        eid,
        question_type,
        answer,
    ) -> dict:
        item = build_student_answer_item(
            eid=eid,
            question_type=question_type,
            answer=answer,
            exam_id=exam_id,
            exam_type=1,
        )
        return self._homework_call(
            SAVE_STUDENT_ANSWER,
            {
                "recruitId": str(recruit_id),
                "courseId": str(course_id),
                "examId": str(exam_id),
                "studentExamId": str(student_exam_id),
                "stuExamAnswer": json.dumps([item], ensure_ascii=False),
            },
            extra_fields={"source": "1"},
        )

    def submit(
        self,
        *,
        recruit_id,
        course_id,
        exam_id,
        student_exam_id,
        achieve_count: int = 0,
    ) -> dict:
        return self._exam_call(
            SUBMIT_EXAM,
            {
                "recruitId": str(recruit_id),
                "courseId": str(course_id),
                "examId": str(exam_id),
                "studentExamId": str(student_exam_id),
                "achieveCount": int(achieve_count),
            },
        )


__all__ = [
    "EXAM_BASE",
    "GET_STUDENT_FINAL_EXAM",
    "DO_EXAM",
    "SUBMIT_EXAM",
    "GET_SAVE_ANSWER_LOCK_RESULT",
    "GET_LOGIN_SCHOOL_INFO",
    "ZhidaoExamApi",
    "extract_exam_questions",
    "extract_final_exams",
    "is_exam_started",
    "lock_state",
    "extract_school_id",
    "submit_succeeded",
]