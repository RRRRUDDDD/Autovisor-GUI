import asyncio
import json
import time
import unittest
from unittest.mock import MagicMock, patch

import requests

from modules import llm

PAGE_TEXT = "在线测验页面文本，包含多道题目和选项。" * 5
QUIZ_JSON = (
    '{"questions": [{"index": 1, "question": "2+2=?", '
    '"options": ["3", "4"], "type": "single", "answer": [1]}]}'
)


def _mock_response(payload: dict, status_code: int = 200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = payload
    resp.text = ""
    resp.raise_for_status.return_value = None
    return resp


class QuizPageAnswererTests(unittest.TestCase):
    def tearDown(self):
        llm.reset_cache()
        llm.set_proxy("")

    def test_prefers_zhidao_answerer_without_deepseek(self):
        with patch("modules.llm.enabled", return_value=False):
            questions = asyncio.run(
                llm.solve_quiz_page(PAGE_TEXT, lambda prompt: QUIZ_JSON)
            )
        self.assertEqual(len(questions), 1)
        self.assertEqual(questions[0]["question"], "2+2=?")
        self.assertEqual(questions[0]["options"], ["3", "4"])
        self.assertEqual(questions[0]["answer"], [1])

    def test_parses_json_fenced_reply(self):
        with patch("modules.llm.enabled", return_value=False):
            questions = asyncio.run(
                llm.solve_quiz_page(
                    PAGE_TEXT, lambda prompt: f"<think>推理过程</think>\n```json\n{QUIZ_JSON}\n```"
                )
            )
        self.assertEqual(len(questions), 1)
        self.assertEqual(questions[0]["answer"], [1])

    def test_falls_back_to_deepseek_when_answerer_fails(self):
        def broken(prompt):
            raise RuntimeError("zhidao down")

        with patch("modules.llm.enabled", return_value=True), patch(
            "modules.llm._conf",
            return_value={
                "api_base": "https://api.example.com",
                "api_key": "key",
                "model": "deepseek-chat",
            },
        ), patch("modules.llm._chat_sync", return_value=QUIZ_JSON) as chat:
            questions = asyncio.run(llm.solve_quiz_page(PAGE_TEXT, broken))
        self.assertEqual(len(questions), 1)
        self.assertTrue(chat.called)

    def test_returns_empty_when_no_source_available(self):
        def broken(prompt):
            raise RuntimeError("zhidao down")

        with patch("modules.llm.enabled", return_value=False):
            questions = asyncio.run(llm.solve_quiz_page(PAGE_TEXT, broken))
        self.assertEqual(questions, [])


class UniversalLlmProtocolTests(unittest.TestCase):
    def tearDown(self):
        llm.reset_cache()
        llm.set_proxy("")

    def test_build_endpoint_for_all_protocols(self):
        self.assertEqual(
            llm.build_endpoint("https://api.openai.com/v1", "gpt-4o", "openai"),
            "https://api.openai.com/v1/chat/completions",
        )
        self.assertEqual(
            llm.build_endpoint("https://api.openai.com/v1/chat/completions", "gpt-4o", "openai"),
            "https://api.openai.com/v1/chat/completions",
        )
        self.assertEqual(
            llm.build_endpoint("https://api.openai.com/v1", "gpt-5", "openai-responses"),
            "https://api.openai.com/v1/responses",
        )
        self.assertEqual(
            llm.build_endpoint("https://api.anthropic.com", "claude-sonnet-4-20250514", "anthropic"),
            "https://api.anthropic.com/v1/messages",
        )
        self.assertEqual(
            llm.build_endpoint("https://generativelanguage.googleapis.com/v1beta", "gemini-2.5-flash", "gemini"),
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
        )
        self.assertEqual(
            llm.build_endpoint("https://myres.openai.azure.com", "gpt4o-dep", "azure", "2024-10-21"),
            "https://myres.openai.azure.com/openai/deployments/gpt4o-dep/chat/completions?api-version=2024-10-21",
        )
        self.assertEqual(
            llm.build_endpoint("http://127.0.0.1:11434", "qwen2.5:7b", "ollama"),
            "http://127.0.0.1:11434/api/chat",
        )

    def test_anthropic_protocol_request_and_response(self):
        mock_resp = _mock_response(
            {
                "content": [
                    {"type": "thinking", "thinking": "internal"},
                    {"type": "text", "text": "正确答案B"},
                ]
            }
        )
        with patch("modules.llm.requests.post", return_value=mock_resp) as post:
            reply = llm.chat_sync(
                "题目",
                conf_override={
                    "provider": "anthropic",
                    "api_format": "anthropic",
                    "api_key": "sk-ant-123",
                    "api_base": "https://api.anthropic.com",
                    "model": "claude-sonnet-4-20250514",
                },
            )
        self.assertEqual(reply, "正确答案B")
        url = post.call_args[0][0]
        headers = post.call_args[1]["headers"]
        self.assertEqual(url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(headers["x-api-key"], "sk-ant-123")
        self.assertEqual(headers["anthropic-version"], "2023-06-01")

    def test_gemini_protocol_request_and_response(self):
        mock_resp = _mock_response(
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": "思考...", "thought": True},
                                {"text": "正确答案C"},
                            ]
                        }
                    }
                ]
            }
        )
        with patch("modules.llm.requests.post", return_value=mock_resp) as post:
            reply = llm.chat_sync(
                "题目",
                json_mode=True,
                conf_override={
                    "provider": "gemini",
                    "api_format": "gemini",
                    "api_key": "AIzaTestKey",
                    "api_base": "https://generativelanguage.googleapis.com/v1beta",
                    "model": "gemini-2.5-flash",
                },
            )
        self.assertEqual(reply, "正确答案C")
        url = post.call_args[0][0]
        headers = post.call_args[1]["headers"]
        body = post.call_args[1]["json"]
        self.assertEqual(
            url,
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
        )
        self.assertEqual(headers["x-goog-api-key"], "AIzaTestKey")
        self.assertEqual(body["generationConfig"]["responseMimeType"], "application/json")

    def test_azure_protocol_uses_api_key_header_and_version(self):
        mock_resp = _mock_response(
            {"choices": [{"message": {"content": "Azure 答案"}}]}
        )
        with patch("modules.llm.requests.post", return_value=mock_resp) as post:
            reply = llm.chat_sync(
                "题目",
                conf_override={
                    "provider": "azure",
                    "api_format": "azure",
                    "api_key": "azure-key",
                    "api_base": "https://demo.openai.azure.com",
                    "model": "gpt4o-deploy",
                    "api_version": "2024-10-21",
                },
            )
        self.assertEqual(reply, "Azure 答案")
        url = post.call_args[0][0]
        headers = post.call_args[1]["headers"]
        self.assertEqual(
            url,
            "https://demo.openai.azure.com/openai/deployments/gpt4o-deploy/chat/completions?api-version=2024-10-21",
        )
        self.assertEqual(headers["api-key"], "azure-key")
        self.assertNotIn("Authorization", headers)

    def test_ollama_local_works_without_api_key_and_bypasses_proxy(self):
        ollama_conf = llm.normalize_conf(
            {
                "provider": "ollama",
                "api_format": "ollama",
                "api_key": "",
                "api_base": "http://127.0.0.1:11434",
                "model": "qwen2.5:7b",
            }
        )
        llm.set_proxy("http://127.0.0.1:7890")
        with patch("modules.llm._conf", return_value=ollama_conf):
            self.assertTrue(llm.enabled())

        mock_resp = _mock_response(
            {"message": {"content": "<think>推理</think>本地模型答案"}}
        )
        with patch("modules.llm.requests.post", return_value=mock_resp) as post:
            reply = llm.chat_sync("题目", conf_override=ollama_conf)
        self.assertEqual(reply, "本地模型答案")
        self.assertEqual(post.call_args[0][0], "http://127.0.0.1:11434/api/chat")
        self.assertNotIn("proxies", post.call_args[1])

    def test_openai_responses_protocol(self):
        mock_resp = _mock_response({"output_text": "Responses 答案"})
        with patch("modules.llm.requests.post", return_value=mock_resp) as post:
            reply = llm.chat_sync(
                "题目",
                conf_override={
                    "provider": "openai",
                    "api_format": "openai-responses",
                    "api_key": "sk-test",
                    "api_base": "https://api.openai.com/v1",
                    "model": "gpt-5",
                },
            )
        self.assertEqual(reply, "Responses 答案")
        self.assertEqual(post.call_args[0][0], "https://api.openai.com/v1/responses")

    def test_openai_400_retries_without_temperature_and_response_format(self):
        bad = _mock_response({"error": {"message": "temperature not supported"}}, status_code=400)
        good = _mock_response({"choices": [{"message": {"content": "推理模型答案"}}]})
        with patch("modules.llm.requests.post", side_effect=[bad, good]) as post:
            reply = llm.chat_sync(
                "题目",
                json_mode=True,
                conf_override={
                    "provider": "openai",
                    "api_format": "openai",
                    "api_key": "sk-test",
                    "api_base": "https://api.openai.com/v1",
                    "model": "o4-mini",
                },
            )
        self.assertEqual(reply, "推理模型答案")
        self.assertEqual(post.call_count, 2)
        first_body = post.call_args_list[0][1]["json"]
        second_body = post.call_args_list[1][1]["json"]
        self.assertIn("temperature", first_body)
        self.assertIn("response_format", first_body)
        self.assertNotIn("temperature", second_body)
        self.assertNotIn("response_format", second_body)


