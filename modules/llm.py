# -*- coding: utf-8 -*-
"""通用大模型答题客户端：支持市面上主流 LLM 接口协议。

支持协议 (api_format):
- openai: OpenAI Chat Completions 兼容接口 (DeepSeek / OpenAI / 通义千问 / 智谱 / Kimi / 豆包 / 硅基流动 / 混元 / 星火 / 千帆 / MiniMax / 零一万物 / xAI / Groq / Mistral / OpenRouter / LM Studio / 各类中转站等)
- openai-responses: OpenAI Responses API (/responses)
- anthropic: Anthropic Claude 原生接口 (/v1/messages)
- gemini: Google Gemini 原生接口 (/v1beta/models/{model}:generateContent)
- azure: Azure OpenAI Service (api-key + api-version)
- ollama: Ollama 本地原生接口 (/api/chat，无需 API Key)

配置文件: data/llm.json
"""
import asyncio
import difflib
import json
import os
import re
import time
from urllib.parse import urlparse

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

from modules.http_utils import build_requests_proxies
from modules.logger import Logger

logger = Logger()

_LLM_CONF = None
_WARNED = False
_RUNTIME_PROXY = ""

SUPPORTED_API_FORMATS = (
    "openai",
    "openai-responses",
    "anthropic",
    "gemini",
    "azure",
    "ollama",
)

DEFAULT_AZURE_API_VERSION = "2024-10-21"
DEFAULT_ANTHROPIC_VERSION = "2023-06-01"

DEFAULT_CONF = {
    "provider": "deepseek",
    "api_format": "openai",
    "api_key": "",
    "api_base": "https://api.deepseek.com",
    "model": "deepseek-chat",
    "api_version": "",
    "auto_submit": False,
}

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}

_PROVIDER_LABELS = {
    "deepseek": "DeepSeek",
    "openai": "OpenAI",
    "anthropic": "Anthropic Claude",
    "gemini": "Google Gemini",
    "qwen": "通义千问",
    "zhipu": "智谱 GLM",
    "moonshot": "Kimi (Moonshot)",
    "doubao": "豆包 (火山方舟)",
    "siliconflow": "硅基流动",
    "hunyuan": "腾讯混元",
    "spark": "讯飞星火",
    "qianfan": "百度千帆",
    "minimax": "MiniMax",
    "yi": "零一万物",
    "xai": "xAI Grok",
    "groq": "Groq",
    "mistral": "Mistral",
    "openrouter": "OpenRouter",
    "azure": "Azure OpenAI",
    "ollama": "Ollama 本地",
    "lmstudio": "LM Studio 本地",
    "custom": "自定义大模型",
}


def reset_cache():
    """清空缓存的 LLM 配置，供常驻后端在每轮任务开始前重新加载 data/llm.json。"""
    global _LLM_CONF, _WARNED
    _LLM_CONF = None
    _WARNED = False


def set_proxy(proxy: str | None):
    """设置运行时网络代理（来自 config.ini），自动跳过本机回环地址。"""
    global _RUNTIME_PROXY
    _RUNTIME_PROXY = str(proxy or "").strip()


def infer_api_format(api_base: str = "", provider: str = "") -> str:
    """根据接口地址或服务商推断协议格式。"""
    prov = (provider or "").strip().lower()
    if prov in ("anthropic", "claude"):
        return "anthropic"
    if prov == "gemini":
        return "gemini"
    if prov == "azure":
        return "azure"
    if prov == "ollama":
        return "ollama"

    base = (api_base or "").strip().rstrip("/").lower()
    if not base:
        return "openai"
    if base.endswith("/messages") or "api.anthropic.com" in base:
        return "anthropic"
    if (
        ":generatecontent" in base
        or ("generativelanguage.googleapis.com" in base and "/openai" not in base)
    ):
        return "gemini"
    if ".openai.azure.com" in base or "/openai/deployments/" in base:
        return "azure"
    if base.endswith("/api/chat"):
        return "ollama"
    if base.endswith("/responses"):
        return "openai-responses"
    return "openai"


