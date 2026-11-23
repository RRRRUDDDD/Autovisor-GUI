# encoding=utf-8
import unittest
from unittest.mock import patch

from modules import question_bank as qb
from modules.exam_ctx import AiExamBase, build_answer_prompt
from modules.zhidao_homework_worker import ZhidaoHomeworkWorker, build_homework_prompt

TOKEN = "test-token"

QUESTION = {
    "eid": 1,
    "type": 1,
    "content": "<p>2 + 2 = ?</p>",
    "options": [
        {"id": 11, "content": "<b>3</b>"},
        {"id": 12, "content": "<b>4</b>"},
    ],
}

HOMEWORK_QUESTION = {
    "eid": 7,
    "type": 1,
    "content": "<p>地球自转方向</p>",
    "options": [
        {"id": 21, "content": "<p>自西向东</p>"},
        {"id": 22, "content": "<p>自东向西</p>"},
    ],
}


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class _Session:
    def __init__(self, handler, headers=None):
        self.handler = handler
        self.headers = dict(headers or {})
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        result = self.handler(url, kwargs)
        if isinstance(result, Exception):
            raise result
        return result


class QuestionBankClientTests(unittest.TestCase):
    def _client(self, handler, token=TOKEN, **kwargs):
        session = _Session(handler)
        client = qb.QuestionBankClient(
            token=token, query_url="https://bank.example/query", session=session, **kwargs
        )
        return client, session

    def test_disabled_when_token_empty(self):
        def handler(url, kwargs):  # pragma: no cover - 不应被调用
            raise AssertionError(f"空 token 不应发起请求: {url}")

        client, session = self._client(handler, token="")
        self.assertFalse(client.enabled())
        self.assertIsNone(client.query("题目", ["A", "B"]))
        self.assertEqual(session.calls, [])
        self.assertEqual(client.stats(), (0, 0))

    def test_info_is_lazy_loaded_once_before_first_query(self):
        def handler(url, kwargs):
            if url.endswith("/info"):
                return _Response({"code": 1, "data": {"times": 3}})
            return _Response({"code": 0, "data": None})

        client, session = self._client(handler)
        client.query("题一", ["A"])
        client.query("题二", ["B"])
        info_calls = [call for call in session.calls if call[0].endswith("/info")]
        query_calls = [call for call in session.calls if call[0].endswith("/query")]
        self.assertEqual(len(info_calls), 1)
        self.assertEqual(info_calls[0][1]["params"], {"token": TOKEN})
        self.assertEqual(len(query_calls), 2)
        self.assertEqual(client.remaining, 3)
        self.assertEqual(client.stats(), (0, 2))

    def test_hit_syncs_remaining_times(self):
        def handler(url, kwargs):
            if url.endswith("/info"):
                return _Response({"code": 1, "data": {"times": 10}})
            return _Response({"code": 1, "data": {"answer": "B", "times": 9}})

        client, _ = self._client(handler)
        self.assertEqual(client.query("2 + 2 = ?", ["3", "4"]), "B")
        self.assertEqual(client.remaining, 9)
        self.assertEqual(client.stats(), (1, 1))

    def test_miss_does_not_decrement_remaining(self):
        def handler(url, kwargs):
            if url.endswith("/info"):
                return _Response({"code": 1, "data": {"times": 10}})
            return _Response({"code": 0, "data": None})

        client, _ = self._client(handler)
        self.assertIsNone(client.query("未知题目", ["A"]))
        self.assertEqual(client.remaining, 10)
        self.assertEqual(client.stats(), (0, 1))

    def test_zero_remaining_skips_query_requests(self):
        def handler(url, kwargs):
            if url.endswith("/info"):
                return _Response({"code": 1, "data": {"times": 0}})
            raise AssertionError("余额为 0 时不应请求查询接口")

        client, session = self._client(handler)
        self.assertIsNone(client.query("题目", ["A"]))
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(client.stats(), (0, 0))

    def test_network_and_json_errors_degrade_to_none(self):
        def handler(url, kwargs):
            if url.endswith("/info"):
                return ConnectionError("boom")
            return _Response(ValueError("bad json"))

        client, _ = self._client(handler)
        self.assertIsNone(client.query("题目", ["A"]))
        self.assertEqual(client.stats(), (0, 1))

    def test_session_headers_strip_sensitive_fields(self):
        def handler(url, kwargs):
            return _Response({"code": 0, "data": None})

        session = _Session(
            handler,
            headers={
                "Cookie": "CASTGC=x",
                "Origin": "https://www.zhihuishu.com",
                "Referer": "https://www.zhihuishu.com/",
                "Authorization": "Bearer x",
            },
        )
        client = qb.QuestionBankClient(
            token=TOKEN, query_url="https://bank.example/query", session=session
        )
        self.assertTrue(client.enabled())
        lowered = {key.lower(): value for key, value in session.headers.items()}
        for key in ("cookie", "origin", "referer", "authorization"):
            self.assertNotIn(key, lowered)
        self.assertEqual(session.headers["User-Agent"], qb.USER_AGENT)
        client.query("题目", ["A"])
        for _, kwargs in session.calls:
            self.assertNotIn("headers", kwargs)
            self.assertNotIn("cookies", kwargs)

    def test_fresh_session_never_carries_cookies(self):
        client = qb.QuestionBankClient(token=TOKEN)
        lowered = {key.lower() for key in client.session.headers}
        self.assertNotIn("cookie", lowered)
        self.assertNotIn("origin", lowered)
        self.assertNotIn("referer", lowered)
        self.assertFalse(client.session.trust_env)
        self.assertEqual(len(client.session.cookies), 0)

    def test_type_mapping_and_params(self):
        seen = []

        def handler(url, kwargs):
            if url.endswith("/info"):
                return _Response({"code": 1, "data": {"times": 5}})
            seen.append(kwargs["params"])
            return _Response({"code": 0, "data": None})

        client, _ = self._client(handler)
        for qtype in (1, 2, 3, 14, 99):
            client.query("题目", ["选项A", "选项B"], qtype)
        self.assertEqual(
            [params["type"] for params in seen],
            ["single", "multiple", "completion", "judgement", "unknown"],
        )
        self.assertEqual(seen[0]["title"], "题目")
        self.assertEqual(seen[0]["options"], "选项A\n选项B")
        self.assertEqual(seen[0]["token"], TOKEN)

    def test_answer_extraction_supports_lists_and_dicts(self):
        def handler(url, kwargs):
            if url.endswith("/info"):
                return _Response({"code": 1, "data": {"times": 5}})
            return _Response(
                {"code": 1, "data": {"answer": [{"content": "A"}, {"content": "B"}]}}
            )

        client, _ = self._client(handler)
        self.assertEqual(client.query("多选题", ["A", "B"], 2), "A | B")

    def test_log_stats_emits_event(self):
        def handler(url, kwargs):
            if url.endswith("/info"):
                return _Response({"code": 1, "data": {"times": 5}})
            return _Response({"code": 1, "data": {"answer": "A", "times": 4}})

        client, _ = self._client(handler)
        client.query("题目", ["A"])
        with patch("modules.question_bank.logger.event") as event:
            client.log_stats()
        event.assert_called_once()
        self.assertEqual(event.call_args[0][0], "第三方题库统计")
        self.assertEqual(event.call_args[1], {"命中": 1, "查询": 1, "剩余": 4})

    def test_from_config_builds_disabled_client_for_blank_token(self):
        with patch(
            "modules.llm.question_bank_conf",
            return_value={"token": "", "url": "https://tk.enncy.cn/query"},
        ):
            client = qb.QuestionBankClient.from_config()
        self.assertFalse(client.enabled())
        self.assertEqual(client.query_url, "https://tk.enncy.cn/query")