class LlmAnsweringLogTests(unittest.TestCase):
    def tearDown(self):
        llm.reset_cache()
        llm.set_proxy("")

    def test_pick_option_logs_question_and_selected_option(self):
        infos = []
        events = []

        class _CaptureLogger:
            def info(self, msg, shift=False):
                infos.append(msg)

            def warn(self, msg, shift=False):
                pass

            def debug(self, msg):
                pass

            def event(self, name, interval=None, **fields):
                events.append((name, fields))

            @staticmethod
            def summarize_exception(exc):
                return f"{type(exc).__name__}: {exc}"

        async def fake_ask(_q, _opts):
            return "选项B"

        with (
            patch.object(llm, "logger", _CaptureLogger()),
            patch("modules.llm.enabled", return_value=True),
            patch("modules.llm.provider_label", return_value="DeepSeek(deepseek-chat)"),
            patch("modules.llm._ask_llm", side_effect=fake_ask),
        ):
            idx = asyncio.run(llm.pick_option("下列哪项正确？", ["选项A", "选项B"]))

        self.assertEqual(idx, 1)
        self.assertTrue(any("下列哪项正确" in m and "选项B" in m for m in infos))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], "大模型选题")
        self.assertEqual(events[0][1]["选项"], "选项B")
        self.assertIn("下列哪项正确", events[0][1]["题目"])

    def test_answer_with_llm_logs_info_and_event(self):
        infos = []
        events = []

        class _CaptureLogger:
            def info(self, msg, shift=False):
                infos.append(msg)

            def warn(self, msg, shift=False):
                pass

            def debug(self, msg):
                pass

            def event(self, name, interval=None, **fields):
                events.append((name, fields))

            @staticmethod
            def summarize_exception(exc):
                return f"{type(exc).__name__}: {exc}"

        class _Choice:
            def __init__(self, text):
                self._text = text
                self.clicked = False

            async def inner_text(self):
                return self._text

            async def click(self, timeout=2000):
                self.clicked = True

        class _Title:
            async def inner_text(self):
                return "1+1等于几？"

        class _Page:
            async def query_selector(self, selector):
                if selector == ".topic-title":
                    return _Title()
                return None

            async def wait_for_timeout(self, _ms):
                return None

        choices = [_Choice("1"), _Choice("2")]

        async def fake_ask(_q, _opts):
            return "2"

        with (
            patch.object(llm, "logger", _CaptureLogger()),
            patch("modules.llm.enabled", return_value=True),
            patch("modules.llm.provider_label", return_value="OpenAI(gpt-4o)"),
            patch("modules.llm._ask_llm", side_effect=fake_ask),
        ):
            ok = asyncio.run(llm.answer_with_llm(_Page(), None, choices))

        self.assertTrue(ok)
        self.assertTrue(choices[1].clicked)
        self.assertTrue(any("1+1等于几" in m and "选择「2」" in m for m in infos))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], "课中答题")
        self.assertEqual(events[0][1]["结果"], "已作答")
        self.assertEqual(events[0][1]["选项"], "2")

    def test_pick_option_and_answer_with_llm_prefer_zhidao_without_custom_llm(self):
        class _Choice:
            def __init__(self, text):
                self._text = text
                self.clicked = False

            async def inner_text(self):
                return self._text

            async def click(self, timeout=2000):
                self.clicked = True

        class _Title:
            async def inner_text(self):
                return "狭义相对论中洛伦兹因子满足？"

        class _Page:
            async def query_selector(self, selector):
                if selector == ".topic-title":
                    return _Title()
                return None

            async def wait_for_timeout(self, _ms):
                return None

        with patch("modules.llm.enabled", return_value=False):
            idx = asyncio.run(
                llm.pick_option(
                    "狭义相对论中洛伦兹因子满足？",
                    ["A. γ < 1", "B. γ >= 1"],
                    answerer=lambda _prompt: "正确答案是 B",
                )
            )
            self.assertEqual(idx, 1)

            choices = [_Choice("γ < 1"), _Choice("γ >= 1")]
            ok = asyncio.run(
                llm.answer_with_llm(
                    _Page(),
                    None,
                    choices,
                    answerer=lambda _prompt: "γ >= 1",
                )
            )
            self.assertTrue(ok)
            self.assertTrue(choices[1].clicked)

    def test_pick_option_falls_back_to_custom_llm_when_zhidao_fails(self):
        def broken(_prompt):
            raise RuntimeError("zhidao down")

        async def fake_ask(_q, _opts):
            return "选项A"

        with (
            patch("modules.llm.enabled", return_value=True),
            patch("modules.llm.provider_label", return_value="DeepSeek(deepseek-chat)"),
            patch("modules.llm._ask_llm", side_effect=fake_ask),
        ):
            idx = asyncio.run(
                llm.pick_option("测试题", ["选项A", "选项B"], answerer=broken)
            )
        self.assertEqual(idx, 0)

    def test_build_models_endpoint_all_protocols(self):
        self.assertEqual(
            llm.build_models_endpoint("https://api.openai.com/v1", "openai"),
            "https://api.openai.com/v1/models",
        )
        self.assertEqual(
            llm.build_models_endpoint("https://api.anthropic.com", "anthropic"),
            "https://api.anthropic.com/v1/models",
        )
        self.assertEqual(
            llm.build_models_endpoint("https://generativelanguage.googleapis.com/v1beta", "gemini"),
            "https://generativelanguage.googleapis.com/v1beta/models",
        )
        self.assertEqual(
            llm.build_models_endpoint("http://127.0.0.1:11434", "ollama"),
            "http://127.0.0.1:11434/api/tags",
        )
        with self.assertRaises(ValueError):
            llm.build_models_endpoint("https://x.openai.azure.com", "azure")

    def test_connection_and_fetch_models_via_requests(self):
        class _Resp:
            def __init__(self, payload, status_code=200):
                self._payload = payload
                self.status_code = status_code
                self.text = str(payload)

            def json(self):
                return self._payload

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise RuntimeError(f"HTTP {self.status_code}")

        cfg = {
            "provider": "deepseek",
            "apiFormat": "openai",
            "apiBase": "https://api.deepseek.com",
            "apiKey": "sk-test-123",
            "model": "deepseek-chat",
        }
        with patch(
            "modules.llm.requests.post",
            return_value=_Resp({"choices": [{"message": {"content": "OK"}}]}),
        ):
            msg = llm.test_connection(cfg)
            self.assertIn("连接成功", msg)
            self.assertIn("OK", msg)

        with patch(
            "modules.llm.requests.get",
            return_value=_Resp({"data": [{"id": "deepseek-reasoner"}, {"id": "deepseek-chat"}]}),
        ):
            models = llm.fetch_models(cfg)
            self.assertEqual(models, ["deepseek-chat", "deepseek-reasoner"])

    def test_vision_config_and_openai_image_payload(self):
        self.assertEqual(llm.normalize_conf({"vision_model": " gpt-4o "})["vision_model"], "gpt-4o")
        self.assertEqual(llm.normalize_conf({"visionModel": "qwen-vl"})["vision_model"], "qwen-vl")
        self.assertEqual(llm.normalize_conf({})["vision_model"], "")

        vision_conf = llm.normalize_conf(
            {
                "provider": "openai",
                "api_format": "openai",
                "api_key": "sk",
                "api_base": "https://api.openai.com/v1",
                "model": "gpt-4o-mini",
                "vision_model": "gpt-4o",
            }
        )
        with patch("modules.llm._conf", return_value=vision_conf):
            self.assertTrue(llm.vision_enabled())
            self.assertIn("gpt-4o", llm.vision_label())

        mock_resp = _mock_response({"choices": [{"message": {"content": "B"}}]})
        with patch("modules.llm.requests.post", return_value=mock_resp) as post:
            reply = llm.chat_vision_sync(b"fake-png", "请作答", conf_override=vision_conf)
        self.assertEqual(reply, "B")
        self.assertEqual(post.call_args[0][0], "https://api.openai.com/v1/chat/completions")
        body = post.call_args[1]["json"]
        self.assertEqual(body["model"], "gpt-4o")
        content = body["messages"][0]["content"]
        self.assertEqual(content[0]["type"], "text")
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_vision_requires_model_and_supports_anthropic_image(self):
        with self.assertRaises(ValueError):
            llm.chat_vision_sync(
                b"x",
                "p",
                conf_override={"api_key": "sk", "api_base": "https://api.anthropic.com"},
            )
        mock_resp = _mock_response({"content": [{"type": "text", "text": "A"}]})
        with patch("modules.llm.requests.post", return_value=mock_resp) as post:
            reply = llm.chat_vision_sync(
                b"img",
                "p",
                "image/jpeg",
                conf_override={
                    "provider": "anthropic",
                    "api_format": "anthropic",
                    "api_key": "sk-ant",
                    "api_base": "https://api.anthropic.com",
                    "model": "claude-sonnet-4-20250514",
                    "vision_model": "claude-sonnet-4-20250514",
                },
            )
        self.assertEqual(reply, "A")
        content = post.call_args[1]["json"]["messages"][0]["content"]
        self.assertEqual(content[0]["type"], "image")
        self.assertEqual(content[0]["source"]["media_type"], "image/jpeg")
        self.assertEqual(content[1]["type"], "text")


