# -*- coding: utf-8 -*-
"""通用大模型 HTTP / SSE 协议客户端（支持连接池复用、流式提前截断与多协议适配）。"""
from __future__ import annotations

import base64
from http.cookiejar import DefaultCookiePolicy
import json
import queue
import re
import threading
import time
from urllib.parse import urlparse

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

from modules.http_utils import build_requests_proxies
from modules.llm_config import (
    DEFAULT_ANTHROPIC_VERSION,
    DEFAULT_STREAM_IDLE_TIMEOUT,
    DEFAULT_STREAM_RETRIES,
    DEFAULT_STREAM_TIMEOUT,
    NON_RETRYABLE_STATUS,
    SUPPORTED_API_FORMATS,
    NonRetryableLLMError,
    StreamNotSupportedError,
    _LOCAL_HOSTS,
    _STREAM_QUEUE_SIZE,
    _conf,
    _http_error_message,
    _is_retryable_status,
    _positive_float,
    _resolve_facade,
    _retry_delay,
    build_endpoint,
    build_models_endpoint,
    get_runtime_proxy,
    infer_api_format,
    is_local_endpoint,
    normalize_conf,
)
from modules.logger import Logger
from modules.text_utils import strip_think_tags as _strip_think_tags

logger = Logger()

_HTTP_LOCAL = threading.local()
_HTTP_SESSIONS_LOCK = threading.Lock()
_HTTP_SESSIONS: set = set()
_HTTP_SESSION_EPOCH = 0
_ORIGINAL_REQUESTS_POST = getattr(requests, "post", None) if requests is not None else None
_ORIGINAL_REQUESTS_GET = getattr(requests, "get", None) if requests is not None else None


class _StatelessCookiePolicy(DefaultCookiePolicy):
    """禁止 requests.Session 持久化 Set-Cookie，防止跨厂商/API Key 状态串号。"""

    def __init__(self) -> None:
        super().__init__(
            netscape=False,
            rfc2965=False,
            hide_cookie2=True,
        )

    def set_ok(self, cookie, request):
        return False

    def return_ok(self, cookie, request):
        return False

    def domain_return_ok(self, domain, request):
        return False

    def path_return_ok(self, path, request):
        return False


def _get_http_session():
    """返回当前线程复用的无状态 requests.Session 连接池（keep-alive 复用且支持集中关闭）。"""
    if requests is None:
        return None
    sess = getattr(_HTTP_LOCAL, "session", None)
    local_epoch = getattr(_HTTP_LOCAL, "epoch", -1)
    with _HTTP_SESSIONS_LOCK:
        cur_epoch = _HTTP_SESSION_EPOCH
        if sess is None or local_epoch != cur_epoch:
            sess = requests.Session()
            try:
                sess.cookies.set_policy(_StatelessCookiePolicy())
            except Exception:
                pass
            _HTTP_LOCAL.session = sess
            _HTTP_LOCAL.epoch = cur_epoch
            _HTTP_SESSIONS.add(sess)
    return sess


def close_http_session() -> None:
    """关闭并清理当前线程缓存的 HTTP Session。"""
    sess = getattr(_HTTP_LOCAL, "session", None)
    if sess is not None:
        _HTTP_LOCAL.session = None
        _HTTP_LOCAL.epoch = -1
        with _HTTP_SESSIONS_LOCK:
            _HTTP_SESSIONS.discard(sess)
        try:
            sess.close()
        except Exception:
            pass


def close_all_http_sessions() -> None:
    """关闭所有线程创建的 LLM HTTP Session 并递增代际，防止代理切换或退出后句柄残留。"""
    global _HTTP_SESSION_EPOCH
    with _HTTP_SESSIONS_LOCK:
        _HTTP_SESSION_EPOCH += 1
        sessions = list(_HTTP_SESSIONS)
        _HTTP_SESSIONS.clear()
    _HTTP_LOCAL.session = None
    _HTTP_LOCAL.epoch = -1
    for sess in sessions:
        try:
            sess.close()
        except Exception:
            pass


