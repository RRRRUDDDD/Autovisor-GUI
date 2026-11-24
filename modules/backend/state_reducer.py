# encoding=utf-8
"""RunState 默认结构与结构化日志事件归约器（以事件常量分发表取代冗长 if/elif 链）。"""
from __future__ import annotations

import time
from typing import Callable

# 结构化事件常量
EVT_CURRENT_COURSE = "当前课程"
EVT_COURSE_CATALOG = "课程目录"
EVT_LESSON_START = "课时开始"
EVT_LESSON_DONE = "课时已完成"
EVT_RESOURCE_START = "资源开始"
EVT_AI_RESOURCE_DONE = "ai-smart 资源完成"
EVT_AI_RESOURCE_REVIEW_DONE = "ai-smart 资源复习完成"
EVT_AI_COURSE_DONE = "ai-smart 课程完成"
EVT_KP_STATUS = "知识点状态"
EVT_MASTERY_RESULT = "掌握度结果"
EVT_KP_DONE = "知识点完成"
EVT_EXAM_SCOPE = "接口考试范围"
EVT_KP_EXAM = "知识点考试"
EVT_EXAM_START = "接口考试开始"
EVT_EXAM_ANSWER = "接口答题"
EVT_KP_EXAM_RESULT = "知识点考试结果"
EVT_RESOURCE_FILL = "资源补齐"
EVT_ZHIDAO_HOMEWORK = "知到作业"
EVT_ZHIDAO_EXAM = "知到考试"
EVT_TIME_LIMIT = "达到课程时限"
EVT_MANUAL_ATTENTION = "人工介入"
EVT_SECURITY_VERIFY = "安全验证"
EVT_RESUME_PLAY = "恢复播放"
EVT_LOGIN_DONE = "登录完成"


def default_run_state() -> dict:
    return {
        "runId": 0,
        "revision": 0,
        "status": "idle",  # idle | running | stopping | completed | time_limit | error | cancelled
        "mode": "study",
        "reviewMode": False,
        "shutdownOnComplete": False,
        "shutdown": None,
        "startedAt": None,
        "finishedAt": None,
        "exitCode": None,
        "stage": "就绪",
        "courseIndex": 0,
        "courseTotal": 0,
        "currentCourse": "",
        "catalogType": "",
        "currentLesson": "",
        "lessonProgress": 0.0,
        "progressDesc": "",
        "remainingMinutes": None,
        "courseElapsedMin": 0.0,
        "completedLessons": [],
        "needsAttention": "",
        "errorMessage": "",
        "logFilePath": "",
        "logs": [],
        "logsTruncated": False,
    }


def format_hms_local() -> str:
    return time.strftime("%H:%M:%S", time.localtime())


def parse_ratio(value) -> tuple[int, int] | None:
    text = str(value or "").strip()
    if "/" not in text:
        return None
    left, right = text.split("/", 1)
    try:
        idx = int(left.strip())
        total = int(right.strip())
    except ValueError:
        return None
    if total <= 0:
        return None
    return idx, total


def _clamp_pct(value: float) -> float:
    return max(0.0, min(100.0, float(value)))


def _handle_current_course(state: dict, fields: dict) -> bool:
    if not fields.get("名称"):
        return False
    state["currentCourse"] = str(fields["名称"])
    return True


def _handle_course_catalog(state: dict, fields: dict) -> bool:
    if not fields.get("类型"):
        return False
    state["catalogType"] = str(fields["类型"])
    return True


def _handle_lesson_start(state: dict, fields: dict) -> bool:
    if not fields.get("标题"):
        return False
    state["currentLesson"] = str(fields["标题"])
    state["lessonProgress"] = 0.0
    state["needsAttention"] = ""
    return True


def _handle_lesson_done(state: dict, fields: dict) -> bool:
    if not fields.get("标题"):
        return False
    title = str(fields["标题"]).strip()
    if title:
        state["completedLessons"].append({"title": title, "time": format_hms_local()})
        state["lessonProgress"] = 100.0
        state["needsAttention"] = ""
    elapsed = fields.get("已学分钟")
    if isinstance(elapsed, (int, float)):
        state["courseElapsedMin"] = float(elapsed)
    return True