def _sse_delta(delta: dict) -> str:
    return "data: " + json.dumps({"choices": [{"delta": delta}]}, ensure_ascii=False)


class _StreamResponse:
    """模拟 requests 的流式响应: 按行产出 SSE, close 后停止继续读取."""

    def __init__(self, lines, delay: float = 0.02):
        self._lines = list(lines)
        self._delay = delay
        self.status_code = 200
        self.headers = {"Content-Type": "text/event-stream"}
        self.text = ""
        self.closed = False
        self.reads = 0

    def iter_lines(self, decode_unicode=False):
        for line in self._lines:
            if self._delay:
                time.sleep(self._delay)
            if self.closed:
                return
            self.reads += 1
            yield line

    def close(self):
        self.closed = True

    def json(self):
        raise ValueError("stream response is not json")


class _JsonResponse:
    """忽略 stream 参数, 直接返回整段 JSON 的兼容接口."""

    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.headers = {"Content-Type": "application/json"}
        self.text = json.dumps(payload, ensure_ascii=False)
        self.closed = False
        self.reads = 0

    def iter_lines(self, decode_unicode=False):
        self.reads += 1
        yield self.text.encode("utf-8")

    def close(self):
        self.closed = True

    def json(self):
        return self._payload


class LlmFailFastTests(unittest.TestCase):
    CONF = {
        "provider": "openai",
        "api_format": "openai",
        "api_key": "sk-test",
        "api_base": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
    }

    def tearDown(self):
        llm.reset_cache()
        llm.set_proxy("")

    def test_401_and_402_fail_fast_without_retry(self):
        for status, hint in ((401, "API Key 无效"), (402, "余额不足")):
            resp = _mock_response({"error": {"message": "denied"}}, status_code=status)
            with patch("modules.llm.requests.post", return_value=resp) as post, patch(
                "modules.llm.time.sleep"
            ):
                with self.assertRaises(llm.NonRetryableLLMError) as ctx:
                    llm.chat_sync("题目", conf_override=self.CONF)
            self.assertEqual(post.call_count, 1)
            self.assertIn(hint, str(ctx.exception))
            self.assertIn(f"HTTP {status}", str(ctx.exception))

    def test_retryable_statuses_are_retried_then_succeed(self):
        good = _mock_response({"choices": [{"message": {"content": "重试成功"}}]})
        for status in (429, 502):
            bad = _mock_response({"error": {"message": "busy"}}, status_code=status)
            with patch(
                "modules.llm.requests.post", side_effect=[bad, good]
            ) as post, patch("modules.llm.time.sleep") as sleep:
                reply = llm.chat_sync("题目", conf_override=self.CONF)
            self.assertEqual(reply, "重试成功")
            self.assertEqual(post.call_count, 2)
            self.assertEqual(sleep.call_count, 1)

    def test_non_retryable_error_is_runtime_error(self):
        self.assertTrue(issubclass(llm.NonRetryableLLMError, RuntimeError))