def _http_post(url: str, **kwargs):
    if requests is not None and getattr(requests, "post", None) is not _ORIGINAL_REQUESTS_POST:
        return requests.post(url, **kwargs)
    sess = _get_http_session()
    if sess is not None:
        return sess.post(url, **kwargs)
    return requests.post(url, **kwargs)


def _http_get(url: str, **kwargs):
    if requests is not None and getattr(requests, "get", None) is not _ORIGINAL_REQUESTS_GET:
        return requests.get(url, **kwargs)
    sess = _get_http_session()
    if sess is not None:
        return sess.get(url, **kwargs)
    return requests.get(url, **kwargs)


def _resolve_proxies(url: str, proxy: str | None = None) -> dict | None:
    eff = get_runtime_proxy() if proxy is None else str(proxy or "").strip()
    if not eff:
        return None
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        host = ""
    if host in _LOCAL_HOSTS:
        return None
    try:
        return build_requests_proxies(eff) or None
    except ValueError:
        return None


def _extract_error_detail(resp) -> str:
    try:
        data = resp.json()
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict):
                msg = err.get("message") or err.get("msg") or err.get("code")
                if msg:
                    return str(msg)[:240]
            elif isinstance(err, str) and err:
                return err[:240]
            for key in ("message", "msg", "detail"):
                if data.get(key):
                    return str(data[key])[:240]
    except Exception:
        pass
    text = getattr(resp, "text", "") or ""
    return text.strip()[:200]


def _extract_openai_content(data: dict) -> str:
    choices = data.get("choices") or []
    if not choices:
        raise ValueError("模型响应缺少 choices 字段")
    msg = choices[0].get("message") or {}
    content = msg.get("content")
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("text"):
                parts.append(str(item["text"]))
            elif isinstance(item, str):
                parts.append(item)
        content = "\n".join(parts)
    if content is None:
        content = choices[0].get("text") or ""
    return _strip_think_tags(str(content))


def _extract_openai_responses_content(data: dict) -> str:
    if isinstance(data.get("output_text"), str) and data["output_text"].strip():
        return _strip_think_tags(data["output_text"])
    texts = []
    for item in data.get("output") or []:
        if not isinstance(item, dict):
            continue
        for block in item.get("content") or []:
            if isinstance(block, dict) and block.get("text"):
                texts.append(str(block["text"]))
    if texts:
        return _strip_think_tags("\n".join(texts))
    if "choices" in data:
        return _extract_openai_content(data)
    raise ValueError("Responses API 响应缺少有效文本内容")


def _extract_anthropic_content(data: dict) -> str:
    blocks = data.get("content")
    if isinstance(blocks, list):
        texts = [
            str(b.get("text") or "")
            for b in blocks
            if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
        ]
        if texts:
            return _strip_think_tags("\n".join(texts))
    if isinstance(data.get("completion"), str):
        return _strip_think_tags(data["completion"])
    raise ValueError("Anthropic 响应缺少有效文本内容")


def _extract_gemini_content(data: dict) -> str:
    candidates = data.get("candidates") or []
    if not candidates:
        raise ValueError("Gemini 响应缺少 candidates 字段")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    texts = [
        str(p.get("text") or "")
        for p in parts
        if isinstance(p, dict) and p.get("text") and not p.get("thought")
    ]
    if not texts:
        texts = [
            str(p.get("text") or "")
            for p in parts
            if isinstance(p, dict) and p.get("text")
        ]
    if not texts:
        raise ValueError("Gemini 响应缺少有效文本内容")
    return _strip_think_tags("\n".join(texts))


def _extract_ollama_content(data: dict) -> str:
    msg = data.get("message")
    if isinstance(msg, dict) and msg.get("content") is not None:
        return _strip_think_tags(str(msg["content"]))
    if data.get("response") is not None:
        return _strip_think_tags(str(data["response"]))
    if "choices" in data:
        return _extract_openai_content(data)
    raise ValueError("Ollama 响应缺少 message.content 字段")