def _handle_resource_start(state: dict, fields: dict) -> bool:
    if not fields.get("标题"):
        return False
    title = str(fields["标题"]).strip()
    kind_label = str(fields.get("类型") or "").strip()
    stage = str(fields.get("阶段") or "").strip()
    state["currentLesson"] = f"{title} ({kind_label})" if kind_label else title
    state["lessonProgress"] = 0.0
    state["needsAttention"] = ""
    if stage == "复习":
        state["progressDesc"] = "AI 智慧课视频复习"
    elif stage == "补齐":
        state["progressDesc"] = "AI 智慧课资源补齐"
    else:
        state["progressDesc"] = "AI 智慧课必学资源"
    return True


def _handle_ai_resource_done(state: dict, fields: dict) -> bool:
    title = str(fields.get("标题") or "").strip()
    if not title:
        return False
    state["completedLessons"].append({"title": title, "time": format_hms_local()})
    state["lessonProgress"] = 100.0
    return True


def _handle_ai_resource_review_done(state: dict, fields: dict) -> bool:
    title = str(fields.get("标题") or "").strip()
    if not title:
        return False
    state["completedLessons"].append(
        {"title": f"{title}（复习）", "time": format_hms_local()}
    )
    state["lessonProgress"] = 100.0
    return True


def _handle_ai_course_done(state: dict, _fields: dict) -> bool:
    state["lessonProgress"] = 100.0
    state["needsAttention"] = ""
    return True


def _handle_kp_status(state: dict, fields: dict) -> bool:
    if not fields.get("名称"):
        return False
    state["currentLesson"] = str(fields["名称"])
    state["needsAttention"] = ""
    mastery = fields.get("掌握度")
    if isinstance(mastery, (int, float)):
        pct = _clamp_pct(mastery)
        state["lessonProgress"] = pct
        state["progressDesc"] = f"知识点掌握度 {pct:g}%"
    else:
        state["progressDesc"] = "知识点掌握度检测"
    return True


def _handle_mastery_result(state: dict, fields: dict) -> bool:
    mastery = fields.get("掌握度")
    if not isinstance(mastery, (int, float)):
        return False
    attempt = fields.get("轮次")
    pct = _clamp_pct(mastery)
    state["lessonProgress"] = pct
    suffix = f"（第 {attempt} 轮）" if attempt is not None else ""
    state["progressDesc"] = f"知识点掌握度 {pct:g}%{suffix}"
    return True


def _handle_kp_done(state: dict, fields: dict) -> bool:
    if not fields.get("名称"):
        return False
    after = fields.get("后")
    if not isinstance(after, (int, float)):
        return False
    kp_name = str(fields["名称"])
    pct = _clamp_pct(after)
    state["lessonProgress"] = pct
    state["completedLessons"].append(
        {"title": f"{kp_name}（掌握度 {pct:g}%）", "time": format_hms_local()}
    )
    return True


def _handle_exam_scope(state: dict, fields: dict) -> bool:
    kp_count = fields.get("知识点")
    state["progressDesc"] = (
        f"接口考试（共 {kp_count} 个知识点）" if kp_count is not None else "接口考试"
    )
    state["lessonProgress"] = 0.0
    state["needsAttention"] = ""
    return True


def _handle_kp_exam(state: dict, fields: dict) -> bool:
    kp_name = str(fields.get("知识点") or "").strip()
    if kp_name:
        state["currentLesson"] = kp_name
    seq_text = str(fields.get("序号") or "").strip()
    if seq_text:
        state["progressDesc"] = f"接口考试 {seq_text}"
        ratio = parse_ratio(seq_text)
        if ratio is not None:
            idx, total = ratio
            state["lessonProgress"] = _clamp_pct((idx - 1) / total * 100.0)
    state["needsAttention"] = ""
    return True


def _handle_exam_start(state: dict, fields: dict) -> bool:
    q_total = fields.get("题目")
    state["progressDesc"] = (
        f"接口考试作答（共 {q_total} 题）" if q_total is not None else "接口考试作答"
    )
    state["lessonProgress"] = 0.0
    state["needsAttention"] = ""
    return True