class BankReferenceInjectionTests(unittest.TestCase):
    def test_homework_prompt_includes_bank_reference(self):
        prompt = build_homework_prompt(HOMEWORK_QUESTION, bank_reference="答案是 自西向东")
        self.assertIn("【第三方题库参考】", prompt)
        self.assertIn("答案是 自西向东", prompt)

    def test_exam_prompt_includes_bank_reference(self):
        prompt = build_answer_prompt(
            1,
            "2 + 2 = ?",
            [{"id": 11, "content": "3"}, {"id": 12, "content": "4"}],
            extra={"题库参考": "答案选 4"},
        )
        self.assertIn("【第三方题库参考】", prompt)
        self.assertIn("答案选 4", prompt)

    def test_exam_without_reference_has_no_bank_section(self):
        prompt = build_answer_prompt(
            1, "2 + 2 = ?", [{"id": 11, "content": "3"}], extra={}
        )
        self.assertNotIn("第三方题库参考", prompt)


class _FakeBank:
    def __init__(self, answer="B"):
        self.answer = answer
        self.calls = []
        self.logged = 0

    def enabled(self):
        return True

    def query(self, title, options=None, question_type=1):
        self.calls.append((title, list(options or []), question_type))
        return self.answer

    def log_stats(self):
        self.logged += 1


class _DisabledBank(_FakeBank):
    def enabled(self):
        return False