class OpenAiStreamTests(unittest.TestCase):
    CONF = {
        "provider": "openai",
        "api_format": "openai",
        "api_key": "sk-test",
        "api_base": "https://api.openai.com/v1",
        "model": "deepseek-reasoner",
        "stream_timeout": 2,
        "stream_idle_timeout": 0.8,
    }

    def tearDown(self):
        llm.reset_cache()
        llm.set_proxy("")

    def test_stream_stops_reading_after_answer_block_is_closed(self):
        lines = [
            _sse_delta({"reasoning_content": "先思考一下"}),
            _sse_delta({"content": "依据略。\n```answer\n[{\"id\": 2}]\n```\n"}),
            _sse_delta({"content": "LATE-CONTENT"}),
        ]
        resp = _StreamResponse(lines, delay=0.03)
        started = time.monotonic()
        with patch("modules.llm.requests.post", return_value=resp) as post:
            reply = llm.chat_sync("题目", conf_override=self.CONF, stream=True)
        elapsed = time.monotonic() - started
        self.assertIn("```answer", reply)
        self.assertIn('"id": 2', reply)
        self.assertNotIn("LATE-CONTENT", reply)
        self.assertTrue(resp.closed)
        self.assertLess(elapsed, 1.0)
        self.assertTrue(post.call_args[1]["stream"])
        self.assertTrue(post.call_args[1]["json"]["stream"])

    def test_reasoning_heartbeats_keep_stream_alive_beyond_idle_timeout(self):
        lines = [
            _sse_delta({"reasoning_content": f"思考{i}"}) for i in range(15)
        ] + [_sse_delta({"content": "```answer\n[{\"id\": 1}]\n```"})]
        resp = _StreamResponse(lines, delay=0.04)
        conf = dict(self.CONF, stream_idle_timeout=0.4, stream_timeout=4)
        started = time.monotonic()
        with patch("modules.llm.requests.post", return_value=resp):
            reply = llm.chat_sync("题目", conf_override=conf, stream=True)
        elapsed = time.monotonic() - started
        self.assertIn('"id": 1', reply)
        self.assertGreater(elapsed, conf["stream_idle_timeout"])

    def test_idle_timeout_exits_promptly(self):
        resp = _StreamResponse([_sse_delta({"content": "never"})], delay=1.5)
        conf = dict(self.CONF, stream_idle_timeout=0.15, stream_timeout=3)
        started = time.monotonic()
        with (
            patch("modules.llm_client.DEFAULT_STREAM_RETRIES", 0),
            patch("modules.llm.requests.post", return_value=resp),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                llm.chat_sync("题目", conf_override=conf, stream=True)
        elapsed = time.monotonic() - started
        self.assertIn("未收到新数据", str(ctx.exception))
        self.assertTrue(resp.closed)
        self.assertLess(elapsed, 1.0)

    def test_total_timeout_stops_long_reasoning(self):
        lines = [
            _sse_delta({"reasoning_content": f"思考{i}"}) for i in range(100)
        ]
        resp = _StreamResponse(lines, delay=0.03)
        conf = dict(self.CONF, stream_idle_timeout=0.4, stream_timeout=0.5)
        started = time.monotonic()
        with (
            patch("modules.llm_client.DEFAULT_STREAM_RETRIES", 0),
            patch("modules.llm.requests.post", return_value=resp),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                llm.chat_sync("题目", conf_override=conf, stream=True)
        elapsed = time.monotonic() - started
        self.assertIn("总时长", str(ctx.exception))
        self.assertTrue(resp.closed)
        self.assertLess(elapsed, 1.5)

    def test_arbitrary_empty_content_delta_refreshes_idle_timeout(self):
        # thinking / role / 空 delta 等非 reasoning_content 字段也应发送心跳刷新 idle 超时
        lines = [
            _sse_delta({"thinking": f"步{i}"}) for i in range(12)
        ] + [
            _sse_delta({"role": "assistant", "content": ""}),
            _sse_delta({}),
            _sse_delta({"content": '```answer\n[{"id": 7}]\n```'}),
        ]
        resp = _StreamResponse(lines, delay=0.04)
        conf = dict(self.CONF, stream_idle_timeout=0.25, stream_timeout=4)
        started = time.monotonic()
        with patch("modules.llm.requests.post", return_value=resp):
            reply = llm.chat_sync("题目", conf_override=conf, stream=True)
        elapsed = time.monotonic() - started
        self.assertIn('"id": 7', reply)
        self.assertGreater(elapsed, conf["stream_idle_timeout"])

    def test_stream_retries_on_transient_consume_error_and_timeout(self):
        class _BrokenStreamResponse(_StreamResponse):
            def iter_lines(self, decode_unicode=False):
                yield _sse_delta({"content": "半截"}).encode("utf-8")
                raise requests.ConnectionError("mid-stream disconnect")

        broken = _BrokenStreamResponse([])
        slow = _StreamResponse([_sse_delta({"content": "too-late"})], delay=1.0)
        good = _StreamResponse([_sse_delta({"content": '```answer\n[{"id": 9}]\n```'})], delay=0.01)
        conf = dict(self.CONF, stream_idle_timeout=0.1, stream_timeout=2)
        with (
            patch("modules.llm.requests.post", side_effect=[broken, slow, good]) as post,
            patch("modules.llm_client._retry_delay", return_value=0.0),
        ):
            reply = llm.chat_sync("题目", conf_override=conf, stream=True)
        self.assertIn('"id": 9', reply)
        self.assertEqual(post.call_count, 3)

    def test_stream_does_not_retry_non_retryable_or_stream_not_supported(self):
        # 401 NonRetryableLLMError 立即上抛，不重试
        unauth = _mock_response({"error": {"message": "bad key"}}, status_code=401)
        with (
            patch("modules.llm.requests.post", return_value=unauth) as post,
            patch("modules.llm_client._retry_delay", return_value=0.0),
        ):
            with self.assertRaises(llm.NonRetryableLLMError):
                llm.chat_sync("题目", conf_override=self.CONF, stream=True)
        self.assertEqual(post.call_count, 1)

        # StreamNotSupportedError 立即回退非流式解析，不在流式重试循环里重复请求
        plain = _JsonResponse({"choices": [{"message": {"content": "非流式直接成功"}}]})
        with (
            patch("modules.llm.requests.post", return_value=plain) as post,
            patch("modules.llm_client._retry_delay", return_value=0.0),
        ):
            reply = llm.chat_sync("题目", conf_override=self.CONF, stream=True)
        self.assertEqual(reply, "非流式直接成功")
        self.assertEqual(post.call_count, 1)

    def test_utf8_bytes_sse_with_comments_and_metadata_streams(self):
        lines = [
            b": keep-alive",
            b"event: message",
            b"id: 1",
            _sse_delta({"reasoning_content": "先想中文"}).encode("utf-8"),
            _sse_delta(
                {"content": "```answer\n[{\"id\": 1, \"note\": \"中文\"}]\n```\n"}
            ).encode("utf-8"),
        ]
        resp = _StreamResponse(lines, delay=0.01)
        with patch("modules.llm.requests.post", return_value=resp) as post:
            reply = llm.chat_sync("题目", conf_override=self.CONF, stream=True)
        self.assertIn('"id": 1', reply)
        self.assertIn("中文", reply)
        self.assertNotIn("ï¿½", reply)
        self.assertEqual(post.call_count, 1)
        self.assertTrue(resp.closed)

    def test_non_sse_endpoint_falls_back_to_plain_json(self):
        payload = {"choices": [{"message": {"content": "普通非流式答案"}}]}
        resp = _JsonResponse(payload)
        with patch("modules.llm.requests.post", return_value=resp) as post:
            reply = llm.chat_sync("题目", conf_override=self.CONF, stream=True)
        self.assertEqual(reply, "普通非流式答案")
        self.assertEqual(post.call_count, 1)

    def test_stream_400_falls_back_to_non_stream_without_optional_params(self):
        bad = _mock_response(
            {"error": {"message": "temperature not supported"}}, status_code=400
        )
        good = _mock_response({"choices": [{"message": {"content": "兼容答案"}}]})
        with patch(
            "modules.llm.requests.post", side_effect=[bad, good]
        ) as post, patch("modules.llm.time.sleep"):
            reply = llm.chat_sync(
                "题目", json_mode=True, conf_override=self.CONF, stream=True
            )
        self.assertEqual(reply, "兼容答案")
        self.assertEqual(post.call_count, 2)
        second_body = post.call_args_list[1][1]["json"]
        self.assertNotIn("temperature", second_body)
        self.assertNotIn("response_format", second_body)
        self.assertNotIn("stream", post.call_args_list[1][1])

    def test_stream_timeout_config_is_normalized(self):
        defaults = llm.normalize_conf({})
        self.assertEqual(defaults["stream_timeout"], llm.DEFAULT_STREAM_TIMEOUT)
        self.assertEqual(
            defaults["stream_idle_timeout"], llm.DEFAULT_STREAM_IDLE_TIMEOUT
        )
        clamped = llm.normalize_conf(
            {"stream_timeout": 10, "stream_idle_timeout": 30}
        )
        self.assertEqual(clamped["stream_timeout"], 10)
        self.assertEqual(clamped["stream_idle_timeout"], 10)
        aliased = llm.normalize_conf(
            {"streamTimeout": 90, "streamIdleTimeout": 15}
        )
        self.assertEqual(aliased["stream_timeout"], 90)
        self.assertEqual(aliased["stream_idle_timeout"], 15)

    def test_think_aware_stream_ignores_aim_block_inside_think_and_strips_unclosed_think(self):
        # <think> tag split across chunks AND contains a decoy ```answer``` block
        lines = [
            _sse_delta({"content": "<thi"}).encode("utf-8"),
            _sse_delta({"content": 'nk>先假设 ```answer\n["A"]\n``` 但好像不对</thi'}).encode("utf-8"),
            _sse_delta({"content": 'nk>最终答案：\n```answer\n["B"]\n```'}).encode("utf-8"),
            _sse_delta({"content": "\n多余解释不应再被读取"}).encode("utf-8"),
        ]
        resp = _StreamResponse(lines, delay=0.01)
        with patch("modules.llm.requests.post", return_value=resp):
            reply = llm.chat_sync("题目", conf_override=self.CONF, stream=True)
        self.assertIn('["B"]', reply)
        self.assertNotIn('["A"]', reply)
        self.assertNotIn("多余解释", reply)
        self.assertTrue(resp.closed)

        # Unclosed truncated <think> is also stripped cleanly
        self.assertEqual(
            llm._strip_think_tags("前缀答案<think>未闭合的思考内容"),
            "前缀答案",
        )

    def test_http_session_pool_reused_per_thread(self):
        import threading

        s1 = llm._get_http_session()
        s2 = llm._get_http_session()
        self.assertIsNotNone(s1)
        self.assertIs(s1, s2)

        other = []

        def worker():
            other.append(llm._get_http_session())
            llm.close_http_session()

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=3)
        self.assertEqual(len(other), 1)
        self.assertIsNot(s1, other[0])

    def test_to_indexes_bounds_and_short_option_safety_and_raw_decode_quiz_json(self):
        # list[int] bounds check + dedup + reject bool
        self.assertEqual(llm.to_indexes([-1, 0, 2, 2, 99], ["甲", "乙", "丙"]), [0, 2])
        self.assertEqual(llm.to_indexes([True], ["对", "错"]), [])

        # Avoid single-char option false substring match
        self.assertEqual(llm.to_indexes("答案是错误", ["错", "错误"]), [1])
        self.assertEqual(llm.to_indexes("包含一的句子", ["一", "完全无关选项"]), [])

        # raw_decode handles surrounding braces before and after JSON
        messy = (
            "说明 {这里有花括号干扰} 如下：\n"
            '{"questions": [{"index": 1, "question": "1+1=?", "options": ["1", "2"], '
            '"type": "single", "answer": [1]}]}\n'
            "结尾补充 {另一个花括号}"
        )
        qs = llm._parse_quiz_questions(messy)
        self.assertEqual(len(qs), 1)
        self.assertEqual(qs[0]["question"], "1+1=?")

    def test_close_all_http_sessions_and_stateless_cookies(self):
        llm.close_all_http_sessions()
        s1 = llm._get_http_session()
        self.assertIsNotNone(s1)
        # Stateless cookie policy refuses storing cookies across providers/keys
        self.assertFalse(s1.cookies._policy.set_ok(None, None))
        self.assertFalse(s1.cookies._policy.rfc2965)
        self.assertFalse(s1.cookies._policy.netscape)
        self.assertTrue(s1.cookies._policy.hide_cookie2)
        llm.close_all_http_sessions()
        s2 = llm._get_http_session()
        self.assertIsNot(s1, s2)
        llm.close_all_http_sessions()

    def test_fetch_models_real_session_ignores_set_cookie_without_rfc2965_error(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer
        import os
        import threading

        seen_cookie_headers: list[tuple[str | None, str | None]] = []

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen_cookie_headers.append(
                    (self.headers.get("Cookie"), self.headers.get("Cookie2"))
                )
                body = json.dumps(
                    {"data": [{"id": "model-b"}, {"id": "model-a"}]}
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Set-Cookie", "sid=abc123; Path=/")
                self.send_header("Set-Cookie2", 'v="1"; Version="1"; Path="/"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), _Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        llm.close_all_http_sessions()
        try:
            cfg = {
                "provider": "openai",
                "apiFormat": "openai",
                "apiBase": f"http://127.0.0.1:{port}/v1",
                "apiKey": "sk-test",
            }
            with patch.dict(
                os.environ,
                {"NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"},
                clear=False,
            ):
                models_1 = llm.fetch_models(cfg)
                models_2 = llm.fetch_models(cfg)
            self.assertEqual(models_1, ["model-a", "model-b"])
            self.assertEqual(models_2, ["model-a", "model-b"])
            sess = llm._get_http_session()
            self.assertEqual(len(sess.cookies), 0)
            self.assertEqual(seen_cookie_headers, [(None, None), (None, None)])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
            llm.close_all_http_sessions()


if __name__ == "__main__":
    unittest.main()
