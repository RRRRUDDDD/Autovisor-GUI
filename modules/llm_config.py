# -*- coding: utf-8 -*-
"""大模型配置归一化、厂商元数据、协议路由与端点 URL 构造。"""
from __future__ import annotations

import json
import os
import re
import sys
from urllib.parse import urlparse

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

from modules.logger import Logger
from modules.paths import get_resource_path

logger = Logger()

_LLM_CONF = None
_WARNED = False
_RUNTIME_PROXY = ""


def _load_provider_catalog() -> dict:
    """从共享 resources/llm_providers.json 加载三端统一的厂商与协议元数据。"""
    path = get_resource_path("resources", "llm_providers.json")
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("providers"), list):
            return data
    except Exception:
        pass
    return {}


_PROVIDER_CATALOG = _load_provider_catalog()

SUPPORTED_API_FORMATS = tuple(
    str(item.get("id") or "").strip().lower()
    for item in (_PROVIDER_CATALOG.get("apiFormats") or [])
    if isinstance(item, dict) and str(item.get("id") or "").strip()
) or (
    "openai",
    "openai-responses",
    "anthropic",
    "gemini",
    "azure",
    "ollama",
)

DEFAULT_AZURE_API_VERSION = "2024-10-21"
DEFAULT_ANTHROPIC_VERSION = "2023-06-01"

DEFAULT_STREAM_TIMEOUT = 120.0
DEFAULT_STREAM_IDLE_TIMEOUT = 30.0
DEFAULT_STREAM_RETRIES = 3
_STREAM_QUEUE_SIZE = 256

# 这些状态码重试也不会成功, 直接快速失败, 避免坏 Key / 欠费卡住整份试卷.
NON_RETRYABLE_STATUS = frozenset({400, 401, 402, 403, 404, 405, 409, 413, 422})
RETRYABLE_STATUS = frozenset({408, 425, 429})

_HTTP_STATUS_HINTS = {
    400: "请求参数错误",
    401: "API Key 无效",
    402: "余额不足",
    403: "无访问权限或已被封禁",
    404: "接口地址或模型不存在",
    405: "请求方法不被允许",
    409: "请求冲突",
    413: "请求体过大",
    422: "请求参数校验失败",
}

QUESTION_BANK_SCOPES = (
    "zhidao_homework",
    "zhidao_exam",
    "ai_homework",
    "ai_exam",
)
DEFAULT_QUESTION_BANK_SCOPES = ["zhidao_exam", "ai_exam"]

DEFAULT_CONF = {
    "provider": "deepseek",
    "api_format": "openai",
    "api_key": "",
    "api_base": "https://api.deepseek.com",
    "model": "deepseek-chat",
    "vision_model": "",
    "api_version": "",
    "auto_submit": False,
    "stream_timeout": DEFAULT_STREAM_TIMEOUT,
    "stream_idle_timeout": DEFAULT_STREAM_IDLE_TIMEOUT,
    "question_bank_token": "",
    "question_bank_url": "https://tk.enncy.cn/query",
    "question_bank_scopes": list(DEFAULT_QUESTION_BANK_SCOPES),
}

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}