def infer_provider(api_base: str = "", api_format: str = "") -> str:
    """根据接口地址与协议推断服务商标识。"""
    base = (api_base or "").strip().lower()
    fmt = (api_format or "").strip().lower()
    if "api.deepseek.com" in base:
        return "deepseek"
    if "api.openai.com" in base:
        return "openai"
    if "anthropic.com" in base or fmt == "anthropic":
        return "anthropic"
    if "generativelanguage.googleapis.com" in base or fmt == "gemini":
        return "gemini"
    if "dashscope.aliyuncs.com" in base:
        return "qwen"
    if "open.bigmodel.cn" in base:
        return "zhipu"
    if "api.moonshot.cn" in base or "api.moonshot.ai" in base:
        return "moonshot"
    if "volces.com" in base:
        return "doubao"
    if "siliconflow.cn" in base or "siliconflow.com" in base:
        return "siliconflow"
    if "hunyuan.cloud.tencent.com" in base:
        return "hunyuan"
    if "spark-api-open.xf-yun.com" in base:
        return "spark"
    if "qianfan.baidubce.com" in base:
        return "qianfan"
    if "minimax.chat" in base or "minimaxi.com" in base:
        return "minimax"
    if "lingyiwanwu.com" in base:
        return "yi"
    if "api.x.ai" in base:
        return "xai"
    if "api.groq.com" in base:
        return "groq"
    if "api.mistral.ai" in base:
        return "mistral"
    if "openrouter.ai" in base:
        return "openrouter"
    if ".openai.azure.com" in base or fmt == "azure":
        return "azure"
    if fmt == "ollama" or ":11434" in base:
        return "ollama"
    if ":1234" in base:
        return "lmstudio"
    return "custom"


def is_local_endpoint(api_base: str = "", provider: str = "", api_format: str = "") -> bool:
    """判断是否为免 API Key 的本地模型端点（Ollama / LM Studio / localhost）。"""
    fmt = (api_format or "").strip().lower()
    prov = (provider or "").strip().lower()
    if fmt == "ollama" or prov in ("ollama", "lmstudio"):
        return True
    try:
        host = (urlparse((api_base or "").strip()).hostname or "").lower()
    except ValueError:
        host = ""
    return host in _LOCAL_HOSTS


def normalize_conf(raw: dict | None) -> dict:
    """归一化 LLM 配置字典，完全向后兼容仅含 4 个字段的旧版 data/llm.json。"""
    src = raw if isinstance(raw, dict) else {}
    api_key = str(src.get("api_key") or src.get("apiKey") or "").strip()
    api_base = str(src.get("api_base") or src.get("apiBase") or "").strip()
    if not api_base:
        api_base = DEFAULT_CONF["api_base"]
    model = str(src.get("model") or "").strip() or DEFAULT_CONF["model"]
    raw_provider = str(src.get("provider") or "").strip().lower()
    raw_format = str(src.get("api_format") or src.get("apiFormat") or "").strip().lower()
    if raw_format in SUPPORTED_API_FORMATS:
        api_format = raw_format
    else:
        api_format = infer_api_format(api_base, raw_provider)
    provider = raw_provider or infer_provider(api_base, api_format)
    api_version = str(src.get("api_version") or src.get("apiVersion") or "").strip()
    auto_submit = bool(src.get("auto_submit", src.get("autoSubmit", False)))
    return {
        "provider": provider,
        "api_format": api_format,
        "api_key": api_key,
        "api_base": api_base,
        "model": model,
        "api_version": api_version,
        "auto_submit": auto_submit,
    }


def _conf():
    global _LLM_CONF, _WARNED
    if _LLM_CONF is not None:
        return _LLM_CONF
    raw = {}
    try:
        # 与启动时 ensure_config_files 的生成位置保持一致(运行根目录/data)
        path = os.path.join(logger.runtime_root, "data", "llm.json")
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8-sig") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    raw = loaded
    except Exception as exc:
        logger.log_exception("读取 data/llm.json 失败, 大模型答题已禁用.", exc)
    _LLM_CONF = normalize_conf(raw)
    if (
        not _LLM_CONF.get("api_key")
        and not is_local_endpoint(
            _LLM_CONF.get("api_base", ""),
            _LLM_CONF.get("provider", ""),
            _LLM_CONF.get("api_format", ""),
        )
        and not _WARNED
    ):
        _WARNED = True
        logger.warn("未配置大模型 API Key (data/llm.json), 弹题回退为默认行为.")
    return _LLM_CONF


