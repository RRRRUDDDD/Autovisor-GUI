# encoding=utf-8
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from modules import ai_smart_mastery as mastery_mod


class _DummyLogger:
    def info(self, *_args, **_kwargs):
        pass

    def warn(self, *_args, **_kwargs):
        pass

    def error(self, *_args, **_kwargs):
        pass

    def debug(self, *_args, **_kwargs):
        pass

    def event(self, *_args, **_kwargs):
        pass

    def section(self, *_args, **_kwargs):
        pass


class _FakeCache:
    def __init__(self):
        self.items = {}

    def put(self, text, answer):
        self.items[text] = answer


class _FakeMasteryPage:
    def __init__(self):
        self.listeners = []

    def on(self, event, handler):
        self.listeners.append((event, handler))

    def remove_listener(self, event, handler):
        self.listeners = [item for item in self.listeners if item != (event, handler)]

    async def goto(self, *_args, **_kwargs):
        return None

    async def wait_for_selector(self, *_args, **_kwargs):
        return None

    async def wait_for_load_state(self, *_args, **_kwargs):
        return None

    async def wait_for_timeout(self, _ms):
        return None


class AiSmartMasteryFixesTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_mastery_removes_response_listener_and_uses_async_sleep(self):
        page = _FakeMasteryPage()
        cache = _FakeCache()
        logger = _DummyLogger()
        learn_url = "https://ai-smart-course-student-pro.zhihuishu.com/learnPage/100/200/300"

        hung_started = asyncio.Event()
        hung_cancelled = False

        class _SlowResponse:
            url = "https://ai-smart-course-student-pro.zhihuishu.com/exam/questions-paper"

            async def text(self):
                nonlocal hung_cancelled
                hung_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    hung_cancelled = True
                    raise
                return "{}"

        async def fake_open_practice(_page, _url, _logger):
            # Trigger a slow response handler that stays pending when run_mastery exits
            for ev, fn in list(page.listeners):
                if ev == "response":
                    fn(_SlowResponse())
            await hung_started.wait()
            return {"paperId": "P1"}

        sleep_calls = []

        async def fake_async_sleep(sec):
            sleep_calls.append(sec)

        with (
            patch.object(mastery_mod, "open_practice", side_effect=fake_open_practice),
            patch.object(mastery_mod, "answer_exam", new=AsyncMock(return_value=[{"cached": True}])),
            patch.object(mastery_mod, "submit_exam", new=AsyncMock(return_value=False)),
            patch.object(mastery_mod, "read_mastery", side_effect=[50, 100]),
            patch.object(mastery_mod.asyncio, "sleep", side_effect=fake_async_sleep),
            patch.object(mastery_mod.time, "sleep", side_effect=AssertionError("must not call blocking time.sleep")),
        ):
            res = await mastery_mod.run_mastery(
                page, learn_url, cache, logger, max_attempts=2, target=100
            )

        self.assertEqual(res["mastery"], 100)
        self.assertEqual(page.listeners, [], "response listener must be removed in finally")
        self.assertTrue(hung_cancelled, "pending response task must be cancelled in finally")
        self.assertEqual(sleep_calls, [0.5])

    async def test_run_course_mastery_counts_target_reached_as_improved_to_avoid_early_abort(self):
        page = _FakeMasteryPage()
        cache = _FakeCache()
        logger = _DummyLogger()
        nodes = [
            {"nodeUid": "n1", "name": "知识点1", "module": "M1", "unit": "U1", "freeExam": 0},
            {"nodeUid": "n2", "name": "知识点2", "module": "M1", "unit": "U1", "freeExam": 0},
        ]

        # Pass 1: n1 stays 50, n2 stays 50 -> both go to pass 2
        # Pass 2: n1 reaches target 100 (improved!), n2 stays 50 -> must continue to pass 3!
        # Pass 3: n2 reaches target 100 -> all done!
        read_seq = iter([50, 50, 50, 50, 50])
        run_seq = iter([
            {"mastery": 50, "attempts": []},   # pass 1 n1
            {"mastery": 50, "attempts": []},   # pass 1 n2
            {"mastery": 100, "attempts": []},  # pass 2 n1 (reached target)
            {"mastery": 50, "attempts": []},   # pass 2 n2 (stuck this pass)
            {"mastery": 100, "attempts": []},  # pass 3 n2 (reached target)
        ])

        with (
            patch.object(mastery_mod, "read_node_mastery", side_effect=lambda *_a, **_kw: next(read_seq)),
            patch.object(mastery_mod, "run_mastery", side_effect=lambda *_a, **_kw: next(run_seq)),
        ):
            summary = await mastery_mod.run_course_mastery(
                page,
                nodes,
                "100",
                "300",
                cache,
                logger,
                target=100,
                max_attempts=2,
                passes=3,
            )

        self.assertEqual(len(summary["passes"]), 3, "pass 3 must run because n1 reached target in pass 2")
        self.assertEqual(summary["passes"][-1]["still"], 0)
        self.assertEqual(len(summary["done"]), 2)
        self.assertEqual(summary["failed"], [])

    async def test_run_course_mastery_normalizes_string_free_exam_values(self):
        from modules.exam_api import is_free_exam, normalize_free_exam

        self.assertEqual(normalize_free_exam("0"), 0)
        self.assertEqual(normalize_free_exam(""), 0)
        self.assertEqual(normalize_free_exam(None), 0)
        self.assertEqual(normalize_free_exam(False), 0)
        self.assertEqual(normalize_free_exam("1"), 1)
        self.assertEqual(normalize_free_exam(1), 1)
        self.assertEqual(normalize_free_exam(True), 1)
        self.assertFalse(is_free_exam({"freeExam": "0"}))
        self.assertTrue(is_free_exam({"freeExam": "1"}))

        parsed = mastery_mod._nodes_from_theme_payload(
            {
                "data": {
                    "courseThemeList": [
                        {
                            "themeName": "M1",
                            "subThemeList": [
                                {
                                    "themeName": "U1",
                                    "knowledgeList": [
                                        {"knowledgeId": 1, "knowledgeName": "非免考字符串0", "freeExam": "0"},
                                        {"knowledgeId": 2, "knowledgeName": "非免考空串", "freeExam": ""},
                                        {"knowledgeId": 3, "knowledgeName": "免考字符串1", "freeExam": "1"},
                                    ],
                                }
                            ],
                        }
                    ]
                }
            }
        )
        self.assertEqual([n["freeExam"] for n in parsed], [0, 0, 1])

        page = _FakeMasteryPage()
        cache = _FakeCache()
        logger = _DummyLogger()
        nodes = [
            {"nodeUid": "n1", "name": "字符串0", "module": "M1", "unit": "U1", "freeExam": "0"},
            {"nodeUid": "n2", "name": "空串", "module": "M1", "unit": "U1", "freeExam": ""},
            {"nodeUid": "n3", "name": "免考1", "module": "M1", "unit": "U1", "freeExam": "1"},
        ]
        with (
            patch.object(mastery_mod, "read_node_mastery", new=AsyncMock(return_value=50)),
            patch.object(mastery_mod, "run_mastery", new=AsyncMock(return_value={"mastery": 100, "attempts": []})),
        ):
            summary = await mastery_mod.run_course_mastery(
                page, nodes, "100", "300", cache, logger, target=100, max_attempts=1, passes=1
            )
        self.assertEqual([item["name"] for item in summary["done"]], ["字符串0", "空串"])
        self.assertEqual([item["name"] for item in summary["skipped"]], ["免考1"])