def _handle_exam_answer(state: dict, fields: dict) -> bool:
    seq_text = str(fields.get("序号") or "").strip()
    ratio = parse_ratio(seq_text)
    if ratio is not None:
        idx, total = ratio
        state["lessonProgress"] = _clamp_pct(idx / total * 100.0)
        state["progressDesc"] = f"接口考试作答 {seq_text}"
        return True
    if seq_text:
        state["progressDesc"] = f"接口考试作答 {seq_text}"
        return True
    return False


def _handle_kp_exam_result(state: dict, fields: dict) -> bool:
    kp_name = str(fields.get("知识点") or state.get("currentLesson") or "").strip()
    score = str(fields.get("得分") or "").strip()
    score_ratio = parse_ratio(score)
    all_correct = bool(fields.get("全对")) or (
        score_ratio is not None and score_ratio[0] >= score_ratio[1]
    )
    if all_correct:
        if kp_name:
            label = f"{kp_name}（接口考试 {score}）" if score else f"{kp_name}（接口考试）"
            state["completedLessons"].append({"title": label, "time": format_hms_local()})
        state["lessonProgress"] = 100.0
        state["progressDesc"] = f"接口考试完成 {score}" if score else "接口考试完成"
        return True
    if score:
        if score_ratio is not None:
            state["lessonProgress"] = _clamp_pct(score_ratio[0] / score_ratio[1] * 100.0)
        state["progressDesc"] = f"接口考试得分 {score}"
        return True
    return False


def _handle_resource_fill(state: dict, fields: dict) -> bool:
    done_count = fields.get("补齐")
    if not (isinstance(done_count, (int, float)) and done_count > 0):
        return False
    res_name = str(fields.get("名称") or "").strip()
    if res_name:
        state["currentLesson"] = res_name
    seq_text = str(fields.get("序号") or "").strip()
    state["progressDesc"] = f"必学资源补齐 {seq_text}" if seq_text else "必学资源补齐"
    ratio = parse_ratio(seq_text)
    if ratio is not None:
        state["lessonProgress"] = _clamp_pct(ratio[0] / ratio[1] * 100.0)
    return True


def _make_zhidao_assessment_handler(event_name: str) -> Callable[[dict, dict], bool]:
    def _handler(state: dict, fields: dict) -> bool:
        item_name = str(fields.get("名称") or "").strip()
        status_val = str(fields.get("状态") or "").strip()
        seq_text = str(fields.get("序号") or "").strip()
        if item_name:
            state["currentLesson"] = item_name
        if status_val == "开始":
            state["needsAttention"] = ""
            state["progressDesc"] = f"{event_name} {seq_text}" if seq_text else event_name
            ratio = parse_ratio(seq_text)
            if ratio is not None:
                idx, total = ratio
                state["lessonProgress"] = _clamp_pct((idx - 1) / total * 100.0)
            else:
                state["lessonProgress"] = 0.0
            return True
        if status_val == "完成":
            if item_name:
                score = fields.get("得分")
                submitted = fields.get("已交卷")
                if score is not None:
                    suffix = f"得分 {score}"
                elif submitted:
                    suffix = "已提交"
                else:
                    suffix = "已保存"
                state["completedLessons"].append(
                    {
                        "title": f"{item_name}（{event_name}·{suffix}）",
                        "time": format_hms_local(),
                    }
                )
            ratio = parse_ratio(seq_text)
            if ratio is not None:
                idx, total = ratio
                state["lessonProgress"] = _clamp_pct(idx / total * 100.0)
            else:
                state["lessonProgress"] = 100.0
            state["progressDesc"] = (
                f"{event_name}完成 {seq_text}" if seq_text else f"{event_name}完成"
            )
            return True
        return False

    return _handler


def _handle_time_limit(state: dict, _fields: dict) -> bool:
    if state["status"] == "running":
        state["status"] = "time_limit"
        return True
    return False


def _handle_manual_attention(state: dict, fields: dict) -> bool:
    status_val = str(fields.get("状态") or "").strip()
    if status_val == "需要":
        state["needsAttention"] = str(fields.get("原因") or "需要人工处理")
        return True
    if status_val == "已解除":
        state["needsAttention"] = ""
        return True
    return False


