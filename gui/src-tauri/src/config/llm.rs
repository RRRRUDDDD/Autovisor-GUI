use serde::{Deserialize, Serialize};
use std::sync::OnceLock;

const LLM_PROVIDERS_JSON: &str = include_str!("../../../../resources/llm_providers.json");

fn default_llm_provider() -> String {
    "deepseek".to_string()
}

fn default_llm_api_format() -> String {
    "openai".to_string()
}

fn default_llm_api_base() -> String {
    "https://api.deepseek.com".to_string()
}

fn default_llm_model() -> String {
    "deepseek-chat".to_string()
}

fn default_stream_timeout() -> f64 {
    120.0
}

fn default_stream_idle_timeout() -> f64 {
    30.0
}

fn default_question_bank_url() -> String {
    "https://tk.enncy.cn/query".to_string()
}

pub const QUESTION_BANK_SCOPES: &[&str] = &[
    "zhidao_homework",
    "zhidao_exam",
    "ai_homework",
    "ai_exam",
];

fn default_question_bank_scopes() -> Vec<String> {
    vec!["zhidao_exam".to_string(), "ai_exam".to_string()]
}

fn normalize_question_bank_scopes(raw: &[String]) -> Vec<String> {
    let mut allowed = std::collections::HashSet::new();
    for item in raw {
        let lowered = item.trim().to_ascii_lowercase();
        if QUESTION_BANK_SCOPES.contains(&lowered.as_str()) {
            allowed.insert(lowered);
        }
    }
    QUESTION_BANK_SCOPES
        .iter()
        .filter(|scope| allowed.contains(**scope))
        .map(|scope| (*scope).to_string())
        .collect()
}

#[derive(Debug, Deserialize)]
struct LlmFormatMeta {
    id: String,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct LlmProviderMeta {
    id: String,
    #[serde(default)]
    match_hosts: Vec<String>,
    #[serde(default)]
    match_formats: Vec<String>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct LlmProviderCatalog {
    #[serde(default)]
    api_formats: Vec<LlmFormatMeta>,
    #[serde(default)]
    providers: Vec<LlmProviderMeta>,
}

fn llm_provider_catalog() -> &'static LlmProviderCatalog {
    static CATALOG: OnceLock<LlmProviderCatalog> = OnceLock::new();
    CATALOG.get_or_init(|| {
        serde_json::from_str::<LlmProviderCatalog>(LLM_PROVIDERS_JSON)
            .expect("embedded resources/llm_providers.json must be valid JSON")
    })
}

pub fn infer_llm_api_format(api_base: &str, provider: &str) -> String {
    let prov = provider.trim().to_ascii_lowercase();
    match prov.as_str() {
        "anthropic" | "claude" => return "anthropic".to_string(),
        "gemini" => return "gemini".to_string(),
        "azure" => return "azure".to_string(),
        "ollama" => return "ollama".to_string(),
        _ => {}
    }
    let base = api_base.trim().trim_end_matches('/').to_ascii_lowercase();
    if base.ends_with("/messages") || base.contains("api.anthropic.com") {
        "anthropic".to_string()
    } else if base.contains(":generatecontent")
        || (base.contains("generativelanguage.googleapis.com") && !base.contains("/openai"))
    {
        "gemini".to_string()
    } else if base.contains(".openai.azure.com") || base.contains("/openai/deployments/") {
        "azure".to_string()
    } else if base.ends_with("/api/chat") {
        "ollama".to_string()
    } else if base.ends_with("/responses") {
        "openai-responses".to_string()
    } else {
        "openai".to_string()
    }
}

pub fn infer_llm_provider(api_base: &str, api_format: &str) -> String {
    let base = api_base.trim().to_ascii_lowercase();
    let fmt = api_format.trim().to_ascii_lowercase();
    let catalog = llm_provider_catalog();
    for prov in &catalog.providers {
        if prov
            .match_hosts
            .iter()
            .any(|h| base.contains(&h.to_ascii_lowercase()))
        {
            return prov.id.clone();
        }
    }
    for prov in &catalog.providers {
        if prov
            .match_formats
            .iter()
            .any(|f| fmt == f.to_ascii_lowercase())
        {
            return prov.id.clone();
        }
    }
    "custom".to_string()
}

fn normalize_positive(value: f64, fallback: f64) -> f64 {
    if value.is_finite() && value > 0.0 {
        value
    } else {
        fallback
    }
}

fn normalize_question_bank_url(raw: &str) -> String {
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        default_question_bank_url()
    } else {
        trimmed.to_string()
    }
}