class AiSmartMasteryUnitTests(unittest.TestCase):
    def test_url_and_percent_parsers(self):
        self.assertTrue(
            mastery_mod.is_ai_leaning_url(
                "https://ai-smart-course-student-pro.zhihuishu.com/learnPage/10/20/30"
            )
        )
        self.assertFalse(mastery_mod.is_ai_leaning_url("https://example.com"))
        self.assertEqual(
            mastery_mod.parse_learn_url(
                "https://ai-smart-course-student-pro.zhihuishu.com/learnPage/10/20/30?x=1"
            ),
            {"courseId": "10", "nodeUid": "20", "classId": "30"},
        )
        self.assertEqual(mastery_mod.parse_learn_url("https://example.com"), {})
        self.assertEqual(
            mastery_mod.parse_exam_url(
                "https://x/studentReviewTestOrExam/99/1/2/88/nodeA/?paperId=777"
            ),
            {
                "examTestId": "99",
                "courseId": "88",
                "nodeName": "nodeA",
                "paperId": "777",
            },
        )
        self.assertEqual(mastery_mod.parse_percent("85%"), 85)
        self.assertEqual(mastery_mod.parse_percent("掌握度 100"), 100)
        self.assertIsNone(mastery_mod.parse_percent("无数字"))

    def test_answer_cache_sqlite_roundtrip_and_letter_normalization(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "mastery_answers.json"
            cache = mastery_mod.AnswerCache(cache_path)
            cache.put("下列哪项属于线性结构？", {"letters": ["A", "C"]}, ["栈", "树", "队列"])
            cache.put("简述时间复杂度的含义", {"text": "表示算法执行时间随规模增长的趋势"})
            cache.put("填空题示例", {"values": ["O(n)", "O(1)"]})
            # Empty answer is ignored
            cache.put("无效题", {})

            self.assertEqual(
                cache.get("下列哪项属于线性结构？"),
                {"letters": ["A", "C"]},
            )
            self.assertEqual(
                cache.get("简述时间复杂度的含义"),
                {"text": "表示算法执行时间随规模增长的趋势"},
            )
            self.assertEqual(
                cache.get("填空题示例"),
                {"values": ["O(n)", "O(1)"]},
            )
            self.assertIsNone(cache.get("无效题"))
            # Normalization converts legacy "A、B" into letters while keeping prose text intact
            self.assertEqual(
                mastery_mod.AnswerCache._normalize_record({"text": "A、B、C"}),
                {"letters": ["A", "B", "C"]},
            )
            self.assertEqual(
                mastery_mod.AnswerCache._normalize_record({"text": "circular/linear"}),
                {"text": "circular/linear"},
            )


if __name__ == "__main__":
    unittest.main()