def provider_label(c: dict | None = None) -> str:
    """返回当前配置的大模型可读名称，用于日志展示。"""
    cfg = normalize_conf(c) if c is not None else _conf()
    name = _PROVIDER_LABELS.get(cfg.get("provider", ""), "大模型")
    model = cfg.get("model", "")
    return f"{name}({model})" if model else name


def _norm(s: str) -> str:
    return re.sub(r"[\s\u3000]+", "", s or "")


def _brief(text: str, limit: int = 80) -> str:
    """将题干/选项/回答折叠成单行摘要，避免多行文本破坏日志格式。"""
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[:limit]}..."


def _sim(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _strip_think_tags(text: str) -> str:
    """移除推理模型可能输出的 <think>...</think> 思考过程，保留最终答案。"""
    cleaned = re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL | re.IGNORECASE)
    return cleaned.strip()


def build_endpoint(
    api_base: str,
    model: str = "",
    api_format: str = "openai",
    api_version: str = "",
) -> str:
    """按协议格式把 api_base 规整为可直接 POST 的完整接口 URL。"""
    base = (api_base or "").strip().rstrip("/")
    fmt = (api_format or "openai").strip().lower()
    if fmt == "anthropic":
        if base.lower().endswith("/messages"):
            return base
        if base.lower().endswith("/v1"):
            return f"{base}/messages"
        return f"{base}/v1/messages"
    if fmt == "gemini":
        if ":generatecontent" in base.lower():
            return base
        model_id = (model or "").strip()
        if model_id.startswith("models/"):
            model_id = model_id[len("models/") :]
        if base.lower().endswith("/models"):
            return f"{base}/{model_id}:generateContent"
        if re.search(r"/v1(?:beta|alpha)?$", base, re.IGNORECASE):
            return f"{base}/models/{model_id}:generateContent"
        return f"{base}/v1beta/models/{model_id}:generateContent"
    if fmt == "azure":
        ver = (api_version or "").strip() or DEFAULT_AZURE_API_VERSION
        if "/chat/completions" in base.lower():
            url = base
        elif "/deployments/" in base.lower():
            url = f"{base}/chat/completions"
        else:
            url = f"{base}/openai/deployments/{(model or '').strip()}/chat/completions"
        if "api-version=" not in url.lower():
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}api-version={ver}"
        return url
    if fmt == "ollama":
        if base.lower().endswith("/api/chat"):
            return base
        if base.lower().endswith("/v1"):
            base = base[:-3].rstrip("/")
        return f"{base}/api/chat"
    if fmt == "openai-responses":
        if base.lower().endswith("/responses"):
            return base
        return f"{base}/responses"
    # 默认 openai chat completions
    if base.lower().endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def build_models_endpoint(api_base: str, api_format: str = "openai") -> str:
    """按协议格式构造模型列表查询 URL。"""
    base = (api_base or "").strip().rstrip("/")
    lower = base.lower()
    fmt = (api_format or "openai").strip().lower()
    if fmt == "azure":
        raise ValueError("Azure OpenAI 请直接填写部署名称（Deployment Name）作为模型名称")
    if fmt == "ollama":
        if lower.endswith("/api/chat"):
            base = base[:-len("/api/chat")].rstrip("/")
        elif lower.endswith("/v1"):
            base = base[:-len("/v1")].rstrip("/")
        return f"{base}/api/tags"
    if fmt == "anthropic":
        if lower.endswith("/messages"):
            base = base[:-len("/messages")].rstrip("/")
        if base.lower().endswith("/v1"):
            return f"{base}/models"
        return f"{base}/v1/models"
    if fmt == "gemini":
        stripped = base.split("/models/", 1)[0].rstrip("/")
        s_lower = stripped.lower()
        if s_lower.endswith("/models"):
            return stripped
        if re.search(r"/v1(?:beta|alpha)?$", s_lower):
            return f"{stripped}/models"
        return f"{stripped}/v1beta/models"
    for suffix in ("/chat/completions", "/responses"):
        if lower.endswith(suffix):
            base = base[:-len(suffix)].rstrip("/")
            break
    return f"{base}/models"


