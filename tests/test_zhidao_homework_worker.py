import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modules.answer_store import AiQuestionStore
from modules.zhidao_homework_worker import (
    ZhidaoHomeworkWorker,
    build_homework_prompt,
    parse_homework_answer,
    question_has_image,
)


def _dto(eid, qtype, content, options=()):
    return {
        "eid": eid,
        "id": eid,
        "name": content,
        "questionType": {"id": qtype},
        "questionOptions": [
            {"id": oid, "content": text} for oid, text in options
        ],
    }


def _normalized(eid, qtype, content, options=()):
    return {
        "eid": eid,
        "question_id": eid,
        "type": qtype,
        "type_name": {1: "single", 2: "multiple", 3: "fill", 14: "judge"}[qtype],
        "content": content,
        "options": [{"id": str(oid), "content": text} for oid, text in options],
        "raw": {},
    }


def _payload(*questions):
    return {
        "rt": {
            "examBase": {
                "workExamParts": [{"questionDtos": list(questions)}]
            }
        }
    }


def _ids(*values):
    return "```answer\n" + json.dumps([{"id": value} for value in values]) + "\n```"


def _fills(*values):
    return "```answer\n" + json.dumps(list(values), ensure_ascii=False) + "\n```"


class _QuietLogger:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


class FakeHomeworkApi:
    def __init__(self, payload, scores):
        self.payload = payload
        self.scores = list(scores)
        self.saved = []
        self.do_calls = 0
        self.submit_calls = 0
        self.look_calls = 0
        self.info_calls = 0
        self.tran_calls = 0

    def do_homework(self, **kwargs):
        self.do_calls += 1
        return self.payload

    def save_student_answer(self, **kwargs):
        self.saved.append(kwargs)
        return {"status": "200", "rt": {}}

    def submit(self, **kwargs):
        self.submit_calls += 1
        score = self.scores.pop(0) if self.scores else 100
        return {"status": "200", "rt": {"score": score}}

    def look_homework(self, **kwargs):
        self.look_calls += 1
        return {
            "rt": {
                "examBase": {
                    "workExamParts": [
                        {
                            "questionDtos": [
                                {
                                    "eid": "q1",
                                    "isCorrect": 0,
                                    "userAnswerVo": {"answer": "11"},
                                },
                                {
                                    "eid": "q2",
                                    "isCorrect": 1,
                                    "correctOptions": [21, 22],
                                    "userAnswerVo": {"answer": [21, 22]},
                                },
                                {
                                    "eid": "q3",
                                    "isCorrect": 0,
                                    "userAnswerVo": {"answer": "错答"},
                                },
                            ]
                        }
                    ]
                }
            }
        }

    def get_stu_answer_info(self, **kwargs):
        self.info_calls += 1
        return {"rt": [{"eid": "q1", "isRight": 0, "userAnswer": "11"}]}

    def save_course_tran(self, **kwargs):
        self.tran_calls += 1


class FakeAnalysisApi:
    def __init__(self):
        self.user_calls = 0
        self.analyze_calls = []

    def get_user_id(self, course_id="", recruit_id=""):
        self.user_calls += 1
        return "777"

    def analyze(self, question, *, user_id="", course_id="", recruit_id="", timeout=120):
        self.analyze_calls.append((question["eid"], user_id, course_id, recruit_id))
        return f"{question['eid']}解析"


class ScriptedAnswerer:
    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        return self.replies.pop(0)


