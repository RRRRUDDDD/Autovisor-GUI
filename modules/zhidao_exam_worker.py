# encoding=utf-8
"""知到正式考试 API worker: 发现进行中考试 -> 锁检查 -> doExam -> 逐题保存 -> 按需交卷."""
from __future__ import annotations

import random

from modules.logger import Logger
from modules.zhidao_exam_api import (
    extract_final_exams,
    extract_school_id,
    is_exam_started,
    lock_state,
    submit_succeeded,
)
from modules.zhidao_homework_api import (
    check_time_window,
    extract_exam_questions,
    format_student_answer,
)
from modules.zhidao_homework_worker import (
    _CAPTCHA_HINTS,
    ZhidaoHomeworkWorker,
    question_has_image,
)
from modules.zhs_api import CaptchaRequiredError

logger = Logger()

__all__ = ["ZhidaoExamWorker"]


class ZhidaoExamWorker(ZhidaoHomeworkWorker):
    """正式考试没有查错/重做闭环, 复用作业的答案生成与题库逻辑."""

    question_bank_scope = "zhidao_exam"

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
        response = self._call_with_http_captcha(
            lambda: self.api.save_student_answer(
                recruit_id=recruit_id,
                course_id=course_id,
                exam_id=exam_id,
                student_exam_id=stu_exam_id,
                school_id=school_id,
                eid=question["eid"],
                question_type=question.get("type", 1),
                answer=answer,
            ),
            recruit_id=recruit_id,
            exam_id=exam_id,
        )
        message = str((response or {}).get("message") or (response or {}).get("msg") or "")
        if any(hint in message.lower() for hint in _CAPTCHA_HINTS):
            from modules.zhs_api import CaptchaRequiredError

            raise CaptchaRequiredError(f"考试保存答案触发人机验证: {message}")

    # ------------------------------------------------------------------ run
    def scan(self, flag: int = 1, *, recruit_id="", course_id="") -> list[dict]:
        return extract_final_exams(
            self.api.get_student_final_exam(
                course_id=str(course_id or self.course_id or ""),
                recruit_id=str(recruit_id or self._recruit_id or ""),
                flag=flag,
            )
        )

    def run_pending(
        self, *, recruit_id="", course_id="", school_id="", now=None
    ) -> list[dict]:
        """扫描未完成考试并逐场接管, 返回每场统计."""
        effective_recruit = str(recruit_id or self._recruit_id or "")
        if effective_recruit and not self._recruit_id:
            self._recruit_id = effective_recruit
        results = []
        for exam in self.scan(
            flag=1, recruit_id=effective_recruit, course_id=course_id
        ):
            if self._is_cancelled():
                logger.warn("知到正式考试收到取消信号, 停止剩余考试.")
                break
            exam_id = exam.get("examId", "")
            stu_exam_id = exam.get("studentExamId", "")
            exam_name = exam.get("examName", "")
            item_recruit = exam.get("recruitId") or effective_recruit
            item_course = exam.get("courseId") or course_id or self.course_id
            item_school = exam.get("schoolId") or school_id or self.school_id
            raw = exam.get("raw") if isinstance(exam.get("raw"), dict) else {}
            in_window, window_reason = check_time_window(
                None,
                exam.get("endDate") or raw.get("endDate"),
                now=now,
            )
            if not in_window:
                logger.info(f"考试「{exam_name or exam_id}」已截止, 跳过.")
                results.append(
                    {
                        "exam_id": exam_id,
                        "exam_name": exam_name,
                        "student_exam_id": stu_exam_id,
                        "recruit_id": item_recruit,
                        "course_id": item_course,
                        "school_id": str(item_school or ""),
                        "skipped": True,
                        "reason": window_reason,
                        "answered": 0,
                        "submitted": False,
                    }
                )
                continue
            if not is_exam_started(exam):
                logger.warn(
                    f"考试「{exam_name or exam_id}」尚未开始, "
                    "请手动进入考试后再运行."
                )
                results.append(
                    {
                        "exam_id": exam_id,
                        "exam_name": exam_name,
                        "student_exam_id": stu_exam_id,
                        "recruit_id": item_recruit,
                        "course_id": item_course,
                        "school_id": str(item_school or ""),
                        "skipped": True,
                        "reason": "not_started",
                        "answered": 0,
                        "submitted": False,
                    }
                )
                continue
            stats = self.run(
                recruit_id=item_recruit,
                course_id=item_course,
                exam_id=exam_id,
                student_exam_id=stu_exam_id,
                school_id=str(item_school or ""),
                exam_name=exam_name,
            )
            stats["exam_id"] = exam_id
            stats["exam_name"] = exam_name
            stats["student_exam_id"] = stu_exam_id
            stats["recruit_id"] = item_recruit
            stats["course_id"] = item_course
            stats["school_id"] = str(item_school or "")
            results.append(stats)
        return results

    def run(
        self,
        *,
        recruit_id,
        course_id,
        exam_id,
        student_exam_id,
        school_id="",
        exam_name="",
    ) -> dict:
        try:
            return self._run(
                recruit_id=recruit_id,
                course_id=course_id,
                exam_id=exam_id,
                student_exam_id=student_exam_id,
                school_id=school_id,
                exam_name=exam_name,
            )
        finally:
            self._log_bank_stats()

    def _run(
        self,
        *,
        recruit_id,
        course_id,
        exam_id,
        student_exam_id,
        school_id="",
        exam_name="",
    ) -> dict:
        stats = {
            "answered": 0,
            "submitted": False,
            "skipped": False,
            "needs_vision": False,
            "lock_state": None,
        }
        lock_payload = self.api.get_save_answer_lock_result(
            exam_id=exam_id,
            recruit_id=recruit_id,
        )
        state = lock_state(lock_payload)
        stats["lock_state"] = state
        if state == 1:
            stats["skipped"] = True
            logger.warn(
                f"考试「{exam_name or exam_id}」已被锁定(可能在另一端作答或已交卷), 跳过."
            )
            return stats

        if not school_id:
            try:
                school_id = extract_school_id(self.api.get_login_school_info())
            except Exception as exc:
                logger.debug(f"getLoginSchoolInfo 失败, 以空 schoolId 继续: {exc}")
                school_id = ""

        payload = self._call_with_http_captcha(
            lambda: self.api.do_exam(
                recruit_id=recruit_id,
                exam_id=exam_id,
                student_exam_id=student_exam_id,
                school_id=school_id,
                course_id=course_id,
            ),
            recruit_id=recruit_id,
            exam_id=exam_id,
        )
        questions = extract_exam_questions(payload)
        if not questions:
            logger.warn(f"考试「{exam_name or exam_id}」未获取到题目, 跳过.")
            return stats

        text_questions = [q for q in questions if not question_has_image(q)]
        if len(text_questions) < len(questions):
            stats["needs_vision"] = True
            stats["image_questions"] = len(questions) - len(text_questions)
            logger.warn(
                f"考试含 {stats['image_questions']} 道图片题, 先接口保存其余 "
                f"{len(text_questions)} 道纯文本题, 图片题回退视觉模型."
            )
            questions = text_questions
        if not questions:
            return stats

        exam_key = str(exam_id)
        for question in questions:
            if self._is_cancelled():
                logger.warn("知到正式考试收到取消信号, 提前退出答题循环.")
                return stats
            existing = self._find_row(question, exam_key, course_id=course_id)
            remembered = self._remember_question(question, exam_key, course_id=course_id)
            row = self._merge_row(remembered, existing)
            values, source = self._answer_question(question, row)
            if source in ("题库", "随机", "排除法"):
                if self._wait_sleep(random.uniform(1.5, 3.0)):
                    logger.warn("知到正式考试收到取消信号, 提前退出答题循环.")
                    return stats
            self._save_answer(
                question=question,
                values=values,
                recruit_id=recruit_id,
                course_id=course_id,
                homework_id=exam_id,
                stu_exam_id=student_exam_id,
                exam_id=exam_id,
                school_id=school_id,
            )
            stats["answered"] += 1
            logger.info(
                f"考试作答 {question['eid']}({source}): {question.get('content', '')[:40]}"
            )

        if self._is_cancelled():
            logger.warn("知到正式考试收到取消信号, 放弃自动交卷.")
            return stats
        if not self.auto_submit:
            logger.info("考试答案已保存, 请手动交卷 (data/llm.json 中 auto_submit=true 可自动交卷).")
            return stats
        if stats["needs_vision"]:
            logger.info("存在图片题, 本次不自动交卷; 等待视觉流程补答后再处理.")
            return stats

        try:
            result = self.api.submit(
                recruit_id=recruit_id,
                course_id=course_id,
                exam_id=exam_id,
                student_exam_id=student_exam_id,
                achieve_count=stats["answered"],
            )
        except CaptchaRequiredError as exc:
            exc.retryable = False
            logger.warn(
                "正式考试交卷阶段触发人机验证, 为防重复交卷不自动重试, 请手动核对交卷状态."
            )
            raise
        stats["submitted"] = submit_succeeded(result)
        if not stats["submitted"]:
            logger.warn("考试交卷接口未返回 statu=1, 请手动确认是否已交卷.")
        return stats