def test_connection(conf_override: dict | None, proxy: str = "") -> str:
    """测试大模型连通性，返回耗时与模型简短回复；失败抛出 ValueError/RuntimeError。"""
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
    proxies = _resolve_proxies(endpoint, proxy)
    headers = {"Content-Type": "application/json"}
    prompt = "请只回复 OK"

    if fmt == "anthropic":
        if api_key:
            headers["x-api-key"] = api_key
        headers["anthropic-version"] = api_version or DEFAULT_ANTHROPIC_VERSION
        base_body = {
            "model": model,
            "max_tokens": 32,
            "messages": [{"role": "user", "content": prompt}],
        }
        body = {**base_body, "temperature": 0}
        extractor = _extract_anthropic_content
    elif fmt == "gemini":
        if api_key:
            headers["x-goog-api-key"] = api_key
        base_body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
        body = {**base_body, "generationConfig": {"temperature": 0, "maxOutputTokens": 32}}
        extractor = _extract_gemini_content
    elif fmt == "ollama":
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        base_body = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }
        body = {**base_body, "options": {"temperature": 0}}
        extractor = _extract_ollama_content
    elif fmt == "openai-responses":
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        base_body = {"model": model, "input": prompt}
        body = dict(base_body)
        extractor = _extract_openai_responses_content
    else:
        if fmt == "azure":
            if api_key:
                headers["api-key"] = api_key
        elif api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        base_body = {"model": model, "messages": [{"role": "user", "content": prompt}]}
        body = {**base_body, "temperature": 0, "max_tokens": 32}
        extractor = _extract_openai_content

    started = time.monotonic()
    try:
        data = _post_with_fallback(endpoint, headers, body, base_body, proxies, timeout=30)
    except ValueError as exc:
        raise RuntimeError("接口返回了非 JSON 响应，请检查 API Base URL 与协议格式") from exc
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"请求失败: {exc}") from exc

    elapsed_ms = int((time.monotonic() - started) * 1000)
    try:
        reply = extractor(data).strip()
    except Exception as exc:
        raise RuntimeError("连接已通，但未解析出模型文本内容，请确认接口协议是否正确") from exc
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
        resp = requests.get(endpoint, **kwargs)
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


def _resolve_proxies(url: str, proxy: str | None = None) -> dict | None:
    eff = _RUNTIME_PROXY if proxy is None else str(proxy or "").strip()
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
):
    kwargs = {"headers": headers, "json": body, "timeout": timeout}
    if proxies:
        kwargs["proxies"] = proxies
    resp = requests.post(url, **kwargs)
    if (
        getattr(resp, "status_code", 200) == 400
        and fallback_body is not None
        and fallback_body != body
    ):
        logger.debug(f"模型接口返回 400, 去掉可选参数后重试: {url}")
        fallback_kwargs = {"headers": headers, "json": fallback_body, "timeout": timeout}
        if proxies:
            fallback_kwargs["proxies"] = proxies
        resp = requests.post(url, **fallback_kwargs)
    if getattr(resp, "status_code", 200) >= 400:
        detail = _extract_error_detail(resp)
        try:
            resp.raise_for_status()
        except Exception as exc:
            if detail:
                raise RuntimeError(f"HTTP {resp.status_code}: {detail}") from exc
            raise
    return resp.json()


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
) -> str:
    fmt = (api_format or "openai").strip().lower()
    proxies = _resolve_proxies(api, proxy)
    headers = {"Content-Type": "application/json"}

    if fmt == "anthropic":
        if api_key:
            headers["x-api-key"] = api_key
        headers["anthropic-version"] = (api_version or "").strip() or DEFAULT_ANTHROPIC_VERSION
        base_body = {
            "model": model,
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": prompt}],
        }
        body = {**base_body, "temperature": 0}
        data = _post_with_fallback(api, headers, body, base_body, proxies)
        return _extract_anthropic_content(data)

    if fmt == "gemini":
        if api_key:
            headers["x-goog-api-key"] = api_key
        base_body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
        gen_cfg = {"temperature": 0}
        if json_mode:
            gen_cfg["responseMimeType"] = "application/json"
        body = {**base_body, "generationConfig": gen_cfg}
        data = _post_with_fallback(api, headers, body, base_body, proxies)
        return _extract_gemini_content(data)

    if fmt == "ollama":
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        base_body = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }
        body = {**base_body, "options": {"temperature": 0}}
        if json_mode:
            body["format"] = "json"
        data = _post_with_fallback(api, headers, body, base_body, proxies)
        return _extract_ollama_content(data)

    if fmt == "openai-responses":
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        base_body = {"model": model, "input": prompt}
        body = dict(base_body)
        if json_mode:
            body["text"] = {"format": {"type": "json_object"}}
        data = _post_with_fallback(api, headers, body, base_body, proxies)
        return _extract_openai_responses_content(data)

    # openai / azure
    if fmt == "azure":
        if api_key:
            headers["api-key"] = api_key
    elif api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    base_body = {"model": model, "messages": [{"role": "user", "content": prompt}]}
    body = {**base_body, "temperature": 0}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    data = _post_with_fallback(api, headers, body, base_body, proxies)
    return _extract_openai_content(data)


