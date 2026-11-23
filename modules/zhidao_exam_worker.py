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
    extract_exam_questions,
    format_student_answer,
)
from modules.zhidao_homework_worker import ZhidaoHomeworkWorker, question_has_image

logger = Logger()

__all__ = ["ZhidaoExamWorker"]


class ZhidaoExamWorker(ZhidaoHomeworkWorker):
    """正式考试没有查错/重做闭环, 复用作业的答案生成与题库逻辑."""

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
        self.api.save_student_answer(
            recruit_id=recruit_id,
            course_id=course_id,
            exam_id=exam_id,
            student_exam_id=stu_exam_id,
            school_id=school_id,
            eid=question["eid"],
            question_type=question.get("type", 1),
            answer=answer,
        )

    # ------------------------------------------------------------------ run
    def scan(self, flag: int = 1) -> list[dict]:
        return extract_final_exams(
            self.api.get_student_final_exam(
                course_id=self.course_id,
                recruit_id=self._recruit_id,
                flag=flag,
            )
        )

    def run_pending(self, *, course_id="", school_id="") -> list[dict]:
        """扫描未完成考试并逐场接管, 返回每场统计."""
        results = []
        for exam in self.scan(flag=1):
            exam_id = exam.get("examId", "")
            if not is_exam_started(exam):
                logger.warn(
                    f"考试「{exam.get('examName') or exam_id}」尚未开始, "
                    "请手动进入考试后再运行."
                )
                results.append(
                    {
                        "exam_id": exam_id,
                        "skipped": True,
                        "reason": "not_started",
                        "answered": 0,
                        "submitted": False,
                    }
                )
                continue
            stats = self.run(
                recruit_id=exam.get("recruitId", ""),
                course_id=exam.get("courseId") or course_id,
                exam_id=exam_id,
                student_exam_id=exam.get("studentExamId", ""),
                school_id=exam.get("schoolId") or school_id,
                exam_name=exam.get("examName", ""),
            )
            stats["exam_id"] = exam_id
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

        payload = self.api.do_exam(
            recruit_id=recruit_id,
            exam_id=exam_id,
            student_exam_id=student_exam_id,
            school_id=school_id,
            course_id=course_id,
        )
        questions = extract_exam_questions(payload)
        if not questions:
            logger.warn(f"考试「{exam_name or exam_id}」未获取到题目, 跳过.")
            return stats
        if any(question_has_image(question) for question in questions):
            logger.warn("考试含图片题, 回退视觉模型逐题作答.")
            stats["needs_vision"] = True
            return stats

        exam_key = str(exam_id)
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
                homework_id=exam_id,
                stu_exam_id=student_exam_id,
                exam_id=exam_id,
                school_id=school_id,
            )
            stats["answered"] += 1
            logger.info(
                f"考试作答 {question['eid']}({source}): {question.get('content', '')[:40]}"
            )

        if not self.auto_submit:
            logger.info("考试答案已保存, 请手动交卷 (data/llm.json 中 auto_submit=true 可自动交卷).")
            return stats

        result = self.api.submit(
            recruit_id=recruit_id,
            course_id=course_id,
            exam_id=exam_id,
            student_exam_id=student_exam_id,
            achieve_count=stats["answered"],
        )
        stats["submitted"] = submit_succeeded(result)
        if not stats["submitted"]:
            logger.warn("考试交卷接口未返回 statu=1, 请手动确认是否已交卷.")
        return stats