def _post_with_fallback(
    url: str,
    headers: dict,
    body: dict,
    fallback_body: dict | None,
    proxies: dict | None,
    timeout: int = 120,
    retries: int = DEFAULT_STREAM_RETRIES,
):
    """发送请求; 400 先剥离可选参数重试, 408/429/5xx 退避重试, 401/402 等快速失败."""
    current_body = body
    fallback_used = False
    attempt = 0
    while True:
        kwargs = {"headers": headers, "json": current_body, "timeout": timeout}
        if proxies:
            kwargs["proxies"] = proxies
        try:
            resp = _http_post(url, **kwargs)
        except Exception as exc:
            if attempt >= retries:
                raise RuntimeError(f"请求失败: {exc}") from exc
            time.sleep(_retry_delay(attempt))
            attempt += 1
            continue

        status = getattr(resp, "status_code", 200)
        if (
            status == 400
            and fallback_body is not None
            and fallback_body != current_body
            and not fallback_used
        ):
            logger.debug(f"模型接口返回 400, 去掉可选参数后重试: {url}")
            fallback_used = True
            current_body = fallback_body
            continue

        if status >= 400:
            detail = _extract_error_detail(resp)
            message = _http_error_message(status, detail)
            if status in NON_RETRYABLE_STATUS:
                raise NonRetryableLLMError(message, status=status)
            if _is_retryable_status(status) and attempt < retries:
                logger.debug(f"模型接口返回 {status}, 第 {attempt + 1} 次退避重试: {url}")
                time.sleep(_retry_delay(attempt))
                attempt += 1
                continue
            raise RuntimeError(message)
        return resp.json()