fn normalize_llm_api_format(raw: &str, api_base: &str, provider: &str) -> String {
    let fmt = raw.trim().to_ascii_lowercase();
    if llm_provider_catalog()
        .api_formats
        .iter()
        .any(|f| f.id.eq_ignore_ascii_case(&fmt))
    {
        fmt
    } else {
        infer_llm_api_format(api_base, provider)
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct LlmConfig {
    #[serde(default)]
    pub provider: String,
    #[serde(default, alias = "api_format")]
    pub api_format: String,
    #[serde(default, alias = "api_key")]
    pub api_key: String,
    #[serde(default = "default_llm_api_base", alias = "api_base")]
    pub api_base: String,
    #[serde(default = "default_llm_model")]
    pub model: String,
    #[serde(default, alias = "vision_model")]
    pub vision_model: String,
    #[serde(default, alias = "api_version")]
    pub api_version: String,
    #[serde(default, alias = "auto_submit")]
    pub auto_submit: bool,
    #[serde(default = "default_stream_timeout", alias = "stream_timeout")]
    pub stream_timeout: f64,
    #[serde(default = "default_stream_idle_timeout", alias = "stream_idle_timeout")]
    pub stream_idle_timeout: f64,
    #[serde(default, alias = "question_bank_token")]
    pub question_bank_token: String,
    #[serde(default = "default_question_bank_url", alias = "question_bank_url")]
    pub question_bank_url: String,
    #[serde(default = "default_question_bank_scopes", alias = "question_bank_scopes")]
    pub question_bank_scopes: Vec<String>,
}

impl Default for LlmConfig {
    fn default() -> Self {
        Self {
            provider: default_llm_provider(),
            api_format: default_llm_api_format(),
            api_key: String::new(),
            api_base: default_llm_api_base(),
            model: default_llm_model(),
            vision_model: String::new(),
            api_version: String::new(),
            auto_submit: false,
            stream_timeout: default_stream_timeout(),
            stream_idle_timeout: default_stream_idle_timeout(),
            question_bank_token: String::new(),
            question_bank_url: default_question_bank_url(),
            question_bank_scopes: default_question_bank_scopes(),
        }
    }
}

impl LlmConfig {
    pub fn normalized(&self) -> Self {
        let base_trim = self.api_base.trim();
        let api_base = if base_trim.is_empty() {
            default_llm_api_base()
        } else {
            base_trim.to_string()
        };
        let model_trim = self.model.trim();
        let model = if model_trim.is_empty() {
            default_llm_model()
        } else {
            model_trim.to_string()
        };
        let raw_provider = self.provider.trim().to_ascii_lowercase();
        let api_format = normalize_llm_api_format(&self.api_format, &api_base, &raw_provider);
        let provider = if raw_provider.is_empty() {
            infer_llm_provider(&api_base, &api_format)
        } else {
            raw_provider
        };
        let stream_timeout = normalize_positive(self.stream_timeout, default_stream_timeout());
        let stream_idle_timeout =
            normalize_positive(self.stream_idle_timeout, default_stream_idle_timeout())
                .min(stream_timeout);
        Self {
            provider,
            api_format,
            api_key: self.api_key.trim().to_string(),
            api_base,
            model,
            vision_model: self.vision_model.trim().to_string(),
            api_version: self.api_version.trim().to_string(),
            auto_submit: self.auto_submit,
            stream_timeout,
            stream_idle_timeout,
            question_bank_token: self.question_bank_token.trim().to_string(),
            question_bank_url: normalize_question_bank_url(&self.question_bank_url),
            question_bank_scopes: normalize_question_bank_scopes(&self.question_bank_scopes),
        }
    }

    pub fn to_disk_value(&self) -> serde_json::Value {
        let norm = self.normalized();
        serde_json::json!({
            "provider": norm.provider,
            "api_format": norm.api_format,
            "api_key": norm.api_key,
            "api_base": norm.api_base,
            "model": norm.model,
            "vision_model": norm.vision_model,
            "api_version": norm.api_version,
            "auto_submit": norm.auto_submit,
            "stream_timeout": norm.stream_timeout,
            "stream_idle_timeout": norm.stream_idle_timeout,
            "question_bank_token": norm.question_bank_token,
            "question_bank_url": norm.question_bank_url,
            "question_bank_scopes": norm.question_bank_scopes,
        })
    }
}