_FALLBACK_PROVIDER_LABELS = {
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

_PROVIDER_LABELS = {
    str(item["id"]).strip().lower(): str(
        item.get("pyLabel") or item.get("shortName") or item["id"]
    )
    for item in (_PROVIDER_CATALOG.get("providers") or [])
    if isinstance(item, dict) and str(item.get("id") or "").strip()
} or dict(_FALLBACK_PROVIDER_LABELS)

_PROVIDER_HOST_RULES: list[tuple[str, tuple[str, ...]]] = [
    (
        str(item["id"]).strip().lower(),
        tuple(str(h).strip().lower() for h in (item.get("matchHosts") or []) if str(h).strip()),
    )
    for item in (_PROVIDER_CATALOG.get("providers") or [])
    if isinstance(item, dict)
    and str(item.get("id") or "").strip()
    and (item.get("matchHosts") or [])
]

_PROVIDER_FORMAT_RULES: list[tuple[str, tuple[str, ...]]] = [
    (
        str(item["id"]).strip().lower(),
        tuple(str(fmt).strip().lower() for fmt in (item.get("matchFormats") or []) if str(fmt).strip()),
    )
    for item in (_PROVIDER_CATALOG.get("providers") or [])
    if isinstance(item, dict)
    and str(item.get("id") or "").strip()
    and (item.get("matchFormats") or [])
]


class NonRetryableLLMError(RuntimeError):
    """不可重试的大模型接口错误(如 401/402), 调用方应停止后续请求."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class StreamNotSupportedError(RuntimeError):
    """接口未按 SSE 返回流式数据, 调用方应回退非流式请求."""

    def __init__(self, payload: str = ""):
        super().__init__("接口不支持流式响应")
        self.payload = payload


def _http_error_message(status: int, detail: str = "") -> str:
    hint = _HTTP_STATUS_HINTS.get(status, "")
    prefix = f"HTTP {status} {hint}" if hint else f"HTTP {status}"
    detail = (detail or "").strip()
    return f"{prefix}: {detail}" if detail else prefix


def _is_retryable_status(status: int) -> bool:
    return status in RETRYABLE_STATUS or 500 <= status < 600


def _retry_delay(attempt: int) -> float:
    return min(0.5 * (2 ** attempt), 4.0)


def _positive_float(value, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _resolve_facade(name: str, fallback):
    """支持测试直接 patch('modules.llm.<name>', ...) 的兼容解析器。"""
    mod = sys.modules.get("modules.llm")
    if mod is not None and hasattr(mod, name):
        return getattr(mod, name)
    return fallback


def get_runtime_proxy() -> str:
    return _RUNTIME_PROXY


def reset_cache():
    """清空缓存的 LLM 配置，供常驻后端在每轮任务开始前重新加载 data/llm.json。"""
    global _LLM_CONF, _WARNED
    _LLM_CONF = None
    _WARNED = False
    client_mod = sys.modules.get("modules.llm_client")
    if client_mod is not None and hasattr(client_mod, "close_all_http_sessions"):
        client_mod.close_all_http_sessions()


def set_proxy(proxy: str | None):
    """设置运行时网络代理（来自 config.ini），自动跳过本机回环地址。"""
    global _RUNTIME_PROXY
    new_proxy = str(proxy or "").strip()
    if new_proxy != _RUNTIME_PROXY:
        _RUNTIME_PROXY = new_proxy
        client_mod = sys.modules.get("modules.llm_client")
        if client_mod is not None and hasattr(client_mod, "close_all_http_sessions"):
            client_mod.close_all_http_sessions()


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
    """根据接口地址与协议推断服务商标识（规则来自共享 resources/llm_providers.json）。"""
    base = (api_base or "").strip().lower()
    fmt = (api_format or "").strip().lower()
    for prov_id, hosts in _PROVIDER_HOST_RULES:
        if any(h in base for h in hosts):
            return prov_id
    for prov_id, formats in _PROVIDER_FORMAT_RULES:
        if fmt in formats:
            return prov_id
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
    vision_model = str(src.get("vision_model") or src.get("visionModel") or "").strip()
    raw_provider = str(src.get("provider") or "").strip().lower()
    raw_format = str(src.get("api_format") or src.get("apiFormat") or "").strip().lower()
    if raw_format in SUPPORTED_API_FORMATS:
        api_format = raw_format
    else:
        api_format = infer_api_format(api_base, raw_provider)
    provider = raw_provider or infer_provider(api_base, api_format)
    api_version = str(src.get("api_version") or src.get("apiVersion") or "").strip()
    auto_submit = bool(src.get("auto_submit", src.get("autoSubmit", False)))
    stream_timeout = _positive_float(
        src.get("stream_timeout", src.get("streamTimeout")), DEFAULT_STREAM_TIMEOUT
    )
    stream_idle_timeout = _positive_float(
        src.get("stream_idle_timeout", src.get("streamIdleTimeout")),
        DEFAULT_STREAM_IDLE_TIMEOUT,
    )
    if stream_idle_timeout > stream_timeout:
        logger.debug(
            "stream_idle_timeout 不能大于 stream_timeout, 已自动收敛为总超时值."
        )
        stream_idle_timeout = stream_timeout
    question_bank_token = str(
        src.get("question_bank_token") or src.get("questionBankToken") or ""
    ).strip()
    question_bank_url = str(
        src.get("question_bank_url") or src.get("questionBankUrl") or ""
    ).strip() or DEFAULT_CONF["question_bank_url"]
    if "question_bank_scopes" in src:
        raw_scopes = src.get("question_bank_scopes")
    elif "questionBankScopes" in src:
        raw_scopes = src.get("questionBankScopes")
    else:
        raw_scopes = None
    question_bank_scopes = normalize_question_bank_scopes(raw_scopes)
    return {
        "provider": provider,
        "api_format": api_format,
        "api_key": api_key,
        "api_base": api_base,
        "model": model,
        "vision_model": vision_model,
        "api_version": api_version,
        "auto_submit": auto_submit,
        "stream_timeout": stream_timeout,
        "stream_idle_timeout": stream_idle_timeout,
        "question_bank_token": question_bank_token,
        "question_bank_url": question_bank_url,
        "question_bank_scopes": question_bank_scopes,
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


def conf() -> dict:
    """返回当前 LLM 配置 (只读用途)。"""
    return _resolve_facade("_conf", _conf)()


def normalize_question_bank_scopes(
    raw, *, default: list[str] | tuple[str, ...] = DEFAULT_QUESTION_BANK_SCOPES
) -> list[str]:
    """归一化外部题库作用域列表；未提供时回退默认值，显式空列表保留为空。"""
    if raw is None:
        return list(default)
    if isinstance(raw, str):
        items = [part.strip().lower() for part in re.split(r"[\s,;]+", raw) if part.strip()]
    elif isinstance(raw, (list, tuple, set, frozenset)):
        items = [str(item or "").strip().lower() for item in raw]
    else:
        return list(default)
    allowed = {item for item in items if item in QUESTION_BANK_SCOPES}
    return [scope for scope in QUESTION_BANK_SCOPES if scope in allowed]


def question_bank_conf() -> dict:
    """返回外部题库的 token、查询地址与生效作用域; token 为空表示未启用."""
    c = _resolve_facade("_conf", _conf)()
    return {
        "token": c.get("question_bank_token", ""),
        "url": c.get("question_bank_url", DEFAULT_CONF["question_bank_url"]),
        "scopes": normalize_question_bank_scopes(c.get("question_bank_scopes")),
    }


def provider_label(c: dict | None = None) -> str:
    """返回当前配置的大模型可读名称，用于日志展示。"""
    cfg = normalize_conf(c) if c is not None else _resolve_facade("_conf", _conf)()
    name = _PROVIDER_LABELS.get(cfg.get("provider", ""), "大模型")
    model = cfg.get("model", "")
    return f"{name}({model})" if model else name


def enabled() -> bool:
    """是否已配置可用的大模型接口（云端模型需配置 API Key，本地 Ollama / LM Studio 免 Key）。"""
    if requests is None:
        return False
    c = _resolve_facade("_conf", _conf)()
    if not c.get("api_base") or not c.get("model"):
        return False
    if c.get("api_key"):
        return True
    return is_local_endpoint(c.get("api_base", ""), c.get("provider", ""), c.get("api_format", ""))


def vision_enabled() -> bool:
    """是否配置了可用于图片题的视觉模型（复用主模型的接口与鉴权）。"""
    if requests is None:
        return False
    c = _resolve_facade("_conf", _conf)()
    if not c.get("vision_model") or not c.get("api_base"):
        return False
    if c.get("api_key"):
        return True
    return is_local_endpoint(c.get("api_base", ""), c.get("provider", ""), c.get("api_format", ""))


def vision_label() -> str:
    """返回视觉模型的可读名称，用于日志展示。"""
    c = _resolve_facade("_conf", _conf)()
    name = _PROVIDER_LABELS.get(c.get("provider", ""), "大模型")
    model = c.get("vision_model", "")
    return f"{name}({model})" if model else f"{name}(未配置视觉模型)"


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