def _close_response(resp) -> None:
    close = getattr(resp, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _sse_text(raw) -> str:
    if isinstance(raw, bytes):
        try:
            return raw.decode("utf-8", "replace")
        except Exception:
            return ""
    return str(raw or "")


def _has_aim_block(text: str, aim_start: str, aim_end: str) -> bool:
    if not aim_start or not aim_end:
        return False
    visible = _strip_think_tags(text)
    start_idx = visible.find(aim_start)
    if start_idx < 0:
        return False
    return visible.find(aim_end, start_idx + len(aim_start)) >= 0


_THINK_OPEN_RE = re.compile(r"<think\b[^>]*>", re.IGNORECASE)
_THINK_CLOSE_RE = re.compile(r"</think>", re.IGNORECASE)


def _trailing_tag_prefix_len(text: str, *, closing: bool) -> int:
    """返回 text 尾部可能构成不完整 <think...> 或 </think> 标签的最大保留长度。"""
    lt = text.rfind("<")
    if lt < 0:
        return 0
    tail = text[lt:]
    if ">" in tail:
        return 0
    lower = tail.lower()
    if closing:
        return len(tail) if "</think>".startswith(lower) else 0
    if "<think".startswith(lower):
        return len(tail)
    if lower.startswith("<think") and (len(lower) == 6 or lower[6].isspace()):
        return len(tail) if len(tail) <= 64 else 0
    return 0


class _ThinkAwareAimTracker:
    """增量追踪 <think>...</think> 状态，仅对思考块之外的正文以 O(N) 尾窗匹配目标块。"""

    def __init__(self, aim_start: str, aim_end: str):
        self._aim_start = aim_start or ""
        self._aim_end = aim_end or ""
        self._enabled = bool(self._aim_start and self._aim_end)
        self._in_think = False
        self._tag_carry = ""
        self._aim_started = False
        self._aim_tail = ""
        self._matched = False

    def _feed_visible(self, text: str) -> bool:
        if not text or not self._enabled or self._matched:
            return self._matched
        if not self._aim_started:
            probe = self._aim_tail + text
            idx = probe.find(self._aim_start)
            if idx >= 0:
                self._aim_started = True
                after = probe[idx + len(self._aim_start) :]
                if after.find(self._aim_end) >= 0:
                    self._matched = True
                    return True
                keep = max(len(self._aim_end) - 1, 0)
                self._aim_tail = after[-keep:] if keep else ""
            else:
                keep = max(len(self._aim_start) - 1, 0)
                self._aim_tail = probe[-keep:] if keep else ""
            return False
        probe = self._aim_tail + text
        if probe.find(self._aim_end) >= 0:
            self._matched = True
            return True
        keep = max(len(self._aim_end) - 1, 0)
        self._aim_tail = probe[-keep:] if keep else ""
        return False

    def feed(self, chunk: str) -> bool:
        if not self._enabled or not chunk or self._matched:
            return self._matched
        buf = self._tag_carry + chunk
        self._tag_carry = ""
        while buf:
            if not self._in_think:
                m = _THINK_OPEN_RE.search(buf)
                if m:
                    if m.start() > 0 and self._feed_visible(buf[: m.start()]):
                        return True
                    self._in_think = True
                    buf = buf[m.end() :]
                    continue
                keep = _trailing_tag_prefix_len(buf, closing=False)
                if keep:
                    visible_part = buf[:-keep]
                    self._tag_carry = buf[-keep:]
                else:
                    visible_part = buf
                if visible_part and self._feed_visible(visible_part):
                    return True
                break
            else:
                m = _THINK_CLOSE_RE.search(buf)
                if m:
                    self._in_think = False
                    buf = buf[m.end() :]
                    continue
                keep = _trailing_tag_prefix_len(buf, closing=True)
                self._tag_carry = buf[-keep:] if keep else ""
                break
        return self._matched


def _consume_openai_stream(
    resp,
    *,
    aim_start: str,
    aim_end: str,
    total_timeout: float,
    idle_timeout: float,
) -> str:
    """后台线程读取 SSE, 主线程双超时等待; 仅在 <think> 之外命中目标代码块后立即断开连接."""
    deadline = time.monotonic() + max(float(total_timeout), 0.001)
    stop_event = threading.Event()
    close_lock = threading.Lock()
    response_closed = False
    chunks: queue.Queue = queue.Queue(maxsize=_STREAM_QUEUE_SIZE)

    def _close_once() -> None:
        nonlocal response_closed
        with close_lock:
            if response_closed:
                return
            response_closed = True
        _close_response(resp)

    def _emit(item) -> bool:
        while not stop_event.is_set():
            try:
                chunks.put(item, timeout=0.2)
                return True
            except queue.Full:
                continue
        return False

    def _reader() -> None:
        saw_data = False
        body_lines: list[str] = []
        try:
            # decode_unicode=False 避免 requests 缺省按 ISO-8859-1 解码中文乱码.
            iterator = iter(resp.iter_lines(decode_unicode=False))
            while not stop_event.is_set():
                try:
                    raw_line = next(iterator)
                except StopIteration:
                    break
                line = _sse_text(raw_line).strip()
                if not line:
                    continue
                if line.startswith(":"):
                    # SSE 注释行(如 ": keep-alive")是合法心跳, 刷新 idle 超时.
                    _emit(("heartbeat", None))
                    continue
                if not line.startswith("data:"):
                    if not line.startswith(("event:", "id:", "retry:")):
                        body_lines.append(line)
                    continue
                saw_data = True
                payload = line[5:].strip()
                if payload == "[DONE]":
                    _emit(("done", None))
                    return
                try:
                    data = json.loads(payload)
                except (ValueError, TypeError):
                    continue
                choices = data.get("choices") if isinstance(data, dict) else None
                if not choices:
                    continue
                choice = choices[0] if isinstance(choices[0], dict) else {}
                delta = choice.get("delta") or {}
                content = delta.get("content")
                reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                if content:
                    _emit(("chunk", str(content)))
                elif reasoning:
                    # 推理模型思考期间 content 为空, 用空心跳刷新 idle 超时.
                    _emit(("heartbeat", None))
                if choice.get("finish_reason"):
                    _emit(("done", None))
                    return
            if body_lines and not saw_data and not stop_event.is_set():
                # 整个响应没有任何合法 data: 行, 正文可能是普通 JSON.
                _emit(("unsupported", "\n".join(body_lines)))
            elif not stop_event.is_set():
                _emit(("done", None))
        except Exception as exc:
            if not stop_event.is_set():
                _emit(("error", exc))
        finally:
            _close_once()

    reader = threading.Thread(target=_reader, daemon=True, name="llm-sse-reader")
    reader.start()

    parts: list[str] = []
    tracker = _ThinkAwareAimTracker(aim_start, aim_end)
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                stop_event.set()
                _close_once()
                raise RuntimeError(
                    f"流式响应超时: 总时长超过 {float(total_timeout):g} 秒"
                )
            try:
                kind, payload = chunks.get(timeout=min(remaining, idle_timeout))
            except queue.Empty:
                stop_event.set()
                _close_once()
                # 剩余总时长小于 idle 时, 等待期间可能刚好越过总超时线.
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"流式响应超时: 总时长超过 {float(total_timeout):g} 秒"
                    )
                raise RuntimeError(
                    f"流式响应超时: {float(idle_timeout):g} 秒内未收到新数据"
                )
            if kind == "heartbeat":
                continue
            if kind == "chunk":
                parts.append(payload)
                if tracker.feed(payload):
                    stop_event.set()
                    return _strip_think_tags("".join(parts))
                continue
            if kind == "done":
                break
            if kind == "unsupported":
                stop_event.set()
                raise StreamNotSupportedError(payload)
            if kind == "error":
                stop_event.set()
                raise RuntimeError(f"流式读取失败: {payload}")
    finally:
        stop_event.set()
        # 优先给 reader 极短窗口在自身线程优雅关闭连接，避免跨线程同时操作底层 socket；
        # 若 reader 仍阻塞在 recv() 上则由主线程兜底强关（_close_once 幂等）。
        reader.join(timeout=0.05)
        _close_once()

    return _strip_think_tags("".join(parts))