def _handle_security_verify(state: dict, fields: dict) -> bool:
    status_val = str(fields.get("状态") or "").strip()
    if status_val == "开始":
        state["needsAttention"] = "检测到安全验证,请手动完成验证..."
        return True
    if status_val in ("自动完成", "完成"):
        state["needsAttention"] = ""
        return True
    return False


def _handle_clear_attention(state: dict, _fields: dict) -> bool:
    if state["needsAttention"]:
        state["needsAttention"] = ""
        return True
    return False


EVENT_HANDLERS: dict[str, Callable[[dict, dict], bool]] = {
    EVT_CURRENT_COURSE: _handle_current_course,
    EVT_COURSE_CATALOG: _handle_course_catalog,
    EVT_LESSON_START: _handle_lesson_start,
    EVT_LESSON_DONE: _handle_lesson_done,
    EVT_RESOURCE_START: _handle_resource_start,
    EVT_AI_RESOURCE_DONE: _handle_ai_resource_done,
    EVT_AI_RESOURCE_REVIEW_DONE: _handle_ai_resource_review_done,
    EVT_AI_COURSE_DONE: _handle_ai_course_done,
    EVT_KP_STATUS: _handle_kp_status,
    EVT_MASTERY_RESULT: _handle_mastery_result,
    EVT_KP_DONE: _handle_kp_done,
    EVT_EXAM_SCOPE: _handle_exam_scope,
    EVT_KP_EXAM: _handle_kp_exam,
    EVT_EXAM_START: _handle_exam_start,
    EVT_EXAM_ANSWER: _handle_exam_answer,
    EVT_KP_EXAM_RESULT: _handle_kp_exam_result,
    EVT_RESOURCE_FILL: _handle_resource_fill,
    EVT_ZHIDAO_HOMEWORK: _make_zhidao_assessment_handler(EVT_ZHIDAO_HOMEWORK),
    EVT_ZHIDAO_EXAM: _make_zhidao_assessment_handler(EVT_ZHIDAO_EXAM),
    EVT_TIME_LIMIT: _handle_time_limit,
    EVT_MANUAL_ATTENTION: _handle_manual_attention,
    EVT_SECURITY_VERIFY: _handle_security_verify,
    EVT_RESUME_PLAY: _handle_clear_attention,
    EVT_LOGIN_DONE: _handle_clear_attention,
}


def apply_context_fields(state: dict, fields: dict) -> bool:
    changed = False
    course_idx = fields.get("课程索引")
    course_total = fields.get("课程总数")
    if isinstance(course_idx, int) and isinstance(course_total, int):
        state["courseIndex"] = course_idx
        state["courseTotal"] = course_total
        state["lessonProgress"] = 0.0
        state["remainingMinutes"] = None
        state["courseElapsedMin"] = 0.0
        changed = True
    elif fields.get("课程序号") and "/" in str(fields["课程序号"]):
        ratio = parse_ratio(fields["课程序号"])
        if ratio is not None:
            state["courseIndex"], state["courseTotal"] = ratio
            state["lessonProgress"] = 0.0
            state["remainingMinutes"] = None
            state["courseElapsedMin"] = 0.0
            changed = True
    if fields.get("目录类型"):
        state["catalogType"] = str(fields["目录类型"])
        changed = True
    if fields.get("课程名称"):
        state["currentCourse"] = str(fields["课程名称"])
        changed = True
    if fields.get("课时名称"):
        state["currentLesson"] = str(fields["课时名称"])
        state["lessonProgress"] = 0.0
        state["needsAttention"] = ""
        changed = True
    if fields.get("子项目名称"):
        state["currentLesson"] = str(fields["子项目名称"])
        state["lessonProgress"] = 0.0
        state["needsAttention"] = ""
        if fields.get("子项目序号"):
            mode_prefix = (
                "AI 智慧课复习子项目"
                if fields.get("模式") == "复习"
                else "AI 智慧课子项目"
            )
            state["progressDesc"] = f"{mode_prefix} {fields['子项目序号']}"
        changed = True
    return changed


def apply_named_event(state: dict, name: str, fields: dict) -> bool:
    handler = EVENT_HANDLERS.get(name)
    if handler is None:
        return False
    return bool(handler(state, fields))
