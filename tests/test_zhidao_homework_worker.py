import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

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
                                    "id": "q1",
                                    "name": "1+1=?",
                                    "questionType": {"id": 1},
                                    "questionOptions": [
                                        {"id": 11, "content": "1"},
                                        {"id": 12, "content": "2"},
                                        {"id": 13, "content": "3"},
                                    ],
                                    "isCorrect": 0,
                                    "userAnswerVo": {"answer": "11"},
                                },
                                {
                                    "eid": "q2",
                                    "id": "q2",
                                    "name": "选出偶数",
                                    "questionType": {"id": 2},
                                    "questionOptions": [
                                        {"id": 21, "content": "2"},
                                        {"id": 22, "content": "4"},
                                        {"id": 23, "content": "5"},
                                    ],
                                    "isCorrect": 1,
                                    "correctOptions": [21, 22],
                                    "userAnswerVo": {"answer": [21, 22]},
                                },
                                {
                                    "eid": "q3",
                                    "id": "q3",
                                    "name": "写出答案",
                                    "questionType": {"id": 3},
                                    "questionOptions": [],
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

    def analyze(
        self,
        question=None,
        *,
        question_id="",
        user_id="",
        course_id="",
        recruit_id="",
        timeout=120,
    ):
        key = str(question_id or (question or {}).get("eid") or "")
        self.analyze_calls.append((key, user_id, course_id, recruit_id))
        return f"{key}解析"


class BridgedHomeworkApi(FakeHomeworkApi):
    """真实结构: doHomework 只有加密 eid, lookHomework 只返回数字 id."""

    def __init__(self, payload, scores):
        super().__init__(payload, scores)
        self.info_question_ids = []

    def look_homework(self, **kwargs):
        self.look_calls += 1
        return {
            "rt": {
                "examBase": {
                    "workExamParts": [
                        {
                            "questionDtos": [
                                {
                                    "id": 90001,
                                    "name": "<p>双键题</p>",
                                    "questionType": {"id": 2},
                                    "questionOptions": [
                                        {"id": 11, "content": "甲"},
                                        {"id": 12, "content": "乙"},
                                        {"id": 13, "content": "丙"},
                                    ],
                                }
                            ]
                        }
                    ]
                }
            }
        }

    def get_stu_answer_info(self, **kwargs):
        self.info_calls += 1
        self.info_question_ids.append(kwargs.get("question_ids"))
        return {"rt": [{"questionId": 90001, "isRight": 0, "userAnswer": ["11"]}]}


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
        self.assertEqual(api.look_calls, 2)
        self.assertEqual(api.info_calls, 2)
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

    def test_bridges_encrypted_eid_to_numeric_question_id(self):
        payload = _payload(
            _dto("enc_1", 2, "双键题", [(11, "甲"), (12, "乙"), (13, "丙")])
        )
        api = BridgedHomeworkApi(payload, [40, 100])
        analysis = FakeAnalysisApi()
        answerer = ScriptedAnswerer([_ids(11), _ids(12)])
        worker = self._worker(api, answerer, analysis, target_score=100)
        stats = worker.run(
            recruit_id="R1",
            course_id="C1",
            homework_id="H1",
            stu_exam_id="S1",
            exam_id="E1",
        )

        self.assertTrue(stats["target_reached"])
        self.assertEqual(stats["rounds"], 2)
        self.assertEqual(api.info_question_ids, [["90001"], ["90001"]])
        self.assertEqual(analysis.analyze_calls, [("90001", "777", "C1", "R1")])
        # 次轮使用桥接后的数字 question_id 带来的解析与错误组合
        self.assertIn("【错误选择方式】", answerer.prompts[1])
        self.assertIn("甲", answerer.prompts[1])
        self.assertIn("【历史AI解析】90001解析", answerer.prompts[1])
        self.assertEqual([call["answer"] for call in api.saved], ["11", "12"])
        row = self.store.get_by_eid("C1", "enc_1", "E1")
        self.assertEqual(row["question_id"], "90001")
        self.assertEqual(row["ai_analysis"], "90001解析")
        self.assertEqual(row["wrong_options"], [["11"]])

    def test_percent_score_uses_total_score(self):
        payload = _payload(
            _dto("q5", 1, "五十分制", [(51, "甲"), (52, "乙")])
        )
        payload["rt"]["examBase"]["totalScore"] = 50
        api = FakeHomeworkApi(payload, [35])
        answerer = ScriptedAnswerer([_ids(51)])
        worker = self._worker(api, answerer, None, target_score=60)
        stats = worker.run(
            recruit_id="R1",
            course_id="C1",
            homework_id="H1",
            stu_exam_id="S1",
            exam_id="E1",
        )

        self.assertEqual(stats["score"], 35.0)
        self.assertEqual(stats["score_percent"], 70.0)
        self.assertTrue(stats["target_reached"])
        self.assertEqual(stats["rounds"], 1)
        self.assertEqual(api.submit_calls, 1)
        self.assertEqual(api.look_calls, 1)

    def test_first_round_reached_caches_real_dict_is_current_without_correct_options(self):
        payload = _payload(
            _dto("enc_single", 1, "真实单选", [(101, "甲"), (102, "乙")]),
            _dto("enc_fill", 3, "真实填空"),
        )

        class RealDictReviewApi(FakeHomeworkApi):
            def look_homework(self, **kwargs):
                self.look_calls += 1
                # 真实 lookHomework 仅返回数字 id 且隐藏 correctOptions
                return {
                    "status": "200",
                    "rt": {
                        "examBase": {
                            "workExamParts": [
                                {
                                    "questionDtos": [
                                        {
                                            "id": 91001,
                                            "name": "真实单选",
                                            "questionType": {"id": 1},
                                            "questionOptions": [
                                                {"id": 101, "content": "甲"},
                                                {"id": 102, "content": "乙"},
                                            ],
                                        },
                                        {
                                            "id": 91002,
                                            "name": "真实填空",
                                            "questionType": {"id": 3},
                                            "questionOptions": [],
                                        },
                                    ]
                                }
                            ]
                        }
                    },
                }

            def get_stu_answer_info(self, **kwargs):
                self.info_calls += 1
                return {
                    "status": "200",
                    "rt": {
                        "91001": {
                            "questionId": 91001,
                            "answer": "101",
                            "isCurrent": "1",
                        },
                        "91002": {
                            "questionId": 91002,
                            "answer": "答案A/答案B",
                            "isCurrent": "1",
                        },
                    },
                }

        api = RealDictReviewApi(payload, [100])
        answerer = ScriptedAnswerer([_ids(101), _fills("答案A", "答案B")])
        worker = self._worker(api, answerer, None, target_score=100)
        stats = worker.run(
            recruit_id="R1",
            course_id="C1",
            homework_id="H_REAL",
            stu_exam_id="S_REAL",
            exam_id="E_REAL",
        )

        self.assertTrue(stats["target_reached"])
        self.assertEqual(stats["rounds"], 1)
        self.assertEqual(api.look_calls, 1)
        self.assertEqual(api.info_calls, 1)
        row_single = self.store.get_by_id("C1", "91001", "E_REAL")
        row_fill = self.store.get_by_id("C1", "91002", "E_REAL")
        self.assertEqual(row_single["correct_options"], ["101"])
        self.assertEqual(row_fill["correct_options"], ["答案A", "答案B"])

    def test_first_round_reached_ignores_review_exception(self):
        from unittest.mock import patch

        payload = _payload(_dto("q_ok", 1, "一次过", [(1, "A"), (2, "B")]))
        api = FakeHomeworkApi(payload, [100])
        answerer = ScriptedAnswerer([_ids(1)])
        worker = self._worker(api, answerer, None, target_score=60)
        with patch.object(
            worker, "_apply_review", side_effect=RuntimeError("review apply glitch")
        ) as apply_mock:
            stats = worker.run(
                recruit_id="R1",
                course_id="C1",
                homework_id="H_ERR",
                stu_exam_id="S_ERR",
                exam_id="E_ERR",
            )
        self.assertTrue(stats["target_reached"])
        self.assertEqual(stats["score"], 100.0)
        apply_mock.assert_called_once()

    def test_apply_review_tri_state_is_current_empty_and_info_error_do_not_record_wrong(self):
        payload = _payload(
            _dto("q_ungraded", 1, "未判分题", [(11, "A"), (12, "B")]),
            _dto("q_nested_ok", 1, "嵌套判对题", [(21, "C"), (22, "D")]),
        )

        class TriStateReviewApi(FakeHomeworkApi):
            def look_homework(self, **kwargs):
                self.look_calls += 1
                return {
                    "status": "200",
                    "rt": {
                        "examBase": {
                            "workExamParts": [
                                {
                                    "questionDtos": [
                                        {
                                            "id": 92001,
                                            "name": "未判分题",
                                            "questionType": {"id": 1},
                                            "questionOptions": [
                                                {"id": 11, "content": "A"},
                                                {"id": 12, "content": "B"},
                                            ],
                                            "userAnswerVo": {"answer": "11", "isCurrent": ""},
                                        },
                                        {
                                            "id": 92002,
                                            "name": "嵌套判对题",
                                            "questionType": {"id": 1},
                                            "questionOptions": [
                                                {"id": 21, "content": "C"},
                                                {"id": 22, "content": "D"},
                                            ],
                                            "userAnswerVo": {"answer": "21", "isCurrent": "1"},
                                        },
                                    ]
                                }
                            ]
                        }
                    },
                }

            def get_stu_answer_info(self, **kwargs):
                self.info_calls += 1
                return {
                    "status": "200",
                    "rt": {
                        "92001": {"questionId": 92001, "answer": "11", "isCurrent": ""},
                    },
                }

        analysis_calls = []

        class _SpyAnalysisApi:
            def get_analysis(self, **kwargs):
                analysis_calls.append(kwargs)
                return {"rt": {"analysis": "不应被调用"}}

        api = TriStateReviewApi(payload, [100])
        worker = self._worker(
            api, ScriptedAnswerer([_ids(11), _ids(21)]), _SpyAnalysisApi(), target_score=100
        )
        stats = worker.run(
            recruit_id="R1",
            course_id="C1",
            homework_id="H_TRI",
            stu_exam_id="S_TRI",
            exam_id="E_TRI",
        )
        self.assertTrue(stats["target_reached"])
        row_ungraded = self.store.get_by_id("C1", "92001", "E_TRI")
        self.assertEqual(row_ungraded.get("wrong_options") or [], [])
        row_nested_ok = self.store.get_by_id("C1", "92002", "E_TRI")
        self.assertEqual(row_nested_ok["correct_options"], ["21"])
        self.assertEqual(row_nested_ok.get("wrong_options") or [], [])
        self.assertEqual(analysis_calls, [])

        # getStuAnswerInfo 抛异常（info={}）时，lookHomework 带回 userAnswerVo 也不误标错题
        class InfoErrorApi(TriStateReviewApi):
            def get_stu_answer_info(self, **kwargs):
                self.info_calls += 1
                raise RuntimeError("info timeout")

        api_err = InfoErrorApi(payload, [100])
        worker_err = self._worker(
            api_err, ScriptedAnswerer([_ids(11), _ids(21)]), _SpyAnalysisApi(), target_score=100
        )
        worker_err.run(
            recruit_id="R1",
            course_id="C1",
            homework_id="H_INFO_ERR",
            stu_exam_id="S_INFO_ERR",
            exam_id="E_INFO_ERR",
        )
        row_err_ungraded = self.store.get_by_id("C1", "92001", "E_INFO_ERR")
        self.assertEqual(row_err_ungraded.get("wrong_options") or [], [])
        row_err_ok = self.store.get_by_id("C1", "92002", "E_INFO_ERR")
        self.assertEqual(row_err_ok["correct_options"], ["21"])
        self.assertEqual(analysis_calls, [])

    def test_single_blank_fill_with_comma_is_not_split_on_comma(self):
        payload = _payload(_dto("f_comma", 3, "数值填空"))

        class CommaFillApi(FakeHomeworkApi):
            def look_homework(self, **kwargs):
                self.look_calls += 1
                return {
                    "status": "200",
                    "rt": {
                        "examBase": {
                            "workExamParts": [
                                {
                                    "questionDtos": [
                                        {
                                            "id": 93001,
                                            "name": "数值填空",
                                            "questionType": {"id": 3},
                                            "questionOptions": [],
                                        }
                                    ]
                                }
                            ]
                        }
                    },
                }

            def get_stu_answer_info(self, **kwargs):
                self.info_calls += 1
                return {
                    "status": "200",
                    "rt": {
                        "93001": {
                            "questionId": 93001,
                            "answer": "1,000",
                            "isCurrent": "1",
                        }
                    },
                }

        api = CommaFillApi(payload, [100])
        worker = self._worker(api, ScriptedAnswerer([_fills("1,000")]), None, target_score=100)
        stats = worker.run(
            recruit_id="R1",
            course_id="C1",
            homework_id="H_COMMA",
            stu_exam_id="S_COMMA",
            exam_id="E_COMMA",
        )
        self.assertTrue(stats["target_reached"])
        row = self.store.get_by_id("C1", "93001", "E_COMMA")
        self.assertEqual(row["correct_options"], ["1,000"])

    def test_submit_captcha_minus_12_does_not_retry_in_outer_captcha_wrappers(self):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch
        from modules import homework
        from modules.stages.zhidao_assessment import _run_zhidao_with_captcha
        from modules.zhs_api import CaptchaRequiredError

        class SubmitCaptchaHomeworkApi(FakeHomeworkApi):
            def submit(self, **kwargs):
                self.submit_calls += 1
                raise CaptchaRequiredError("submit -12", code="-12")

        api = SubmitCaptchaHomeworkApi(self.payload, [])
        worker = self._worker(api, ScriptedAnswerer([_ids(11), _ids(21, 22), _fills("牛顿")]), None)
        run_calls = 0

        def _run_hw():
            nonlocal run_calls
            run_calls += 1
            return worker.run(
                recruit_id="R1",
                course_id="C1",
                homework_id="H_SUB",
                stu_exam_id="S_SUB",
                exam_id="E_SUB",
            )

        with patch.object(
            homework, "_settle_captcha", AsyncMock(return_value=True)
        ) as settle_mock:
            with self.assertRaises(CaptchaRequiredError) as ctx1:
                asyncio.run(
                    _run_zhidao_with_captcha(
                        page=MagicMock(),
                        client=MagicMock(),
                        config=MagicMock(),
                        logger=MagicMock(),
                        label="第一章作业",
                        captcha_url="",
                        run_fn=_run_hw,
                    )
                )
        self.assertFalse(ctx1.exception.retryable)
        self.assertEqual(run_calls, 1)
        self.assertEqual(api.submit_calls, 1)
        settle_mock.assert_not_awaited()

        # 验证 homework._try_api_with_captcha 也不对交卷阶段 -12 重试
        api_calls = 0

        async def _fake_try_api(_page, _config, _params):
            nonlocal api_calls
            api_calls += 1
            raise CaptchaRequiredError("submit -12", code="-12", retryable=False)

        with (
            patch.object(homework, "_try_zhidao_api", side_effect=_fake_try_api),
            patch.object(
                homework, "_settle_captcha", AsyncMock(return_value=True)
            ) as settle_mock2,
        ):
            with self.assertRaises(CaptchaRequiredError):
                asyncio.run(homework._try_api_with_captcha(MagicMock(), MagicMock(), {}))
        self.assertEqual(api_calls, 1)
        settle_mock2.assert_not_awaited()

    def test_do_homework_captcha_minus_12_propagates(self):
        from modules.zhs_api import CaptchaRequiredError

        class CaptchaApi(FakeHomeworkApi):
            def do_homework(self, **kwargs):
                raise CaptchaRequiredError("接口要求验证码(code=-12)")

        worker = self._worker(CaptchaApi(self.payload, []), ScriptedAnswerer([]), None)
        with self.assertRaises(CaptchaRequiredError):
            worker.run(
                recruit_id="R1",
                course_id="C1",
                homework_id="H1",
                stu_exam_id="S1",
                exam_id="E1",
            )

    def test_fill_cache_hits_without_llm(self):
        api = FakeHomeworkApi(_payload(_dto("f1", 3, "默写填空")), [])
        self.store.save_options_by_eid(
            "C1", "E1", "f1", [], question_type="3", content="默写填空"
        )
        self.store.mark_correct(
            "C1", "E1", eid="f1", correct_options=["甲", "乙"]
        )

        def boom(_prompt):
            raise AssertionError("填空命中缓存不应调用大模型")

        worker = self._worker(api, boom, None, auto_submit=False)
        stats = worker.run(
            recruit_id="R1",
            course_id="C1",
            homework_id="H1",
            stu_exam_id="S1",
            exam_id="E1",
        )
        self.assertEqual(stats["answered"], 1)
        self.assertEqual(api.saved[0]["answer"], "甲/乙")

    def test_max_rounds_zero_with_server_max_redo_runs_up_to_three_rounds(self):
        payload = _payload(
            _dto("q1", 1, "1+1=?", [(11, "1"), (12, "2"), (13, "3")])
        )
        payload["rt"]["examBase"]["maxRedo"] = 3
        api = FakeHomeworkApi(payload, [20, 40, 50])
        answerer = ScriptedAnswerer([_ids(11), _ids(12), _ids(13)])
        worker = self._worker(
            api, answerer, None, target_score=80, max_rounds=0
        )
        stats = worker.run(
            recruit_id="R1",
            course_id="C1",
            homework_id="H1",
            stu_exam_id="S1",
            exam_id="E1",
        )
        self.assertEqual(stats["rounds"], 3)
        self.assertEqual(api.submit_calls, 3)
        self.assertEqual(api.tran_calls, 2)
        self.assertFalse(stats["target_reached"])

    def test_non_100_scale_score_uses_percentage_not_raw_score(self):
        payload = _payload(
            _dto("q1", 1, "200分制题", [(11, "1"), (12, "2"), (13, "3")])
        )
        payload["rt"]["examBase"]["totalScore"] = 200
        # 100/200 = 50% < 80% 不应提前停止; 160/200 = 80% >= 80% 达标停止
        api = FakeHomeworkApi(payload, [100, 160])
        answerer = ScriptedAnswerer([_ids(11), _ids(12)])
        worker = self._worker(
            api, answerer, None, target_score=80, max_rounds=3
        )
        stats = worker.run(
            recruit_id="R1",
            course_id="C1",
            homework_id="H1",
            stu_exam_id="S1",
            exam_id="E1",
        )
        self.assertEqual(stats["rounds"], 2)
        self.assertEqual(stats["score"], 160.0)
        self.assertEqual(stats["score_percent"], 80.0)
        self.assertTrue(stats["target_reached"])
        self.assertEqual(api.submit_calls, 2)

    def test_multi_blank_fill_preserves_order_and_duplicate_values(self):
        payload = _payload(_dto("f2", 3, "双空填空题"))

        class MultiBlankApi(FakeHomeworkApi):
            def look_homework(self, **kwargs):
                self.look_calls += 1
                return {
                    "rt": {
                        "examBase": {
                            "workExamParts": [
                                {
                                    "questionDtos": [
                                        {
                                            "eid": "f2",
                                            "id": "f2",
                                            "name": "双空填空题",
                                            "questionType": {"id": 3},
                                            "questionOptions": [],
                                            "isCorrect": 0,
                                            "userAnswerVo": {"answer": "1/1"},
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                }

            def get_stu_answer_info(self, **kwargs):
                self.info_calls += 1
                return {"rt": [{"questionId": "f2", "isRight": 0, "userAnswer": "1/1"}]}

        api = MultiBlankApi(payload, [0, 100])
        # 预置历史错解 ["甲", "乙"]，第一轮填 ["1", "1"] 被判错后应原样保留两空，
        # 第二轮模型返回逆序 ["乙", "甲"] 应被接受而非因无序去重被拒。
        self.store.save_options_by_eid(
            "C1", "E1", "f2", [], question_type="3", content="双空填空题"
        )
        self.store.record_wrong(
            "C1",
            "E1",
            eid="f2",
            wrong_options=["甲", "乙"],
            preserve_order=True,
        )
        answerer = ScriptedAnswerer([_fills("1", "1"), _fills("乙", "甲")])
        worker = self._worker(api, answerer, None, target_score=80, max_rounds=3)
        stats = worker.run(
            recruit_id="R1",
            course_id="C1",
            homework_id="H1",
            stu_exam_id="S1",
            exam_id="E1",
        )
        self.assertTrue(stats["target_reached"])
        self.assertEqual([call["answer"] for call in api.saved], ["1/1", "乙/甲"])
        row = self.store.get_by_eid("C1", "f2", "E1")
        self.assertIn(["甲", "乙"], row["wrong_options"])
        self.assertIn(["1", "1"], row["wrong_options"])

    def test_cached_qid_or_old_eid_hits_bank_when_new_eid_arrives_and_uses_exam_key(self):
        # 1) 缓存中仅有 question_id（无 eid）
        self.store.save_options_by_id(
            "C1",
            "H1",
            "901",
            [{"id": "11", "content": "甲"}, {"id": "12", "content": "乙"}],
            question_type="1",
            content="仅qid题",
        )
        self.store.mark_correct(
            "C1", "H1", question_id="901", correct_options=["12"]
        )
        # 2) 缓存中存在旧 eid 的已掌握记录
        self.store.save_options_by_eid(
            "C1",
            "H1",
            "old_eid",
            [{"id": "21", "content": "丙"}, {"id": "22", "content": "丁"}],
            question_type="1",
            content="旧eid题",
        )
        self.store.mark_correct(
            "C1", "H1", eid="old_eid", correct_options=["21"]
        )

        payload = _payload(
            {
                "eid": "fresh_eid_1",
                "name": "仅qid题",
                "questionType": {"id": 1},
                "questionOptions": [
                    {"id": 11, "content": "甲"},
                    {"id": 12, "content": "乙"},
                ],
            },
            {
                "eid": "fresh_eid_2",
                "name": "旧eid题",
                "questionType": {"id": 1},
                "questionOptions": [
                    {"id": 21, "content": "丙"},
                    {"id": 22, "content": "丁"},
                ],
            },
        )
        api = FakeHomeworkApi(payload, [])

        def boom(_prompt):
            raise AssertionError("命中已掌握缓存不应调用大模型")

        worker = self._worker(api, boom, None, auto_submit=False)
        stats = worker.run(
            recruit_id="R1",
            course_id="C1",
            homework_id="H1",
            stu_exam_id="S1",
            exam_id="",
        )
        self.assertEqual(stats["answered"], 2)
        self.assertEqual([call["answer"] for call in api.saved], ["12", "21"])
        # exam_id 为空时 _save_answer 应统一使用 exam_key (homework_id="H1")
        self.assertEqual([call["exam_id"] for call in api.saved], ["H1", "H1"])


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

    def test_think_block_content_is_ignored(self):
        think = "<thi" + "nk>"
        end = "</thi" + "nk>"
        question = _normalized("q", 2, "题", [(101, "甲"), (102, "乙")])
        reply = (
            f"{think}分析中选 [101] 或 [A]{end}\n"
            '```answer\n[{"id": 102}]\n```'
        )
        self.assertEqual(parse_homework_answer(reply, question), (["102"], []))
        self.assertEqual(
            parse_homework_answer(f'{think}内部 101{end}\n["102"]', question),
            (["102"], []),
        )

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


class FakeHomeworkListApi:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get_student_homework(self, **kwargs):
        self.calls.append(kwargs)
        page = int(kwargs.get("page_num") or 0)
        return self.pages.get(page, {"rt": {"studentHomeworkList": [], "next": -1}})


class HomeworkScanTests(unittest.TestCase):
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

    def _worker(self, api):
        worker = ZhidaoHomeworkWorker(
            api,
            course_id="C1",
            store=self.store,
            answerer=lambda _prompt: "",
            sleep=lambda _seconds: None,
        )
        worker._recruit_id = "R1"
        return worker

    def test_scan_pages_follows_next_and_dedupes(self):
        api = FakeHomeworkListApi(
            {
                0: {
                    "rt": {
                        "studentHomeworkList": [
                            {
                                "id": "S1",
                                "examId": "E1",
                                "recruitId": "R1",
                                "courseId": "C1",
                                "examName": "作业一",
                            }
                        ],
                        "next": 2,
                    }
                },
                2: {
                    "rt": {
                        "studentHomeworkList": [
                            {"id": "S1", "examId": "E1"},
                            {
                                "id": "S2",
                                "examId": "E2",
                                "recruitId": "R1",
                                "courseId": "C1",
                                "examName": "作业二",
                            },
                        ],
                        "next": -1,
                    }
                },
            }
        )
        worker = self._worker(api)
        items = worker.scan(flag=1)
        self.assertEqual([item["examId"] for item in items], ["E1", "E2"])
        self.assertEqual(api.calls[0]["flag"], 1)
        self.assertEqual(api.calls[0]["page_size"], 100)
        self.assertEqual(api.calls[0]["page_num"], 0)
        self.assertEqual(api.calls[1]["page_num"], 2)
        self.assertEqual(api.calls[0]["recruit_id"], "R1")

    def test_run_pending_passes_item_ids_and_course(self):
        api = FakeHomeworkListApi(
            {
                0: {
                    "rt": {
                        "studentHomeworkList": [
                            {
                                "id": "S1",
                                "examId": "E1",
                                "recruitId": "R1",
                                "courseId": "C9",
                                "examName": "作业",
                                "schoolId": "SCH9",
                            }
                        ],
                        "next": -1,
                    }
                }
            }
        )
        worker = self._worker(api)
        worker.run = MagicMock(return_value={"answered": 2, "submitted": True})
        results = worker.run_pending(course_id="C1", school_id="SCH")
        worker.run.assert_called_once_with(
            recruit_id="R1",
            course_id="C9",
            homework_id="E1",
            stu_exam_id="S1",
            exam_id="E1",
            school_id="SCH9",
        )
        self.assertEqual(results[0]["homework_id"], "E1")
        self.assertEqual(results[0]["exam_name"], "作业")

    def test_run_pending_uses_worker_recruit_and_school_and_skips_out_of_window(self):
        from datetime import datetime

        api = FakeHomeworkListApi(
            {
                0: {
                    "rt": {
                        "studentHomeworkList": [
                            {
                                "id": "S_OPEN",
                                "examId": "E_OPEN",
                                "courseId": 1000013343,
                                "examName": "第一章单元测试",
                                "startTime": "2026-10-19 00:00:00",
                                "endDate": "2026-11-30 23:59:59",
                            },
                            {
                                "id": "S_FUTURE",
                                "examId": "E_FUTURE",
                                "courseId": 1000013343,
                                "examName": "未开放作业",
                                "startTime": "2026-12-01 00:00:00",
                                "endDate": "2026-12-10 23:59:59",
                            },
                            {
                                "id": "S_OLD",
                                "examId": "E_OLD",
                                "courseId": 1000013343,
                                "examName": "已截止作业",
                                "startTime": "2026-09-01 00:00:00",
                                "endDate": "2026-10-01 23:59:59",
                            },
                        ],
                        "next": -1,
                    }
                }
            }
        )
        worker = ZhidaoHomeworkWorker(
            api,
            course_id="1000013343",
            recruit_id="458533",
            school_id="1847",
            store=self.store,
            sleep=lambda _s: None,
        )
        worker.run = MagicMock(return_value={"answered": 5, "submitted": False})
        results = worker.run_pending(now=datetime(2026, 11, 24, 9, 0, 0))
        self.assertEqual(len(results), 3)
        worker.run.assert_called_once_with(
            recruit_id="458533",
            course_id="1000013343",
            homework_id="E_OPEN",
            stu_exam_id="S_OPEN",
            exam_id="E_OPEN",
            school_id="1847",
        )
        self.assertEqual(results[1]["skipped"], True)
        self.assertEqual(results[1]["reason"], "not_started")
        self.assertEqual(results[2]["skipped"], True)
        self.assertEqual(results[2]["reason"], "expired")

    def test_open_homework_resets_when_do_homework_raises_submitted_error(self):
        from modules.zhs_api import ZhsApiError

        class ResetOnErrorApi(FakeHomeworkApi):
            def __init__(self):
                super().__init__(
                    _payload(_dto("q1", 1, "1+1=?", ((1, "1"), (2, "2")))), [100]
                )
                self.attempts = 0

            def do_homework(self, **kwargs):
                self.attempts += 1
                if self.attempts == 1:
                    raise ZhsApiError("status=500 msg=试卷已提交,不能继续答题")
                return super().do_homework(**kwargs)

        api = ResetOnErrorApi()
        worker = ZhidaoHomeworkWorker(
            api,
            course_id="C1",
            recruit_id="R1",
            store=self.store,
            answerer=lambda _prompt: _ids(2),
            auto_submit=False,
            sleep=lambda _s: None,
        )
        stats = worker.run(
            recruit_id="R1",
            course_id="C1",
            homework_id="E1",
            stu_exam_id="S1",
        )
        self.assertEqual(api.attempts, 2)
        self.assertEqual(api.tran_calls, 1)
        self.assertEqual(stats["answered"], 1)

    def test_should_redo_submitted_checks_score_attempts_and_deadline(self):
        from datetime import datetime
        from modules.zhidao_homework_worker import payload_max_redo, should_redo_submitted

        now = datetime(2026, 11, 24, 9, 0, 0)
        self.assertEqual(payload_max_redo({"rt": {"backNum": 3}}), 3)
        self.assertTrue(
            should_redo_submitted(
                {
                    "score": 12,
                    "totalScore": "20",
                    "isMarking": 1,
                    "backNum": 3,
                    "startTime": "2026-10-19 00:00:00",
                    "endDate": "2026-11-30 23:59:59",
                },
                target_score=100,
                now=now,
            )
        )
        self.assertFalse(
            should_redo_submitted(
                {
                    "score": 20,
                    "totalScore": "20",
                    "isMarking": 1,
                    "backNum": 3,
                    "endDate": "2026-11-30 23:59:59",
                },
                target_score=100,
                now=now,
            )
        )
        self.assertFalse(
            should_redo_submitted(
                {
                    "score": 12,
                    "totalScore": "20",
                    "isMarking": 3,
                    "backNum": 3,
                    "endDate": "2026-11-30 23:59:59",
                },
                target_score=100,
                now=now,
            )
        )
        self.assertFalse(
            should_redo_submitted(
                {
                    "score": 12,
                    "totalScore": "20",
                    "isMarking": 0,
                    "backNum": 3,
                    "endDate": "2026-11-01 00:00:00",
                },
                target_score=100,
                now=now,
            )
        )

    def test_cross_exam_fallback_reuses_analysis_without_leaking_wrong_options(self):
        # hw-old 记录了 wrong_options=["999"] 与 ai_analysis，但未记录 correct_options
        self.store.save_options_by_id(
            "C1",
            "hw-old",
            "old_q1",
            [{"id": "999", "content": "旧A"}, {"id": "998", "content": "旧B"}],
            question_type="1",
            content="同课程跨卷复用题干",
        )
        self.store.record_wrong("C1", "hw-old", question_id="old_q1", wrong_options=["999"])
        self.store.mark_correct(
            "C1",
            "hw-old",
            question_id="old_q1",
            correct_options=[],
            ai_analysis="选第二项因为能量守恒",
            auto_clear_wrong=False,
        )

        # 新测验中选项 ID 不同（11/12），只应复用 ai_analysis，不应混入旧 wrong_options=["999"]
        new_q = _normalized("new_eid", 1, "同课程跨卷复用题干", ((11, "新A"), (12, "新B")))
        worker = ZhidaoHomeworkWorker(
            FakeHomeworkApi(_payload(), [100]),
            course_id="C1",
            store=self.store,
            sleep=lambda _: None,
        )
        found = worker._find_row(new_q, "hw-new", "C1")
        self.assertIsNotNone(found)
        self.assertEqual(found.get("ai_analysis"), "选第二项因为能量守恒")
        self.assertEqual(found.get("wrong_options"), [])


class HomeworkHttpCaptchaTests(unittest.TestCase):
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
        self.payload = _payload(_dto("q1", 1, "1+1=?", ((1, "2"), (2, "3"))))

    def _worker(self, api, answerer, analysis_api=None, **kwargs):
        options = {
            "course_id": "C1",
            "store": self.store,
            "answerer": answerer,
            "analysis_api": analysis_api,
            "auto_submit": True,
            "sleep": lambda _seconds: None,
        }
        options.update(kwargs)
        return ZhidaoHomeworkWorker(api, **options)

    def _valid_fp(self):
        return ("H" + "kL9+/=_-3mNpQ4rS") * 10 + "abcdef:1764200000123"

    def test_http_captcha_unlocks_and_retries_do_homework_and_save_answer(self):
        from modules.yidun_http import CaptchaSolution
        from modules.zhs_api import CaptchaRequiredError

        class CaptchaOnceApi(FakeHomeworkApi):
            def __init__(self, payload, scores):
                super().__init__(payload, scores)
                self.do_attempts = 0
                self.save_attempts = 0
                self.validate_calls = []

            def do_homework(self, **kwargs):
                self.do_attempts += 1
                if self.do_attempts == 1:
                    raise CaptchaRequiredError("接口要求验证码(code=-12)", code="-12")
                return super().do_homework(**kwargs)

            def save_student_answer(self, **kwargs):
                self.save_attempts += 1
                if self.save_attempts == 1:
                    raise CaptchaRequiredError("接口要求验证码(code=-12)", code="-12")
                return super().save_student_answer(**kwargs)

            def validate_slide_token(self, **kwargs):
                self.validate_calls.append(kwargs)
                return {"status": "200", "pass": True}

        class FakeSolver:
            def __init__(self):
                self.calls = []

            def solve(self, trigger, fp=None):
                self.calls.append((trigger.kind, fp))
                return CaptchaSolution(
                    validate=f"CN31_solved_{len(self.calls)}_v_i_1",
                    raw_validate="raw",
                )

        fp = self._valid_fp()
        api = CaptchaOnceApi(self.payload, [100])
        solver = FakeSolver()
        worker = self._worker(
            api,
            ScriptedAnswerer([_ids(1)]),
            None,
            auto_submit=False,
            captcha_solver=solver,
            fp_provider=lambda: fp,
        )
        stats = worker.run(
            recruit_id="R1",
            course_id="C1",
            homework_id="H1",
            stu_exam_id="S1",
            exam_id="E1",
        )
        self.assertEqual(stats["answered"], 1)
        self.assertEqual(api.do_attempts, 2)
        self.assertEqual(api.save_attempts, 2)
        self.assertEqual(len(solver.calls), 2)
        self.assertEqual(len(api.validate_calls), 2)
        self.assertEqual(api.validate_calls[0]["recruit_id"], "R1")
        self.assertEqual(api.validate_calls[0]["exam_id"], "E1")
        self.assertEqual(api.validate_calls[0]["token"], "CN31_solved_1_v_i_1")

    def test_http_captcha_falls_back_when_fp_missing_or_disabled(self):
        from modules.yidun_http import CaptchaSolution
        from modules.zhs_api import CaptchaRequiredError

        class AlwaysCaptchaApi(FakeHomeworkApi):
            def __init__(self, payload, scores):
                super().__init__(payload, scores)
                self.validate_calls = []

            def do_homework(self, **kwargs):
                raise CaptchaRequiredError("接口要求验证码(code=-12)", code="-12")

            def validate_slide_token(self, **kwargs):
                self.validate_calls.append(kwargs)
                return {"status": "200", "pass": True}

        solver_called = []

        class SpySolver:
            def solve(self, trigger, fp=None):
                solver_called.append((trigger, fp))
                return CaptchaSolution(validate="CN31_x_v_i_1", raw_validate="raw")

        # 无 fp 时直接上抛回退浏览器，不调用求解器也不调用 validateSlideToken
        api1 = AlwaysCaptchaApi(self.payload, [])
        worker1 = self._worker(
            api1,
            ScriptedAnswerer([]),
            None,
            captcha_solver=SpySolver(),
            fp_provider=lambda: None,
        )
        with self.assertRaises(CaptchaRequiredError):
            worker1.run(
                recruit_id="R1",
                course_id="C1",
                homework_id="H1",
                stu_exam_id="S1",
                exam_id="E1",
            )
        self.assertEqual(solver_called, [])
        self.assertEqual(api1.validate_calls, [])

        # 关闭自动验证码时即便有 fp 也直接回退
        api2 = AlwaysCaptchaApi(self.payload, [])
        worker2 = self._worker(
            api2,
            ScriptedAnswerer([]),
            None,
            captcha_solver=SpySolver(),
            fp_provider=self._valid_fp,
            enable_http_captcha=False,
        )
        with self.assertRaises(CaptchaRequiredError):
            worker2.run(
                recruit_id="R1",
                course_id="C1",
                homework_id="H1",
                stu_exam_id="S1",
                exam_id="E1",
            )
        self.assertEqual(solver_called, [])
        self.assertEqual(api2.validate_calls, [])

    def test_http_captcha_falls_back_on_solve_failure_or_validate_rejected_or_error(self):
        from modules.yidun_http import CaptchaSolution
        from modules.zhs_api import CaptchaRequiredError

        class RejectValidateApi(FakeHomeworkApi):
            def __init__(self, payload, scores, *, mode="reject"):
                super().__init__(payload, scores)
                self.mode = mode
                self.do_attempts = 0
                self.validate_calls = []

            def do_homework(self, **kwargs):
                self.do_attempts += 1
                raise CaptchaRequiredError("接口要求验证码(code=-12)", code="-12")

            def validate_slide_token(self, **kwargs):
                self.validate_calls.append(kwargs)
                if self.mode == "error":
                    raise RuntimeError("protocol changed")
                if self.mode == "pass":
                    return {"status": "200", "pass": True}
                return {"status": "200", "pass": False}

        # 1) 求解返回 None -> 不调用 validateSlideToken，静默回退
        api_none = RejectValidateApi(self.payload, [], mode="pass")
        worker_none = self._worker(
            api_none,
            ScriptedAnswerer([]),
            None,
            captcha_solver=lambda _trigger: None,
            fp_provider=self._valid_fp,
        )
        with self.assertRaises(CaptchaRequiredError):
            worker_none.run(
                recruit_id="R1", course_id="C1", homework_id="H1", stu_exam_id="S1", exam_id="E1"
            )
        self.assertEqual(api_none.do_attempts, 1)
        self.assertEqual(api_none.validate_calls, [])

        # 2) validateSlideToken 返回 pass=False -> 不重试原请求，静默回退
        api_reject = RejectValidateApi(self.payload, [], mode="reject")
        worker_reject = self._worker(
            api_reject,
            ScriptedAnswerer([]),
            None,
            captcha_solver=lambda _trigger: CaptchaSolution("CN31_bad_v_i_1", "raw"),
            fp_provider=self._valid_fp,
        )
        with self.assertRaises(CaptchaRequiredError):
            worker_reject.run(
                recruit_id="R1", course_id="C1", homework_id="H1", stu_exam_id="S1", exam_id="E1"
            )
        self.assertEqual(api_reject.do_attempts, 1)
        self.assertEqual(len(api_reject.validate_calls), 1)

        # 3) validateSlideToken 抛异常（协议变更） -> 静默回退原 CaptchaRequiredError
        api_err = RejectValidateApi(self.payload, [], mode="error")
        worker_err = self._worker(
            api_err,
            ScriptedAnswerer([]),
            None,
            captcha_solver=lambda _trigger: CaptchaSolution("CN31_err_v_i_1", "raw"),
            fp_provider=self._valid_fp,
        )
        with self.assertRaises(CaptchaRequiredError):
            worker_err.run(
                recruit_id="R1", course_id="C1", homework_id="H1", stu_exam_id="S1", exam_id="E1"
            )
        self.assertEqual(api_err.do_attempts, 1)

        # 4) pass=True 但重试仍返回 -12 -> 最多只重试一次，随后回退
        api_still = RejectValidateApi(self.payload, [], mode="pass")
        worker_still = self._worker(
            api_still,
            ScriptedAnswerer([]),
            None,
            captcha_solver=lambda _trigger: CaptchaSolution("CN31_ok_v_i_1", "raw"),
            fp_provider=self._valid_fp,
        )
        with self.assertRaises(CaptchaRequiredError):
            worker_still.run(
                recruit_id="R1", course_id="C1", homework_id="H1", stu_exam_id="S1", exam_id="E1"
            )
        self.assertEqual(api_still.do_attempts, 2)


if __name__ == "__main__":
    unittest.main()