class _DummyApi:
    session = object()
    proxy = ""
    lock = None


class _DummyExam(AiExamBase):
    def _fetch_sheet_items(self):
        return []

    def _finish(self, submit, sheets):
        return (True, 0, 0)


class _CapturingAnswerer:
    def __init__(self, ids):
        self.ids = ids
        self.prompts = []

    def answer(self, prompt):
        self.prompts.append(prompt)
        return list(self.ids)


class ExamAnswerPipelineBankTests(unittest.TestCase):
    def _exam(self, bank):
        exam = _DummyExam(
            _DummyApi(), "course", "knowledge", "exam-test", "exam-paper", question_bank=bank
        )
        return exam

    def test_bank_reference_uses_plain_text(self):
        bank = _FakeBank("B")
        exam = self._exam(bank)
        reference = exam._bank_reference(
            {"content": "<p>2 + 2 = ?</p>"},
            1,
            [{"id": 11, "content": "<b>3</b>"}, {"id": 12, "content": "<b>4</b>"}],
        )
        self.assertEqual(reference, "B")
        self.assertEqual(bank.calls, [("2 + 2 = ?", ["3", "4"], 1)])

    def test_disabled_bank_returns_empty_reference(self):
        bank = _DisabledBank()
        exam = self._exam(bank)
        self.assertEqual(exam._bank_reference({"content": "题目"}, 1, []), "")
        self.assertEqual(bank.calls, [])

    def test_generate_answer_injects_reference_into_prompt(self):
        exam = self._exam(_FakeBank("B"))
        answerer = _CapturingAnswerer([12])
        exam.zhidao_answerer = answerer
        ids = exam._generate_answer(
            1,
            {"content": "2 + 2 = ?"},
            [{"id": 11, "content": "3"}, {"id": 12, "content": "4"}],
        )
        self.assertEqual(ids, [12])
        self.assertEqual(len(answerer.prompts), 1)
        self.assertIn("【第三方题库参考】", answerer.prompts[0])
        self.assertIn("B", answerer.prompts[0])

    def test_bank_errors_do_not_break_answer_generation(self):
        class _BrokenBank(_FakeBank):
            def query(self, *args, **kwargs):
                raise RuntimeError("bank down")

        exam = self._exam(_BrokenBank())
        answerer = _CapturingAnswerer([11])
        exam.zhidao_answerer = answerer
        ids = exam._generate_answer(
            1, {"content": "2 + 2 = ?"}, [{"id": 11, "content": "3"}]
        )
        self.assertEqual(ids, [11])
        self.assertNotIn("第三方题库参考", answerer.prompts[0])


class HomeworkWorkerBankTests(unittest.TestCase):
    def _worker(self, answerer, bank):
        return ZhidaoHomeworkWorker(
            None,
            course_id="course",
            store=object(),
            answerer=answerer,
            question_bank=bank,
            sleep=lambda _: None,
        )

    def test_worker_injects_bank_reference_before_llm(self):
        prompts = []

        def answerer(prompt):
            prompts.append(prompt)
            return '```answer\n[{"id": 21}]\n```'

        bank = _FakeBank("自西向东")
        worker = self._worker(answerer, bank)
        ids, source = worker._answer_question(HOMEWORK_QUESTION, None)
        self.assertEqual(ids, ["21"])
        self.assertEqual(source, "AI")
        self.assertEqual(bank.calls, [("地球自转方向", ["自西向东", "自东向西"], 1)])
        self.assertIn("【第三方题库参考】", prompts[0])
        self.assertIn("自西向东", prompts[0])

    def test_worker_skips_bank_when_cache_hit(self):
        bank = _FakeBank("自西向东")
        worker = self._worker(lambda _: self.fail("缓存命中不应调用 LLM"), bank)
        ids, source = worker._answer_question(
            HOMEWORK_QUESTION, {"correct_options": ["22"]}
        )
        self.assertEqual(ids, ["22"])
        self.assertEqual(source, "题库")
        self.assertEqual(bank.calls, [])

    def test_worker_survives_bank_failure(self):
        class _BrokenBank(_FakeBank):
            def query(self, *args, **kwargs):
                raise RuntimeError("bank down")

        prompts = []

        def answerer(prompt):
            prompts.append(prompt)
            return '```answer\n[{"id": 22}]\n```'

        worker = self._worker(answerer, _BrokenBank())
        ids, source = worker._answer_question(HOMEWORK_QUESTION, None)
        self.assertEqual(ids, ["22"])
        self.assertEqual(source, "AI")
        self.assertNotIn("第三方题库参考", prompts[0])


if __name__ == "__main__":
    unittest.main()