class HomeworkWorkerLifecycleTests(unittest.TestCase):
    def setUp(self):
        patcher = patch("modules.zhidao_homework_worker.logger", _QuietLogger())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = AiQuestionStore(
            db_path=root / "bank.db", data_root=root, migrate=False
        )
        self.questions = [
            _dto("q1", 1, "1+1=?", [(11, "1"), (12, "2"), (13, "3")]),
            _dto("q2", 2, "选出偶数", [(21, "2"), (22, "4"), (23, "5")]),
            _dto("q3", 3, "写出答案"),
        ]
        self.payload = _payload(*self.questions)

    def _worker(self, api, answerer, analysis_api, **kwargs):
        options = {
            "course_id": "C1",
            "course_name": "测试课",
            "store": self.store,
            "answerer": answerer,
            "analysis_api": analysis_api,
            "auto_submit": True,
            "target_score": 60,
            "sleep": lambda _seconds: None,
        }
        options.update(kwargs)
        return ZhidaoHomeworkWorker(api, **options)

    def test_full_lifecycle_with_wrong_redo_and_ai_analysis(self):
        api = FakeHomeworkApi(self.payload, [40, 100])
        analysis = FakeAnalysisApi()
        answerer = ScriptedAnswerer(
            [
                _ids(11),
                _ids(21, 22),
                _fills("错答"),
                _ids(12),
                _fills("正确答案"),
            ]
        )
        worker = self._worker(api, answerer, analysis)
        stats = worker.run(
            recruit_id="R1",
            course_id="C1",
            homework_id="H1",
            stu_exam_id="S1",
            exam_id="E1",
            school_id="SCH",
        )

        self.assertEqual(stats["answered"], 3)
        self.assertTrue(stats["submitted"])
        self.assertEqual(stats["score"], 100.0)
        self.assertTrue(stats["target_reached"])
        self.assertEqual(stats["rounds"], 2)
        self.assertEqual(stats["is_marking"], 1)

        self.assertEqual(api.do_calls, 2)
        self.assertEqual(api.submit_calls, 2)
        self.assertEqual(api.look_calls, 1)
        self.assertEqual(api.info_calls, 1)
        self.assertEqual(api.tran_calls, 1)

        # 首轮三题走 LLM, 次轮只重答错题 (多选命中题库)
        self.assertEqual(len(answerer.prompts), 5)
        self.assertIn("【排除选项】", answerer.prompts[3])
        self.assertIn("q1解析", answerer.prompts[3])
        self.assertIn("【错误选择方式】", answerer.prompts[4])
        self.assertIn("q3解析", answerer.prompts[4])

        # 两次 review 只对答错题请求官方解析
        self.assertEqual(analysis.user_calls, 1)
        self.assertEqual(
            [item[0] for item in analysis.analyze_calls], ["q1", "q3"]
        )
        self.assertTrue(
            all(item[3] == "R1" for item in analysis.analyze_calls)
        )

        self.assertEqual(len(api.saved), 6)
        second_round = api.saved[3:]
        self.assertEqual([call["answer"] for call in second_round], ["12", "21,22", "正确答案"])
        self.assertTrue(all(call["exam_type"] == "" for call in second_round))
        self.assertTrue(all(call["stu_exam_id"] == "S1" for call in second_round))
        self.assertTrue(all(call["eid"] for call in second_round))

        row1 = self.store.get_by_eid("C1", "q1", "E1")
        row2 = self.store.get_by_eid("C1", "q2", "E1")
        self.assertEqual(row1["ai_analysis"], "q1解析")
        self.assertIn(["11"], row1["wrong_options"])
        self.assertEqual(row2["correct_options"], ["21", "22"])

    def test_without_auto_submit_only_saves(self):
        api = FakeHomeworkApi(self.payload, [])
        analysis = FakeAnalysisApi()
        answerer = ScriptedAnswerer([_ids(11), _ids(21, 22), _fills("x")])
        worker = self._worker(api, answerer, analysis, auto_submit=False)
        stats = worker.run(
            recruit_id="R1",
            course_id="C1",
            homework_id="H1",
            stu_exam_id="S1",
            exam_id="E1",
        )
        self.assertEqual(stats["answered"], 3)
        self.assertFalse(stats["submitted"])
        self.assertEqual(api.submit_calls, 0)
        self.assertEqual(api.look_calls, 0)
        self.assertEqual(api.tran_calls, 0)

    def test_single_choice_exclusion_skips_llm(self):
        api = FakeHomeworkApi(
            _payload(
                _dto("q9", 1, "唯一答案", [(1, "错"), (2, "对")])
            ),
            [],
        )
        self.store.save_options_by_eid(
            "C1",
            "E1",
            "q9",
            [{"id": "1", "content": "错"}, {"id": "2", "content": "对"}],
            question_type="1",
            content="唯一答案",
        )
        self.store.record_wrong("C1", "E1", eid="q9", wrong_options=["1"])

        def boom(_prompt):
            raise AssertionError("排除法不应调用大模型")

        worker = self._worker(api, boom, None, auto_submit=False)
        stats = worker.run(
            recruit_id="R1",
            course_id="C1",
            homework_id="H1",
            stu_exam_id="S1",
            exam_id="E1",
        )
        self.assertEqual(stats["answered"], 1)
        self.assertEqual(api.saved[0]["answer"], "2")

    def test_image_question_requests_vision_fallback_without_saving(self):
        api = FakeHomeworkApi(
            _payload(
                _dto(
                    "img",
                    1,
                    '<p>看图选择</p><img src="https://cdn.example/x.png">',
                    [(1, "甲"), (2, "乙")],
                )
            ),
            [],
        )

        def boom(_prompt):
            raise AssertionError("图片题不应调用文本模型")

        worker = self._worker(api, boom, None)
        stats = worker.run(
            recruit_id="R1",
            course_id="C1",
            homework_id="H1",
            stu_exam_id="S1",
            exam_id="E1",
        )
        self.assertTrue(stats["needs_vision"])
        self.assertEqual(api.saved, [])
        self.assertEqual(api.submit_calls, 0)
        self.assertEqual(api.look_calls, 0)