def chat_sync(
    prompt: str,
    json_mode: bool = False,
    conf_override: dict | None = None,
    proxy: str | None = None,
) -> str:
    """使用当前（或指定）LLM 配置发起一次同步对话，自动适配协议格式。"""
    c = normalize_conf(conf_override) if conf_override is not None else _conf()
    fmt = c.get("api_format") or infer_api_format(c.get("api_base", ""), c.get("provider", ""))
    api = build_endpoint(c["api_base"], c["model"], fmt, c.get("api_version", ""))
    if fmt == "openai" and proxy is None and not c.get("api_version"):
        return _chat_sync(api, c["api_key"], c["model"], prompt, json_mode)
    return _chat_sync(
        api,
        c["api_key"],
        c["model"],
        prompt,
        json_mode,
        api_format=fmt,
        api_version=c.get("api_version", ""),
        proxy=proxy,
    )


def _build_option_prompt(question_text: str, options: list) -> str:
    opt_lines = "\n".join(f"{i}. {o}" for i, o in enumerate(options))
    return (
        "你是网课弹题助手。下面是一道弹题的题目和选项，直接给出正确答案。\n"
        "只输出正确选项的完整原文，不要解释，不要加标点以外的任何内容。\n\n"
        f"题目：{question_text}\n\n选项：\n{opt_lines}"
    )


def _ask_llmSync(question_text: str, options: list) -> str:
    return chat_sync(_build_option_prompt(question_text, options), json_mode=False)


async def _ask_llm(question_text: str, options: list) -> str:
    return await asyncio.to_thread(_ask_llmSync, question_text, options)


def _match_option(answer: str, option_texts: list) -> tuple[int, float]:
    """将模型回复匹配到最佳选项下标, 匹配失败返回 (-1, best_score)."""
    cleaned = _strip_think_tags(answer or "")
    if not cleaned:
        return -1, 0.0
    best, best_score = -1, 0.0
    for idx, t in enumerate(option_texts):
        if not t:
            continue
        score = _sim(cleaned, t)
        if score > best_score:
            best, best_score = idx, score
    if best >= 0 and best_score >= 0.5:
        return best, best_score
    # 兼容回复为选项字母/序号(如 "B" / "B. xxx" / "正确答案是 B")或包含唯一选项全文的情况
    idxs = to_indexes(cleaned, option_texts)
    if len(idxs) == 1 and 0 <= idxs[0] < len(option_texts):
        return idxs[0], max(best_score, 0.8)
    letter_match = re.search(
        r"(?:正确答案|答案|选项|选择|选)\s*(?:是|为|:|：)?\s*([A-Ha-h])(?![A-Za-z])",
        cleaned,
    )
    if letter_match:
        letter_idx = "ABCDEFGH".index(letter_match.group(1).upper())
        if 0 <= letter_idx < len(option_texts):
            return letter_idx, max(best_score, 0.8)
    n_ans = _norm(cleaned)
    contained = [
        idx
        for idx, t in enumerate(option_texts)
        if len(_norm(t)) >= 2 and _norm(t) in n_ans
    ]
    if len(contained) == 1:
        return contained[0], max(best_score, 0.75)
    return -1, best_score