def _chat_openai_stream(
    endpoint: str,
    headers: dict,
    body: dict,
    proxies: dict | None,
    *,
    aim_start: str = "```answer",
    aim_end: str = "```",
    total_timeout: float = DEFAULT_STREAM_TIMEOUT,
    idle_timeout: float = DEFAULT_STREAM_IDLE_TIMEOUT,
    retries: int = DEFAULT_STREAM_RETRIES,
) -> str:
    """OpenAI 兼容接口的流式作答; 双超时 + 命中目标代码块立即断开."""
    stream_body = dict(body)
    stream_body["stream"] = True
    total_timeout = _positive_float(total_timeout, DEFAULT_STREAM_TIMEOUT)
    idle_timeout = _positive_float(idle_timeout, DEFAULT_STREAM_IDLE_TIMEOUT)
    if idle_timeout > total_timeout:
        idle_timeout = total_timeout
    socket_read_timeout = max(0.5, min(idle_timeout + 2.0, total_timeout + 2.0))

    attempt = 0
    while True:
        kwargs = {
            "headers": headers,
            "json": stream_body,
            "timeout": (10, socket_read_timeout),
            "stream": True,
        }
        if proxies:
            kwargs["proxies"] = proxies
        try:
            resp = _http_post(endpoint, **kwargs)
        except Exception as exc:
            if attempt >= retries:
                raise RuntimeError(f"流式请求失败: {exc}") from exc
            time.sleep(_retry_delay(attempt))
            attempt += 1
            continue

        status = getattr(resp, "status_code", 200)
        if status >= 400:
            detail = _extract_error_detail(resp)
            _close_response(resp)
            message = _http_error_message(status, detail)
            if status in NON_RETRYABLE_STATUS:
                raise NonRetryableLLMError(message, status=status)
            if _is_retryable_status(status) and attempt < retries:
                logger.debug(f"流式接口返回 {status}, 第 {attempt + 1} 次退避重试: {endpoint}")
                time.sleep(_retry_delay(attempt))
                attempt += 1
                continue
            raise RuntimeError(message)

        return _consume_openai_stream(
            resp,
            aim_start=aim_start,
            aim_end=aim_end,
            total_timeout=total_timeout,
            idle_timeout=idle_timeout,
        )


