# encoding=utf-8
"""统一解析智慧树各类任务链接, 供课程队列与"手动补充链接"分流.

支持: 共享/融合课 recruitAndCourseId、知到作业/正式考试 dohomework|doexam、
AI 知识点学习 learnPage、知识图谱 knowledgeStudy、测试详情 testDetail。
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urlparse

KIND_COURSE = "course"
KIND_HOMEWORK = "dohomework"
KIND_EXAM = "doexam"
KIND_LEARN_PAGE = "learnPage"
KIND_KNOWLEDGE_STUDY = "knowledgeStudy"
KIND_TEST_DETAIL = "testDetail"

_EXAM_RE = re.compile(r"/(dohomework|doexam)/([^?#]+)", re.IGNORECASE)
_LEARN_PAGE_RE = re.compile(r"/learnPage/(\d+)/(\d+)/(\d+)", re.IGNORECASE)
_KNOWLEDGE_STUDY_RE = re.compile(r"/knowledgeStudy/(\d+)/(\d+)", re.IGNORECASE)
_TEST_DETAIL_RE = re.compile(r"/testDetail/(\d+)/(\d+)/(\d+)/(\d+)", re.IGNORECASE)


def _query(url: str) -> dict:
    try:
        parsed = urlparse(url)
    except ValueError:
        return {}
    return {key: values[0] for key, values in parse_qs(parsed.query).items() if values}


def parse_exam_url(url) -> dict:
    """解析 dohomework / doexam 答题链接 (hash 路由同样适用)."""
    raw = unquote(str(url or ""))
    match = _EXAM_RE.search(raw)
    if not match:
        return {}
    parts = [part for part in match.group(2).split("/") if part]
    if len(parts) < 5:
        return {}
    return {
        "kind": match.group(1).lower(),
        "recruitId": parts[0],
        "stuExamId": parts[1],
        "examId": parts[2],
        "courseId": parts[3],
        "schoolId": parts[4],
    }


def parse_learn_page(url) -> dict:
    raw = unquote(str(url or ""))
    match = _LEARN_PAGE_RE.search(raw)
    if match:
        return {
            "kind": KIND_LEARN_PAGE,
            "courseId": match.group(1),
            "nodeUid": match.group(2),
            "classId": match.group(3),
        }
    if "/learnpage" not in raw.lower():
        return {}
    query = _query(raw)
    course_id = query.get("courseId") or ""
    class_id = query.get("classId") or ""
    if not course_id or not class_id:
        return {}
    return {
        "kind": KIND_LEARN_PAGE,
        "courseId": course_id,
        "nodeUid": query.get("nodeUid") or "",
        "classId": class_id,
    }


def parse_knowledge_study(url) -> dict:
    raw = unquote(str(url or ""))
    match = _KNOWLEDGE_STUDY_RE.search(raw)
    if match:
        return {
            "kind": KIND_KNOWLEDGE_STUDY,
            "courseId": match.group(1),
            "classId": match.group(2),
        }
    if "knowledgestudy" not in raw.lower():
        return {}
    query = _query(raw)
    course_id = query.get("courseId") or ""
    class_id = query.get("classId") or ""
    if not course_id or not class_id:
        return {}
    return {
        "kind": KIND_KNOWLEDGE_STUDY,
        "courseId": course_id,
        "classId": class_id,
    }


def parse_test_detail(url) -> dict:
    raw = unquote(str(url or ""))
    match = _TEST_DETAIL_RE.search(raw)
    if not match:
        return {}
    return {
        "kind": KIND_TEST_DETAIL,
        "courseId": match.group(1),
        "classId": match.group(2),
        "examTestId": match.group(3),
        "examPaperId": match.group(4),
    }


def parse_course_url(url) -> dict:
    raw = unquote(str(url or ""))
    recruit = _query(raw).get("recruitAndCourseId") or ""
    if not recruit:
        return {}
    return {"kind": KIND_COURSE, "recruitAndCourseId": recruit}


def parse_url(url) -> dict:
    """按最具体的任务类型优先解析; 无法识别返回空字典."""
    for parser in (
        parse_exam_url,
        parse_test_detail,
        parse_learn_page,
        parse_knowledge_study,
        parse_course_url,
    ):
        parsed = parser(url)
        if parsed:
            return parsed
    return {}


def task_kind(url) -> str:
    return parse_url(url).get("kind", "")


def is_task_url(url) -> bool:
    return bool(parse_url(url))