async def _ask_option_with_fallback(
    question_text: str,
    option_texts: list,
    answerer=None,
    *,
    event_name: str,
    q_brief: str,
) -> tuple[int, float, str, str]:
    """优先调用智慧树 AI (answerer), 失败或未匹配时回退自定义大模型."""
    prompt = _build_option_prompt(question_text, option_texts)

    if answerer is not None:
        try:
            answer = await asyncio.to_thread(answerer, prompt)
            best, best_score = _match_option(answer, option_texts)
            if best >= 0:
                return best, best_score, "智慧树AI", answer
            logger.debug(f"智慧树 AI 答案未能匹配选项(回答: {_brief(answer, 50)}), 准备回退自定义大模型.")
            if not enabled():
                logger.warn(f"智慧树 AI 答案未能匹配选项(回答: {answer[:50]}), 使用默认选择.", shift=True)
                logger.event(
                    event_name,
                    方式="智慧树AI",
                    题目=q_brief,
                    回答=_brief(answer, 80),
                    结果="未匹配",
                )
                return -1, best_score, "智慧树AI", answer
        except Exception as exc:
            logger.debug(f"智慧树 AI 答题请求失败, 准备回退自定义大模型: {exc}")
            if not enabled():
                logger.warn(f"智慧树 AI 答题请求失败: {exc}, 使用默认选择.", shift=True)
                logger.event(
                    event_name,
                    方式="智慧树AI",
                    题目=q_brief,
                    结果="请求失败",
                    错误=logger.summarize_exception(exc),
                )
                return -1, 0.0, "智慧树AI", ""

    if not enabled():
        return -1, 0.0, "", ""

    label = provider_label()
    try:
        answer = await _ask_llm(question_text, option_texts)
    except Exception as exc:
        logger.warn(f"大模型答题请求失败: {exc}, 回退默认行为.", shift=True)
        logger.event(
            event_name,
            方式=label,
            题目=q_brief,
            结果="请求失败",
            错误=logger.summarize_exception(exc),
        )
        return -1, 0.0, label, ""

    best, best_score = _match_option(answer, option_texts)
    if best < 0:
        logger.warn(f"大模型答案未能匹配选项(回答: {answer[:50]}), 回退默认行为.", shift=True)
        logger.event(
            event_name,
            方式=label,
            题目=q_brief,
            回答=_brief(answer, 80),
            结果="未匹配",
        )
        return -1, best_score, label, answer
    return best, best_score, label, answer


def enabled() -> bool:
    """是否已配置可用的大模型接口（云端模型需配置 API Key，本地 Ollama / LM Studio 免 Key）。"""
    if requests is None:
        return False
    c = _conf()
    if not c.get("api_base") or not c.get("model"):
        return False
    if c.get("api_key"):
        return True
    return is_local_endpoint(c.get("api_base", ""), c.get("provider", ""), c.get("api_format", ""))


def conf() -> dict:
    """返回当前 LLM 配置 (只读用途)。"""
    return _conf()


def to_indexes(answer, options: list) -> list:
    """把各种形态的答案统一成选项下标数组; 填空/无法解析返回空表以外的处理见调用方。"""
    if answer is None:
        return []
    if isinstance(answer, list) and answer and all(isinstance(x, int) for x in answer):
        return answer
    items = answer if isinstance(answer, list) else [answer]
    items = [str(x).strip() for x in items if str(x).strip()]
    idxs = []
    letters = "ABCDEFGH"
    for item in items:
        if re.fullmatch(r"[A-Ha-h]+", item) and len(item) <= max(len(options), 1):
            idxs.extend(letters.index(c.upper()) for c in item.upper())
            continue
        n_item = _norm(item)
        hit = -1
        for i, o in enumerate(options):
            n_o = _norm(o)
            if n_o and (n_o == n_item or n_o in n_item or n_item in n_o):
                hit = i
                break
        if hit < 0:
            m = re.match(r"^([A-Ha-h])[\.、．:：]", item)
            if m:
                hit = letters.index(m.group(1).upper())
        if hit < 0 and item.isdigit():
            n = int(item)
            hit = n - 1 if 1 <= n <= len(options) else n
        if 0 <= hit < len(options):
            idxs.append(hit)
    return idxs


def _parse_quiz_questions(answer_text: str) -> list:
    """从模型回复里取出 questions 数组, 容忍 ```json 围栏和前后说明."""
    raw = _strip_think_tags(answer_text or "")
    if not raw:
        return []
    block = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL | re.IGNORECASE)
    if block:
        raw = block.group(1).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        raw = raw[start : end + 1]
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    questions = data.get("questions") if isinstance(data, dict) else None
    return questions if isinstance(questions, list) else []