def _chat_sync(
    api: str,
    api_key: str,
    model: str,
    prompt: str,
    json_mode: bool = False,
    *,
    api_format: str = "openai",
    api_version: str = "",
    proxy: str | None = None,
    image: tuple[bytes, str] | None = None,
    stream: bool = False,
    stream_timeout: float = DEFAULT_STREAM_TIMEOUT,
    stream_idle_timeout: float = DEFAULT_STREAM_IDLE_TIMEOUT,
    max_tokens: int | None = None,
    timeout: int = 120,
) -> str:
    fmt = (api_format or "openai").strip().lower()
    proxies = _resolve_proxies(api, proxy)
    headers = {"Content-Type": "application/json"}

    image_b64 = ""
    image_mime = "image/png"
    if image:
        raw_bytes, raw_mime = image
        if raw_bytes:
            image_b64 = base64.b64encode(raw_bytes).decode("ascii")
            image_mime = (raw_mime or "image/png").strip() or "image/png"
    data_url = f"data:{image_mime};base64,{image_b64}" if image_b64 else ""

    if fmt == "anthropic":
        if api_key:
            headers["x-api-key"] = api_key
        headers["anthropic-version"] = (api_version or "").strip() or DEFAULT_ANTHROPIC_VERSION
        content = prompt
        if image_b64:
            content = [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": image_mime, "data": image_b64},
                },
                {"type": "text", "text": prompt},
            ]
        base_body = {
            "model": model,
            "max_tokens": max_tokens or 4096,
            "messages": [{"role": "user", "content": content}],
        }
        body = {**base_body, "temperature": 0}
        data = _post_with_fallback(api, headers, body, base_body, proxies, timeout=timeout)
        return _extract_anthropic_content(data)

    if fmt == "gemini":
        if api_key:
            headers["x-goog-api-key"] = api_key
        parts = [{"text": prompt}]
        if image_b64:
            parts.append({"inline_data": {"mime_type": image_mime, "data": image_b64}})
        base_body = {"contents": [{"role": "user", "parts": parts}]}
        gen_cfg = {"temperature": 0}
        if max_tokens:
            gen_cfg["maxOutputTokens"] = max_tokens
        if json_mode:
            gen_cfg["responseMimeType"] = "application/json"
        body = {**base_body, "generationConfig": gen_cfg}
        data = _post_with_fallback(api, headers, body, base_body, proxies, timeout=timeout)
        return _extract_gemini_content(data)

    if fmt == "ollama":
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        message = {"role": "user", "content": prompt}
        if image_b64:
            message["images"] = [image_b64]
        base_body = {
            "model": model,
            "messages": [message],
            "stream": False,
        }
        body = {**base_body, "options": {"temperature": 0}}
        if json_mode:
            body["format"] = "json"
        data = _post_with_fallback(api, headers, body, base_body, proxies, timeout=timeout)
        return _extract_ollama_content(data)

    if fmt == "openai-responses":
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        if image_b64:
            base_body = {
                "model": model,
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": prompt},
                            {"type": "input_image", "image_url": data_url},
                        ],
                    }
                ],
            }
        else:
            base_body = {"model": model, "input": prompt}
        body = dict(base_body)
        if json_mode:
            body["text"] = {"format": {"type": "json_object"}}
        data = _post_with_fallback(api, headers, body, base_body, proxies, timeout=timeout)
        return _extract_openai_responses_content(data)

    # openai / azure
    if fmt == "azure":
        if api_key:
            headers["api-key"] = api_key
    elif api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    content = prompt
    if image_b64:
        content = [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]
    base_body = {"model": model, "messages": [{"role": "user", "content": content}]}
    body = {**base_body, "temperature": 0}
    if max_tokens:
        body["max_tokens"] = max_tokens
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    if stream and not image_b64:
        try:
            return _chat_openai_stream(
                api,
                headers,
                body,
                proxies,
                total_timeout=stream_timeout,
                idle_timeout=stream_idle_timeout,
            )
        except StreamNotSupportedError as exc:
            if exc.payload:
                try:
                    return _extract_openai_content(json.loads(exc.payload))
                except Exception:
                    pass
            logger.debug("当前接口未返回 SSE 流, 已回退非流式请求.")
        except NonRetryableLLMError as exc:
            if exc.status != 400:
                raise
            logger.debug("流式接口返回 400, 回退非流式并剥离可选参数重试.")
            data = _post_with_fallback(
                api, headers, base_body, base_body, proxies, timeout=timeout
            )
            return _extract_openai_content(data)
    data = _post_with_fallback(api, headers, body, base_body, proxies, timeout=timeout)
    return _extract_openai_content(data)