class PromptAndParseTests(unittest.TestCase):
    def test_parse_homework_answer_supports_ids_letters_and_text(self):
        question = _normalized("q", 2, "题", [(11, "甲"), (12, "乙")])
        self.assertEqual(parse_homework_answer(_ids(11), question), (["11"], []))
        self.assertEqual(parse_homework_answer(_ids("11"), question), (["11"], []))
        self.assertEqual(parse_homework_answer(_ids("A"), question), (["11"], []))
        self.assertEqual(parse_homework_answer(_ids(1), question), (["11"], []))
        self.assertEqual(parse_homework_answer(_ids("乙"), question), (["12"], []))
        self.assertEqual(parse_homework_answer("not-json", question), ([], []))

    def test_parse_fill_answer(self):
        question = _normalized("q", 3, "填空")
        self.assertEqual(parse_homework_answer(_fills("甲", "乙"), question), ([], ["甲", "乙"]))

    def test_prompt_injects_exclusions_combos_and_analysis(self):
        question = _normalized("q", 2, "题", [(11, "甲"), (12, "乙"), (13, "丙")])
        prompt = build_homework_prompt(
            question,
            wrong_combos=[["11"]],
            analysis="历史解析",
            excluded=["11"],
        )
        self.assertIn("【排除选项】", prompt)
        self.assertIn("11:甲", prompt)
        self.assertIn("【错误选择方式】", prompt)
        self.assertIn("【历史AI解析】历史解析", prompt)
        self.assertIn("```answer", prompt)

    def test_question_has_image_ignores_spacer_gif(self):
        self.assertFalse(
            question_has_image(
                _normalized(
                    "q",
                    1,
                    '<img src="data:image/gif;base64,r0lgodAAA">',
                )
            )
        )
        self.assertTrue(
            question_has_image(
                _normalized("q", 1, '<img src="https://cdn.example/q.png">')
            )
        )
        with_image_option = _normalized("q", 1, "题", [(1, '<img src="https://x/a.jpg">')])
        self.assertTrue(question_has_image(with_image_option))


if __name__ == "__main__":
    unittest.main()