def _quiz_prompt(text: str) -> str:
    return f"""下面是一个在线测验页面的文本。提取其中所有测验题目（忽略导航栏、菜单、按钮、页脚等无关文本）并作答。

只输出一个 JSON 对象，结构严格如下：
{{"questions": [
  {{"index": 1,
    "question": "题干原文",
    "options": ["选项原文1", "选项原文2"],
    "type": "single|multiple|judge|fill",
    "answer": [0],
    "confidence": "high|medium|low",
    "reason": "一句话理由"}}]}}

规则:
1. answer 对选择/判断题是选项下标数组（从 0 开始），单选长度 1、多选长度 >=1；填空题 options 留空数组、answer 是 ["答案文本"]。
2. option 必须抄页面上的选项原文，不要带 "A."、"B." 之类字母前缀，不要改写。
3. 判断题（对/错、正确/错误）按两个选项处理。
4. 没把握也要给最可能的答案，confidence 填 low。
5. 页面文本可能乱序，靠题号与选项结构还原顺序。

页面文本:
<<<
{text}
>>>"""


async def solve_quiz_page(text: str, answerer=None) -> list:
    """整页抽取测验题目并作答, 返回归一化后的题目列表.

    answerer: 可选同步函数 prompt -> 原始回复文本, 会优先尝试(智慧树 AI);
    失败或未给出有效题目时回退到 data/llm.json 配置的大模型.
    """
    if not text or len(text.strip()) < 30:
        return []
    prompt = _quiz_prompt(text)
    answer_text = None
    source = ""
    if answerer is not None:
        try:
            answer_text = await asyncio.to_thread(answerer, prompt)
        except Exception as exc:
            logger.debug(f"智慧树 AI 整页作答失败, 回退自定义大模型: {exc}")
    raw = _parse_quiz_questions(answer_text)
    if raw:
        source = "智慧树AI"
    else:
        if answerer is not None and answer_text:
            logger.debug("智慧树 AI 返回内容未解析出有效题目, 准备回退自定义大模型.")
        if not enabled():
            logger.warn("未配置可用的自定义大模型, 无法解析当前测验页题目.", shift=True)
            return []
        source = provider_label()
        logger.debug(f"使用自定义大模型整页作答: {source}")
        try:
            answer_text = await asyncio.to_thread(chat_sync, prompt, True)
        except Exception as exc:
            logger.warn(f"大模型整页作答请求失败: {exc}", shift=True)
            logger.event(
                "测验题解",
                来源=source,
                结果="请求失败",
                错误=logger.summarize_exception(exc),
            )
            return []
        raw = _parse_quiz_questions(answer_text)
        if not raw:
            logger.warn("大模型返回的 JSON 解析失败, 本轮跳过.", shift=True)
            logger.event(
                "测验题解",
                来源=source,
                结果="解析失败",
                原始回复=_brief(answer_text, 120),
            )
            return []

    questions = []
    for i, q in enumerate(raw):
        if not isinstance(q, dict):
            continue
        question = str(q.get("question") or q.get("题干") or q.get("题目") or "").strip()
        options = [str(o).strip() for o in (q.get("options") or q.get("选项") or []) if str(o).strip()]
        qtype = str(q.get("type") or q.get("题型") or "").lower()
        raw_answer = q.get("answer", q.get("答案"))
        if qtype not in ("single", "multiple", "judge", "fill"):
            qtype = "fill" if not options else ("multiple" if isinstance(raw_answer, list) and len(raw_answer) > 1 else "single")
        if qtype == "fill":
            ans_text = raw_answer[0] if isinstance(raw_answer, list) and raw_answer else raw_answer
            questions.append({
                "index": q.get("index") or i + 1,
                "question": question, "options": [], "type": "fill",
                "answer": [str(ans_text or "").strip()],
                "confidence": str(q.get("confidence") or q.get("置信度") or "medium").lower(),
                "reason": str(q.get("reason") or q.get("理由") or ""),
            })
        else:
            questions.append({
                "index": q.get("index") or i + 1,
                "question": question, "options": options, "type": qtype,
                "answer": to_indexes(raw_answer, options),
                "confidence": str(q.get("confidence") or q.get("置信度") or "medium").lower(),
                "reason": str(q.get("reason") or q.get("理由") or ""),
            })
    if questions:
        logger.info(f"题目解析完成({source}): 共识别 {len(questions)} 题.")
        logger.event("测验题解", 来源=source, 题数=len(questions))
        for q in questions:
            if q["type"] == "fill":
                ans_summary = _brief((q.get("answer") or [""])[0], 80)
            else:
                opts = q.get("options") or []
                ans_summary = "; ".join(
                    _brief(opts[idx], 40) if isinstance(idx, int) and 0 <= idx < len(opts) else str(idx)
                    for idx in (q.get("answer") or [])
                )
            logger.debug(
                f"[题解明细] 题{q.get('index')}({q.get('type')}) "
                f"题目={_brief(q.get('question', ''), 80)} | 答案={ans_summary} | "
                f"置信度={q.get('confidence', '?')} | 理由={_brief(q.get('reason', ''), 80)}"
            )
    return questions