def test_connection(conf_override: dict | None, proxy: str = "") -> str:
    """测试大模型连通性（复用 _chat_sync 协议适配器），返回耗时与模型简短回复。"""
    if requests is None:
        raise RuntimeError("当前运行环境缺少 requests 依赖")
    raw = conf_override if isinstance(conf_override, dict) else {}
    api_base = str(raw.get("api_base") or raw.get("apiBase") or "").strip()
    model = str(raw.get("model") or "").strip()
    api_key = str(raw.get("api_key") or raw.get("apiKey") or "").strip()
    provider = str(raw.get("provider") or "").strip().lower()
    raw_fmt = str(raw.get("api_format") or raw.get("apiFormat") or "").strip().lower()
    api_version = str(raw.get("api_version") or raw.get("apiVersion") or "").strip()

    if not api_base or not (api_base.startswith("http://") or api_base.startswith("https://")):
        raise ValueError("API Base URL 格式无效，请以 http:// 或 https:// 开头")
    if not model:
        raise ValueError("请先填写模型名称")
    fmt = raw_fmt if raw_fmt in SUPPORTED_API_FORMATS else infer_api_format(api_base, provider)
    if not api_key and not is_local_endpoint(api_base, provider, fmt):
        raise ValueError("请先填写 API Key（本地 Ollama / LM Studio 可留空）")

    endpoint = build_endpoint(api_base, model, fmt, api_version)
    chat_fn = _resolve_facade("_chat_sync", _chat_sync)
    started = time.monotonic()
    try:
        reply = chat_fn(
            endpoint,
            api_key,
            model,
            "请只回复 OK",
            False,
            api_format=fmt,
            api_version=api_version,
            proxy=proxy,
            max_tokens=32,
            timeout=30,
        ).strip()
    except json.JSONDecodeError as exc:
        raise RuntimeError("接口返回了非 JSON 响应，请检查 API Base URL 与协议格式") from exc
    except ValueError as exc:
        msg = str(exc)
        if "响应缺少" in msg:
            raise RuntimeError("连接已通，但未解析出模型文本内容，请确认接口协议是否正确") from exc
        raise RuntimeError("接口返回了非 JSON 响应，请检查 API Base URL 与协议格式") from exc
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"请求失败: {exc}") from exc

    elapsed_ms = int((time.monotonic() - started) * 1000)
    if not reply:
        raise RuntimeError("连接已通，但未解析出模型文本内容，请确认接口协议是否正确")
    return f"连接成功（{elapsed_ms}ms），模型回复：{reply[:40]}"