async def pick_option(dialog_text: str, option_texts: list, answerer=None) -> int:
    """从弹窗文本+选项里选出正确选项下标（优先智慧树 AI，失败回退自定义大模型）。失败/没把握返回 -1。"""
    if (answerer is None and not enabled()) or not option_texts:
        return -1
    if not any(option_texts):
        return -1
    question_text = dialog_text or "请从选项中选出正确答案。"
    q_brief = _brief(question_text, 80)
    best, best_score, label, answer = await _ask_option_with_fallback(
        question_text,
        option_texts,
        answerer=answerer,
        event_name="大模型选题",
        q_brief=q_brief,
    )
    if best >= 0:
        logger.info(
            f"大模型选题({label}): 题目「{_brief(dialog_text, 48)}」 -> "
            f"选择「{_brief(option_texts[best], 60)}」(相似度:{round(best_score, 2)})"
        )
        logger.event(
            "大模型选题",
            方式=label,
            题目=q_brief,
            回答=_brief(answer, 80),
            选项=_brief(option_texts[best], 80),
            相似度=round(best_score, 2),
        )
        logger.debug(f"大模型选题候选选项: {option_texts}")
    return best


async def answer_with_llm(page, ques, choices, answerer=None) -> bool:
    """对一道弹题用 AI 作答（优先智慧树 AI，失败回退自定义大模型）。成功点击返回 True；未启用/失败返回 False。"""
    if (answerer is None and not enabled()) or not choices:
        return False

    try:
        option_texts = [" ".join((await c.inner_text()).split()) for c in choices]
    except Exception:
        return False
    if not option_texts or all(not t for t in option_texts):
        return False

    # 题干：优先 .topic-title，否则取弹窗文本去掉选项行
    question_text = ""
    try:
        title = await page.query_selector(".topic-title")
        if title:
            question_text = await title.inner_text()
    except Exception:
        pass
    if not question_text:
        try:
            dialog = await page.query_selector(".el-dialog")
            if dialog:
                question_text = await dialog.inner_text()
        except Exception:
            pass
    question_text = " ".join((question_text or "").split())
    q_brief = _brief(question_text, 80)

    best, best_score, label, answer = await _ask_option_with_fallback(
        question_text,
        option_texts,
        answerer=answerer,
        event_name="课中答题",
        q_brief=q_brief,
    )
    if best < 0:
        return False

    chosen_text = _brief(option_texts[best], 80)
    logger.debug(f"课中弹题选项列表: {option_texts}")
    try:
        await choices[best].click(timeout=2000)
        await page.wait_for_timeout(200)
        logger.info(
            f"课中弹题({label}): 题目「{_brief(question_text, 48)}」 -> "
            f"选择「{_brief(option_texts[best], 60)}」(相似度:{round(best_score, 2)})"
        )
        logger.event(
            "课中答题",
            方式=label,
            题目=q_brief,
            回答=_brief(answer, 80),
            选项=chosen_text,
            相似度=round(best_score, 2),
            结果="已作答",
        )
        return True
    except Exception as exc:
        logger.warn(f"点击大模型选项失败: {exc}, 回退默认行为.", shift=True)
        logger.event(
            "课中答题",
            方式=label,
            题目=q_brief,
            选项=chosen_text,
            结果="点击失败",
            错误=logger.summarize_exception(exc),
        )
        return False