def fetch_models(conf_override: dict | None, proxy: str = "") -> list[str]:
    """从大模型服务商拉取可用模型列表。"""
    if requests is None:
        raise RuntimeError("当前运行环境缺少 requests 依赖")
    raw = conf_override if isinstance(conf_override, dict) else {}
    api_base = str(raw.get("api_base") or raw.get("apiBase") or "").strip()
    api_key = str(raw.get("api_key") or raw.get("apiKey") or "").strip()
    provider = str(raw.get("provider") or "").strip().lower()
    raw_fmt = str(raw.get("api_format") or raw.get("apiFormat") or "").strip().lower()
    api_version = str(raw.get("api_version") or raw.get("apiVersion") or "").strip()

    if not api_base or not (api_base.startswith("http://") or api_base.startswith("https://")):
        raise ValueError("API Base URL 格式无效，请以 http:// 或 https:// 开头")
    fmt = raw_fmt if raw_fmt in SUPPORTED_API_FORMATS else infer_api_format(api_base, provider)
    if not api_key and not is_local_endpoint(api_base, provider, fmt):
        raise ValueError("请先填写 API Key 后再拉取模型列表")

    endpoint = build_models_endpoint(api_base, fmt)
    proxies = _resolve_proxies(endpoint, proxy)
    headers = {}
    if fmt == "anthropic":
        if api_key:
            headers["x-api-key"] = api_key
        headers["anthropic-version"] = api_version or DEFAULT_ANTHROPIC_VERSION
    elif fmt == "gemini":
        if api_key:
            headers["x-goog-api-key"] = api_key
    else:
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

    kwargs = {"headers": headers, "timeout": 20}
    if proxies:
        kwargs["proxies"] = proxies
    try:
        resp = _http_get(endpoint, **kwargs)
    except Exception as exc:
        raise RuntimeError(f"拉取模型列表失败: {exc}") from exc

    if getattr(resp, "status_code", 200) >= 400:
        detail = _extract_error_detail(resp)
        raise RuntimeError(f"HTTP {resp.status_code}：{detail}" if detail else f"HTTP {resp.status_code}")

    try:
        parsed = resp.json()
    except Exception as exc:
        raise RuntimeError("模型列表响应不是有效 JSON") from exc

    models: set[str] = set()
    if isinstance(parsed, dict):
        if fmt == "ollama":
            for item in parsed.get("models") or []:
                if isinstance(item, dict) and isinstance(item.get("name"), str):
                    cleaned = item["name"].strip()
                    if cleaned:
                        models.add(cleaned)
        elif fmt == "gemini":
            for item in parsed.get("models") or []:
                if not isinstance(item, dict):
                    continue
                methods = item.get("supportedGenerationMethods")
                if isinstance(methods, list) and "generateContent" not in methods:
                    continue
                name = item.get("name")
                if isinstance(name, str):
                    cleaned = name.strip()
                    if cleaned.startswith("models/"):
                        cleaned = cleaned[len("models/"):]
                    if cleaned:
                        models.add(cleaned)
        else:
            arr = parsed.get("data")
            if not isinstance(arr, list):
                arr = parsed.get("models")
            for item in arr or []:
                if not isinstance(item, dict):
                    continue
                mid = item.get("id") or item.get("name")
                if isinstance(mid, str) and mid.strip():
                    models.add(mid.strip())

    if not models:
        raise RuntimeError("接口未返回可用模型列表，请手动输入模型名称")
    return sorted(models)


def chat_vision_sync(
    image_bytes: bytes,
    prompt: str,
    mime: str = "image/png",
    conf_override: dict | None = None,
    proxy: str | None = None,
) -> str:
    """用配置的视觉模型识别图片并回答 prompt（复用主模型的接口与鉴权）。"""
    if requests is None:
        raise RuntimeError("当前运行环境缺少 requests 依赖")
    if not image_bytes:
        raise ValueError("图片内容为空")
    c = (
        normalize_conf(conf_override)
        if conf_override is not None
        else _resolve_facade("_conf", _conf)()
    )
    model = c.get("vision_model") or ""
    if not model:
        raise ValueError("未配置视觉模型（data/llm.json 的 vision_model）")
    fmt = c.get("api_format") or infer_api_format(c.get("api_base", ""), c.get("provider", ""))
    api = build_endpoint(c["api_base"], model, fmt, c.get("api_version", ""))
    chat_fn = _resolve_facade("_chat_sync", _chat_sync)
    return chat_fn(
        api,
        c["api_key"],
        model,
        prompt,
        False,
        api_format=fmt,
        api_version=c.get("api_version", ""),
        proxy=proxy,
        image=(image_bytes, mime),
    )


def chat_sync(
    prompt: str,
    json_mode: bool = False,
    conf_override: dict | None = None,
    proxy: str | None = None,
    stream: bool = False,
) -> str:
    """使用当前（或指定）LLM 配置发起一次同步对话，自动适配协议格式。"""
    c = (
        normalize_conf(conf_override)
        if conf_override is not None
        else _resolve_facade("_conf", _conf)()
    )
    fmt = c.get("api_format") or infer_api_format(c.get("api_base", ""), c.get("provider", ""))
    api = build_endpoint(c["api_base"], c["model"], fmt, c.get("api_version", ""))
    chat_fn = _resolve_facade("_chat_sync", _chat_sync)
    return chat_fn(
        api,
        c["api_key"],
        c["model"],
        prompt,
        json_mode,
        api_format=fmt,
        api_version=c.get("api_version", ""),
        proxy=proxy,
        stream=stream,
        stream_timeout=c.get("stream_timeout", DEFAULT_STREAM_TIMEOUT),
        stream_idle_timeout=c.get("stream_idle_timeout", DEFAULT_STREAM_IDLE_TIMEOUT),
    )
