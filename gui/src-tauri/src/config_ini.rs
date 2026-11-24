use crate::browser_detect::{detect_installed_browsers, resolve_browser_choice};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, HashSet};
use std::fs;
use std::path::Path;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct CourseItem {
    pub id: String,
    pub title: String,
    pub url: String,
    pub enabled: bool,
    #[serde(default)]
    pub teacher: String,
    #[serde(default)]
    pub school: String,
    #[serde(default)]
    pub progress: String,
    #[serde(default)]
    pub course_type: String,
    #[serde(default)]
    pub auto_detected: bool,
}

fn default_true() -> bool {
    true
}

fn default_mastery_target() -> f64 {
    100.0
}

fn default_notification_provider() -> String {
    "Windows".to_string()
}

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

fn default_homework_threshold() -> f64 {
    100.0
}

fn default_homework_max_redo() -> i64 {
    3
}

fn default_log_max_size_mb() -> i64 {
    10
}

fn default_log_retention_days() -> i64 {
    14
}

fn default_log_max_files() -> i64 {
    200
}

fn default_log_compress_after_days() -> i64 {
    3
}

const LLM_PROVIDERS_JSON: &str = include_str!("../../../resources/llm_providers.json");

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
    static CATALOG: std::sync::OnceLock<LlmProviderCatalog> = std::sync::OnceLock::new();
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
        })
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct AutovisorConfig {
    pub username: String,
    pub password: String,
    #[serde(default)]
    pub qr_login: bool,
    #[serde(default)]
    pub qr_terminal: bool,
    #[serde(default)]
    pub qr_unicode: bool,
    #[serde(default)]
    pub qr_save_dir: String,
    pub driver: String,
    pub exe_path: String,
    #[serde(default)]
    pub proxy: String,
    pub attach_existing_chrome: bool,
    pub cdp_url: String,
    pub enable_auto_captcha: bool,
    #[serde(default = "default_true")]
    pub enable_hide_window: bool,
    #[serde(default)]
    pub shutdown_on_complete: bool,
    #[serde(default)]
    pub enable_debug_log: bool,
    #[serde(default = "default_notification_provider")]
    pub notification_provider: String,
    #[serde(default)]
    pub notification_url: String,
    #[serde(default)]
    pub notification_tg_chat_id: String,
    #[serde(default = "default_true")]
    pub notify_on_verify: bool,
    #[serde(default = "default_true")]
    pub notify_on_finish: bool,
    #[serde(default = "default_true")]
    pub notify_on_error: bool,
    pub limit_max_time: f64,
    pub limit_speed: f64,
    pub sound_off: bool,
    #[serde(default)]
    pub review_mode: bool,
    #[serde(default = "default_true")]
    pub enable_exam_api: bool,
    #[serde(default)]
    pub enable_review_exam_api: bool,
    #[serde(default = "default_true")]
    pub enable_zhidao_quiz: bool,
    #[serde(default = "default_true")]
    pub enable_mastery: bool,
    #[serde(default = "default_mastery_target")]
    pub mastery_target: f64,
    #[serde(default = "default_true")]
    pub enable_ppt_reference: bool,
    #[serde(default = "default_true")]
    pub enable_popup_intercept: bool,
    #[serde(default = "default_true")]
    pub enable_homework_api: bool,
    #[serde(default = "default_homework_threshold")]
    pub homework_threshold: f64,
    #[serde(default = "default_homework_max_redo")]
    pub homework_max_redo: i64,
    #[serde(default = "default_true")]
    pub enable_formal_exam_api: bool,
    #[serde(default)]
    pub force_redo_answered: bool,
    #[serde(default = "default_log_max_size_mb")]
    pub log_max_size_mb: i64,
    #[serde(default = "default_log_retention_days")]
    pub log_retention_days: i64,
    #[serde(default = "default_log_max_files")]
    pub log_max_files: i64,
    #[serde(default = "default_log_compress_after_days")]
    pub log_compress_after_days: i64,
    #[serde(default)]
    pub llm: LlmConfig,
    pub courses: Vec<CourseItem>,
    pub mirrors: BTreeMap<String, String>,
}

impl Default for AutovisorConfig {
    fn default() -> Self {
        let mut mirrors = BTreeMap::new();
        mirrors.insert(
            "Huawei".to_string(),
            "https://mirrors.huaweicloud.com/repository/pypi".to_string(),
        );
        mirrors.insert(
            "Aliyun".to_string(),
            "https://mirrors.aliyun.com/pypi".to_string(),
        );
        mirrors.insert(
            "Tsinghua".to_string(),
            "https://pypi.tuna.tsinghua.edu.cn".to_string(),
        );
        mirrors.insert("Official".to_string(), "https://pypi.org".to_string());

        Self {
            username: String::new(),
            password: String::new(),
            qr_login: false,
            qr_terminal: false,
            qr_unicode: false,
            qr_save_dir: String::new(),
            driver: "Edge".to_string(),
            exe_path: String::new(),
            proxy: String::new(),
            attach_existing_chrome: false,
            cdp_url: "http://127.0.0.1:9222".to_string(),
            enable_auto_captcha: true,
            enable_hide_window: true,
            shutdown_on_complete: false,
            enable_debug_log: false,
            notification_provider: default_notification_provider(),
            notification_url: String::new(),
            notification_tg_chat_id: String::new(),
            notify_on_verify: true,
            notify_on_finish: true,
            notify_on_error: true,
            limit_max_time: 0.0,
            limit_speed: 1.0,
            sound_off: true,
            review_mode: false,
            enable_exam_api: true,
            enable_review_exam_api: false,
            enable_zhidao_quiz: true,
            enable_mastery: true,
            mastery_target: 100.0,
            enable_ppt_reference: true,
            enable_popup_intercept: true,
            enable_homework_api: true,
            homework_threshold: 100.0,
            homework_max_redo: 3,
            enable_formal_exam_api: true,
            force_redo_answered: false,
            log_max_size_mb: 10,
            log_retention_days: 14,
            log_max_files: 200,
            log_compress_after_days: 3,
            llm: LlmConfig::default(),
            courses: Vec::new(),
            mirrors,
        }
    }
}

fn parse_bool(raw: &str, default: bool) -> bool {
    match raw.trim().to_ascii_lowercase().as_str() {
        "true" | "1" | "yes" => true,
        "false" | "0" | "no" => false,
        _ => default,
    }
}

fn normalize_driver(raw: &str) -> String {
    match raw.trim().to_ascii_lowercase().as_str() {
        "chrome" => "Chrome".to_string(),
        "chromium" => "Chromium".to_string(),
        _ => "Edge".to_string(),
    }
}

fn normalize_notification_provider(raw: &str) -> String {
    match raw.trim().to_ascii_lowercase().as_str() {
        "windows" => "Windows".to_string(),
        "serverchan" => "ServerChan".to_string(),
        "qmsg" => "Qmsg".to_string(),
        "bark" => "Bark".to_string(),
        "telegram" => "Telegram".to_string(),
        _ => String::new(),
    }
}

fn is_https_url(raw: &str) -> bool {
    let s = raw.trim();
    s.starts_with("https://") && s.len() > 10 && !s.contains(char::is_whitespace)
}

fn infer_title(url: &str, idx: usize) -> String {
    let lower = url.to_ascii_lowercase();
    let kind = if lower.contains("/dohomework/") {
        "知到作业"
    } else if lower.contains("/doexam/") {
        "知到考试"
    } else if lower.contains("/testdetail/") {
        "AI 测试详情"
    } else if lower.contains("ai-smart-course-student-pro.zhihuishu.com") {
        "AI 智慧课"
    } else if lower.contains("wisdom-mooc.zhihuishu.com") {
        "AI 慕课"
    } else if lower.contains("fusioncourseh5.zhihuishu.com") {
        "融合共享课"
    } else if lower.contains("studywisdomh5.zhihuishu.com") || lower.contains("hike.zhihuishu.com") {
        "翻转 / 智慧课"
    } else if lower.contains("studyvideoh5.zhihuishu.com") {
        "智慧共享课"
    } else {
        "智慧树课程"
    };
    format!("{kind} {idx}")
}

fn sanitize_ini_value(raw: &str, should_trim: bool) -> String {
    let cleaned: String = raw
        .chars()
        .filter(|&c| !matches!(c, '\r' | '\n' | '\0'))
        .collect();
    if should_trim {
        cleaned.trim().to_string()
    } else {
        cleaned
    }
}

fn format_bool(v: bool) -> &'static str {
    if v { "True" } else { "False" }
}

fn format_number(v: f64) -> String {
    if (v - v.round()).abs() < 1e-6 {
        format!("{}", v.round() as i64)
    } else {
        format!("{:.1}", v)
    }
}

pub fn parse_ini_str(content: &str) -> (AutovisorConfig, Vec<String>) {
    let mut cfg = AutovisorConfig::default();
    let mut urls = Vec::new();
    let mut section = String::new();
    let mut has_notification_provider = false;

    for raw_line in content.trim_start_matches('\u{feff}').lines() {
        let raw_no_cr = raw_line.trim_end_matches('\r');
        let line = raw_no_cr.trim();
        if line.is_empty() || line.starts_with(';') || line.starts_with('#') {
            continue;
        }
        if line.starts_with('[') && line.ends_with(']') {
            section = line[1..line.len() - 1].trim().to_ascii_lowercase();
            continue;
        }
        let Some((k, raw_v)) = raw_no_cr.split_once('=') else {
            continue;
        };
        let key = k.trim().to_ascii_lowercase();
        let val = raw_v.trim().to_string();

        match section.as_str() {
            "user-account" => match key.as_str() {
                "username" => cfg.username = val,
                "password" => {
                    // 保留密码可能包含的前导/尾随空格，仅剥离等号后单个标准分隔空格
                    let preserved = raw_v.strip_prefix(' ').unwrap_or(raw_v);
                    cfg.password = sanitize_ini_value(preserved, false);
                }
                "qrlogin" => cfg.qr_login = parse_bool(&val, false),
                "qrterminal" => cfg.qr_terminal = parse_bool(&val, false),
                "qrunicode" => cfg.qr_unicode = parse_bool(&val, false),
                "qrsavedir" => cfg.qr_save_dir = val,
                _ => {}
            },
            "browser-option" => match key.as_str() {
                "driver" => cfg.driver = normalize_driver(&val),
                "exe_path" => cfg.exe_path = val,
                "proxy" => cfg.proxy = val,
                "attachexistingchrome" => {
                    cfg.attach_existing_chrome = parse_bool(&val, false);
                }
                "cdpurl" => {
                    if !val.is_empty() {
                        cfg.cdp_url = val;
                    }
                }
                _ => {}
            },
            "script-option" => match key.as_str() {
                "enableautocaptcha" => cfg.enable_auto_captcha = parse_bool(&val, true),
                "enablehidewindow" => cfg.enable_hide_window = parse_bool(&val, true),
                "shutdownoncomplete" => cfg.shutdown_on_complete = parse_bool(&val, false),
                "enabledebuglog" => cfg.enable_debug_log = parse_bool(&val, false),
                "log_max_size_mb" => {
                    if let Ok(num) = val.parse::<i64>() {
                        if num > 0 {
                            cfg.log_max_size_mb = num;
                        }
                    }
                }
                "log_retention_days" => {
                    if let Ok(num) = val.parse::<i64>() {
                        if num >= 0 {
                            cfg.log_retention_days = num;
                        }
                    }
                }
                "log_max_files" => {
                    if let Ok(num) = val.parse::<i64>() {
                        if num > 0 {
                            cfg.log_max_files = num;
                        }
                    }
                }
                "log_compress_after_days" => {
                    if let Ok(num) = val.parse::<i64>() {
                        if num >= 0 {
                            cfg.log_compress_after_days = num;
                        }
                    }
                }
                _ => {}
            },
            "notification" => match key.as_str() {
                "provider" => {
                    has_notification_provider = true;
                    cfg.notification_provider = normalize_notification_provider(&val);
                }
                "url" => cfg.notification_url = val,
                "tg_chat_id" | "tgchatid" => cfg.notification_tg_chat_id = val,
                "barkurl" => {
                    if (!has_notification_provider || cfg.notification_provider.is_empty())
                        && cfg.notification_url.is_empty()
                        && !val.is_empty()
                    {
                        cfg.notification_provider = "Bark".to_string();
                        cfg.notification_url = val;
                    }
                }
                "notifyonverify" => cfg.notify_on_verify = parse_bool(&val, true),
                "notifyonfinish" => cfg.notify_on_finish = parse_bool(&val, true),
                "notifyonerror" => cfg.notify_on_error = parse_bool(&val, true),
                _ => {}
            },
            "course-option" => match key.as_str() {
                "limitmaxtime" => {
                    if let Ok(num) = val.parse::<f64>() {
                        if num.is_finite() && num >= 0.0 {
                            cfg.limit_max_time = num;
                        }
                    }
                }
                "limitspeed" => {
                    if let Ok(num) = val.parse::<f64>() {
                        if num.is_finite() {
                            cfg.limit_speed = (num.clamp(0.5, 1.8) * 10.0).round() / 10.0;
                        }
                    }
                }
                "soundoff" => cfg.sound_off = parse_bool(&val, true),
                "reviewmode" => cfg.review_mode = parse_bool(&val, false),
                "enableexamapi" => cfg.enable_exam_api = parse_bool(&val, true),
                "enablereviewexamapi" => {
                    cfg.enable_review_exam_api = parse_bool(&val, false);
                }
                "enablezhidaoquiz" => cfg.enable_zhidao_quiz = parse_bool(&val, true),
                "enablemastery" => cfg.enable_mastery = parse_bool(&val, true),
                "masterytarget" => {
                    if let Ok(num) = val.parse::<f64>() {
                        if num.is_finite() && num > 0.0 {
                            cfg.mastery_target = num.clamp(1.0, 100.0);
                        }
                    }
                }
                "enablepptreference" => cfg.enable_ppt_reference = parse_bool(&val, true),
                "enablepopupintercept" => {
                    cfg.enable_popup_intercept = parse_bool(&val, true);
                }
                "enablehomeworkapi" => cfg.enable_homework_api = parse_bool(&val, true),
                "homeworkthreshold" => {
                    if let Ok(num) = val.parse::<f64>() {
                        if num.is_finite() {
                            cfg.homework_threshold = num.clamp(0.0, 100.0);
                        }
                    }
                }
                "homeworkmaxredo" => {
                    if let Ok(num) = val.parse::<i64>() {
                        if num >= 0 {
                            cfg.homework_max_redo = num;
                        }
                    }
                }
                "enableformalexamapi" => {
                    cfg.enable_formal_exam_api = parse_bool(&val, true);
                }
                "forceredoanswered" => {
                    cfg.force_redo_answered = parse_bool(&val, false);
                }
                _ => {}
            },
            "course-url" => {
                if is_https_url(&val) && !urls.contains(&val) {
                    urls.push(val);
                }
            }
            _ => {}
        }
    }

    (cfg, urls)
}

pub fn render_ini_str(cfg: &AutovisorConfig) -> String {
    let cdp_clean = sanitize_ini_value(&cfg.cdp_url, true);
    let mut lines = vec![
        "[user-account]".to_string(),
        ";配置账号密码，留空则打开网页后需手动登录".to_string(),
        format!("username = {}", sanitize_ini_value(&cfg.username, true)),
        format!("password = {}", sanitize_ini_value(&cfg.password, false)),
        ";可选: 启用终端二维码登录(默认:False)".to_string(),
        format!("qrLogin = {}", format_bool(cfg.qr_login)),
        format!("qrTerminal = {}", format_bool(cfg.qr_terminal)),
        format!("qrUnicode = {}", format_bool(cfg.qr_unicode)),
        format!("qrSaveDir = {}", sanitize_ini_value(&cfg.qr_save_dir, true)),
        String::new(),
        "[browser-option]".to_string(),
        ";配置浏览器,可选 Edge 或 Chrome".to_string(),
        format!("driver = {}", normalize_driver(&cfg.driver)),
        ";指定浏览器所在路径,不填则使用默认路径".to_string(),
        format!("EXE_PATH = {}", sanitize_ini_value(&cfg.exe_path, true)),
        ";可选: 代理服务器地址 (支持 http:// / https:// / socks5://)".to_string(),
        format!("proxy = {}", sanitize_ini_value(&cfg.proxy, true)),
        format!(
            "attachExistingChrome = {}",
            format_bool(cfg.attach_existing_chrome)
        ),
        format!(
            "cdpUrl = {}",
            if cdp_clean.is_empty() {
                "http://127.0.0.1:9222".to_string()
            } else {
                cdp_clean
            }
        ),
        String::new(),
        "[script-option]".to_string(),
        ";是否自动过验证码(登录滑块[易盾/腾讯] / 课中点选, 默认:True)".to_string(),
        format!("enableAutoCaptcha = {}", format_bool(cfg.enable_auto_captcha)),
        ";是否自动隐藏浏览器窗口(默认:True)".to_string(),
        format!("enableHideWindow = {}", format_bool(cfg.enable_hide_window)),
        ";刷课任务完成后自动关机(默认:False, 仅正常完成或达到课程时限时倒计时60秒关机, 手动停止/出错不关机)".to_string(),
        format!("shutdownOnComplete = {}", format_bool(cfg.shutdown_on_complete)),
        ";是否在桌面端实时输出 DEBUG 调试日志(默认:False)".to_string(),
        format!("enableDebugLog = {}", format_bool(cfg.enable_debug_log)),
        ";单个日志文件体积上限/MB, 超过后轮转 (默认:10)".to_string(),
        format!("log_max_size_mb = {}", cfg.log_max_size_mb.max(1)),
        ";日志保留天数, 过期归档清理 (默认:14)".to_string(),
        format!("log_retention_days = {}", cfg.log_retention_days.max(0)),
        ";日志文件最大保留数量 (默认:200)".to_string(),
        format!("log_max_files = {}", cfg.log_max_files.max(1)),
        ";日志归档多少天后压缩 (默认:3)".to_string(),
        format!(
            "log_compress_after_days = {}",
            cfg.log_compress_after_days.max(0)
        ),
        String::new(),
        "[notification]".to_string(),
        ";通知服务商（默认:Windows，留空表示禁用通知）。可选值: Windows / ServerChan / Qmsg / Bark / Telegram"
            .to_string(),
        format!(
            "provider = {}",
            normalize_notification_provider(&cfg.notification_provider)
        ),
        format!("url = {}", sanitize_ini_value(&cfg.notification_url, true)),
        format!(
            "tg_chat_id = {}",
            sanitize_ini_value(&cfg.notification_tg_chat_id, true)
        ),
        format!("notifyOnVerify = {}", format_bool(cfg.notify_on_verify)),
        format!("notifyOnFinish = {}", format_bool(cfg.notify_on_finish)),
        format!("notifyOnError = {}", format_bool(cfg.notify_on_error)),
        String::new(),
        "[course-option]".to_string(),
        ";限制每门课程刷课时长/min (不限制:0)".to_string(),
        format!("limitMaxTime = {}", format_number(cfg.limit_max_time.max(0.0))),
        ";限定播放倍速 (最高:1.8)".to_string(),
        format!(
            "limitSpeed = {:.1}",
            cfg.limit_speed.clamp(0.5, 1.8)
        ),
        ";设置是否静音播放 (默认:True)".to_string(),
        format!("soundOff = {}", format_bool(cfg.sound_off)),
        ";复习模式/刷习惯分: 忽略已完成状态重播视频, 设置 limitMaxTime 时循环刷满时长 (默认:False)"
            .to_string(),
        format!("reviewMode = {}", format_bool(cfg.review_mode)),
        ";AI智慧课: 是否先用接口版 ExamCtx 刷计分考试并交卷 (默认:True)".to_string(),
        format!("enableExamApi = {}", format_bool(cfg.enable_exam_api)),
        ";复习/模拟考试页: 是否捕获新页面并用接口直接作答交卷 (默认:False, 正式考试勿开)"
            .to_string(),
        format!(
            "enableReviewExamApi = {}",
            format_bool(cfg.enable_review_exam_api)
        ),
        ";课中弹题: 是否优先用智慧树 AI 作答, 失败回退自定义大模型 (默认:True)".to_string(),
        format!("enableZhidaoQuiz = {}", format_bool(cfg.enable_zhidao_quiz)),
        format!("enableMastery = {}", format_bool(cfg.enable_mastery)),
        format!(
            "masteryTarget = {}",
            format_number(cfg.mastery_target.clamp(1.0, 100.0))
        ),
        ";弹题响应监听: 只读捕获官方 AI 答案, 未命中回退大模型 (默认:True)".to_string(),
        format!(
            "enablePopupIntercept = {}",
            format_bool(cfg.enable_popup_intercept)
        ),
        ";知到作业: 是否自动刷作业并按目标分重做错题 (默认:True)".to_string(),
        format!("enableHomeworkApi = {}", format_bool(cfg.enable_homework_api)),
        ";作业达标得分率 (0-100, 默认:100)".to_string(),
        format!(
            "homeworkThreshold = {}",
            format_number(cfg.homework_threshold.clamp(0.0, 100.0))
        ),
        ";作业错题重做最大轮数 (默认:3)".to_string(),
        format!("homeworkMaxRedo = {}", cfg.homework_max_redo.max(0)),
        ";正式考试: 是否用接口自动作答交卷, 交卷受 llm.json 的 auto_submit 控制 (默认:True)"
            .to_string(),
        format!(
            "enableFormalExamApi = {}",
            format_bool(cfg.enable_formal_exam_api)
        ),
        ";考试参考资料: 自动抓取课程知识点 PPT 作为答题参考 (默认:True)".to_string(),
        format!(
            "enablePptReference = {}",
            format_bool(cfg.enable_ppt_reference)
        ),
        ";答题时遇到已有保存答案的题目是否强制重新作答 (默认:False)".to_string(),
        format!(
            "forceRedoAnswered = {}",
            format_bool(cfg.force_redo_answered)
        ),
        String::new(),
        "[course-url]".to_string(),
        ";配置要学习的课程链接,支持智慧共享课、翻转课、融合共享课、AI慕课(wisdom-mooc)、AI智慧课".to_string(),
    ];

    let enabled_urls: Vec<String> = cfg
        .courses
        .iter()
        .filter(|c| c.enabled && is_https_url(&c.url))
        .map(|c| sanitize_ini_value(&c.url, true))
        .filter(|u| !u.is_empty())
        .collect();

    if enabled_urls.is_empty() {
        lines.push("URL1 =".to_string());
    } else {
        for (idx, url) in enabled_urls.iter().enumerate() {
            lines.push(format!("URL{} = {}", idx + 1, url));
        }
    }

    lines.join("\r\n") + "\r\n"
}

pub fn load_config_from_dir(base_dir: &Path) -> Result<AutovisorConfig, String> {
    let config_path = base_dir.join("config.ini");
    let (mut cfg, ini_urls) = match fs::read(&config_path) {
        Ok(bytes) => {
            if bytes.contains(&0) {
                return Err("config.ini 文件已损坏（包含非法空字节）".to_string());
            }
            let text = String::from_utf8_lossy(&bytes).to_string();
            parse_ini_str(&text)
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            (AutovisorConfig::default(), Vec::new())
        }
        Err(e) => return Err(format!("读取 config.ini 失败: {e}")),
    };

    // Merge companion metadata (gui_courses.json) to preserve custom titles and unchecked courses
    let meta_path = base_dir.join("data").join("gui_courses.json");
    let saved_courses: Vec<CourseItem> = match fs::read_to_string(&meta_path) {
        Ok(raw) => serde_json::from_str::<Vec<CourseItem>>(raw.trim_start_matches('\u{feff}'))
            .map_err(|e| format!("解析 gui_courses.json 失败: {e}"))?,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Vec::new(),
        Err(e) => return Err(format!("读取 gui_courses.json 失败: {e}")),
    };

    let ini_set: HashSet<String> = ini_urls.iter().cloned().collect();
    let mut merged: Vec<CourseItem> = Vec::new();
    let mut seen: HashSet<String> = HashSet::new();

    for mut item in saved_courses {
        let trimmed = item.url.trim().to_string();
        if !is_https_url(&trimmed) || seen.contains(&trimmed) {
            continue;
        }
        seen.insert(trimmed.clone());
        item.url = trimmed.clone();
        // If user manually added/removed URLs in config.ini, respect config.ini enabled state when ini has URLs
        if !ini_urls.is_empty() {
            item.enabled = ini_set.contains(&trimmed);
        }
        merged.push(item);
    }

    for url in ini_urls {
        if seen.insert(url.clone()) {
            let idx = merged.len() + 1;
            merged.push(CourseItem {
                id: format!("course-{idx}"),
                title: infer_title(&url, idx),
                url,
                enabled: true,
                teacher: String::new(),
                school: String::new(),
                progress: String::new(),
                course_type: String::new(),
                auto_detected: false,
            });
        }
    }
    cfg.courses = merged;

    // Auto-select an installed browser on this device when exe_path is empty or invalid
    let detected = detect_installed_browsers();
    if let Some((driver, exe_path)) =
        resolve_browser_choice(&cfg.driver, &cfg.exe_path, &detected)
    {
        cfg.driver = driver;
        cfg.exe_path = exe_path;
    }

    // Load mirrors.json
    let mirrors_path = base_dir.join("data").join("mirrors.json");
    match fs::read_to_string(&mirrors_path) {
        Ok(raw) => {
            let parsed = serde_json::from_str::<BTreeMap<String, String>>(
                raw.trim_start_matches('\u{feff}'),
            )
            .map_err(|e| format!("解析 mirrors.json 失败: {e}"))?;
            if !parsed.is_empty() {
                cfg.mirrors = parsed;
            }
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
        Err(e) => return Err(format!("读取 mirrors.json 失败: {e}")),
    }

    // Load data/llm.json (universal LLM quiz solver)
    let llm_path = base_dir.join("data").join("llm.json");
    match fs::read_to_string(&llm_path) {
        Ok(raw) => {
            let parsed =
                serde_json::from_str::<LlmConfig>(raw.trim_start_matches('\u{feff}'))
                    .map_err(|e| format!("解析 llm.json 失败: {e}"))?;
            cfg.llm = parsed.normalized();
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
        Err(e) => return Err(format!("读取 llm.json 失败: {e}")),
    }

    Ok(cfg)
}

fn canonical_course_url(raw: &str) -> String {
    let mut s = raw.trim().to_string();
    if s.ends_with("#/") {
        s.truncate(s.len() - 2);
    } else if s.ends_with('#') {
        s.truncate(s.len() - 1);
    }
    s
}

fn is_generic_title(title: &str) -> bool {
    let t = title.trim();
    t.is_empty()
        || matches!(
            t,
            "智慧共享课"
                | "融合共享课"
                | "翻转 / 智慧课"
                | "校内学分课"
                | "AI 慕课"
                | "AI 智慧课"
                | "智慧树课程"
                | "进入学习"
                | "继续学习"
                | "开始学习"
                | "去学习"
                | "立即学习"
                | "查看详情"
                | "进入课程"
                | "开始上课"
        )
}

pub fn merge_discovered_courses(
    existing: &[CourseItem],
    discovered: Vec<CourseItem>,
) -> Vec<CourseItem> {
    let mut by_url: std::collections::HashMap<String, &CourseItem> =
        std::collections::HashMap::new();
    for item in existing {
        by_url.insert(canonical_course_url(&item.url), item);
    }

    let mut result = Vec::new();
    let mut seen = HashSet::new();

    for mut item in discovered {
        let trimmed = canonical_course_url(&item.url);
        if !is_https_url(&trimmed) || !seen.insert(trimmed.clone()) {
            continue;
        }
        item.url = trimmed.clone();
        item.auto_detected = true;
        if let Some(prev) = by_url.get(&trimmed) {
            item.id = prev.id.clone();
            item.enabled = prev.enabled;
            if is_generic_title(&item.title) && !is_generic_title(&prev.title) {
                item.title = prev.title.clone();
            }
            if item.teacher.trim().is_empty() && !prev.teacher.trim().is_empty() {
                item.teacher = prev.teacher.clone();
            }
            if item.school.trim().is_empty() && !prev.school.trim().is_empty() {
                item.school = prev.school.clone();
            }
            if item.progress.trim().is_empty() && !prev.progress.trim().is_empty() {
                item.progress = prev.progress.clone();
            }
            if item.course_type.trim().is_empty() && !prev.course_type.trim().is_empty() {
                item.course_type = prev.course_type.clone();
            }
        }
        result.push(item);
    }

    // Keep any manually added custom courses that aren't in the auto-discovered set
    for prev in existing {
        let trimmed = canonical_course_url(&prev.url);
        if !prev.auto_detected && is_https_url(&trimmed) && seen.insert(trimmed.clone()) {
            let mut kept = prev.clone();
            kept.url = trimmed;
            result.push(kept);
        }
    }

    result
}

fn atomic_io_lock() -> &'static std::sync::Mutex<()> {
    static LOCK: std::sync::OnceLock<std::sync::Mutex<()>> = std::sync::OnceLock::new();
    LOCK.get_or_init(|| std::sync::Mutex::new(()))
}

pub fn write_file_atomic(target: &Path, content: &[u8], label: &str) -> Result<(), String> {
    use std::io::Write;

    let _guard = atomic_io_lock().lock().unwrap_or_else(|e| e.into_inner());
    let parent = target
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."));
    fs::create_dir_all(parent).map_err(|e| format!("创建 {label} 目录失败: {e}"))?;

    let mut tmp = tempfile::Builder::new()
        .prefix(".tmp-autovisor-")
        .tempfile_in(parent)
        .map_err(|e| format!("创建 {label} 临时文件失败: {e}"))?;

    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = tmp
            .as_file()
            .set_permissions(fs::Permissions::from_mode(0o600));
    }

    tmp.write_all(content)
        .and_then(|_| tmp.as_file().sync_all())
        .map_err(|e| format!("写入 {label} 失败: {e}"))?;

    tmp.persist(target)
        .map_err(|e| format!("保存 {label} 失败: {}", e.error))?;
    Ok(())
}

pub fn save_config_to_dir(base_dir: &Path, cfg: &AutovisorConfig) -> Result<AutovisorConfig, String> {
    let config_path = base_dir.join("config.ini");
    let ini_text = render_ini_str(cfg);
    write_file_atomic(&config_path, ini_text.as_bytes(), "config.ini")?;

    let data_dir = base_dir.join("data");
    let _ = fs::create_dir_all(&data_dir);

    let meta_path = data_dir.join("gui_courses.json");
    let meta_json = serde_json::to_string_pretty(&cfg.courses)
        .map_err(|e| format!("序列化课程列表失败: {e}"))?;
    write_file_atomic(&meta_path, meta_json.as_bytes(), "gui_courses.json")?;

    if !cfg.mirrors.is_empty() {
        let mirrors_path = data_dir.join("mirrors.json");
        let mirrors_json = serde_json::to_string_pretty(&cfg.mirrors)
            .map_err(|e| format!("序列化镜像源失败: {e}"))?;
        write_file_atomic(&mirrors_path, mirrors_json.as_bytes(), "mirrors.json")?;
    }

    let llm_path = data_dir.join("llm.json");
    let llm_norm = cfg.llm.normalized();
    let llm_disk = llm_norm.to_disk_value();
    let llm_json = serde_json::to_string_pretty(&llm_disk)
        .map_err(|e| format!("序列化大模型配置失败: {e}"))?;
    let llm_text = format!("{llm_json}\n");
    write_file_atomic(&llm_path, llm_text.as_bytes(), "llm.json")?;

    // 在内存中构造规范化副本，避免每次保存都冗余重读磁盘和扫描本机浏览器
    let (mut normalized, _) = parse_ini_str(&ini_text);
    let mut cleaned_courses = Vec::with_capacity(cfg.courses.len());
    let mut seen_urls = HashSet::new();
    for item in &cfg.courses {
        let trimmed = item.url.trim().to_string();
        if !is_https_url(&trimmed) || !seen_urls.insert(trimmed.clone()) {
            continue;
        }
        let mut copy = item.clone();
        copy.url = trimmed;
        cleaned_courses.push(copy);
    }
    normalized.courses = cleaned_courses;
    if !cfg.mirrors.is_empty() {
        normalized.mirrors = cfg.mirrors.clone();
    }
    normalized.llm = llm_norm;
    Ok(normalized)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roundtrip_ini_preserves_all_fields_and_enabled_urls() {
        let mut cfg = AutovisorConfig::default();
        assert!(cfg.enable_hide_window);
        assert_eq!(cfg.notification_provider, "Windows");
        assert!((cfg.limit_max_time - 0.0).abs() < 1e-6);
        cfg.username = "13800138000".to_string();
        cfg.qr_login = true;
        cfg.qr_terminal = true;
        cfg.qr_unicode = true;
        cfg.qr_save_dir = "C:\\temp\\qr".to_string();
        cfg.driver = "Chrome".to_string();
        cfg.proxy = "http://127.0.0.1:7890".to_string();
        cfg.notification_provider = "Telegram".to_string();
        cfg.notification_url = "https://api.telegram.org/bot123:abc/sendMessage".to_string();
        cfg.notification_tg_chat_id = "123456789".to_string();
        cfg.notify_on_verify = false;
        cfg.limit_speed = 1.5;
        cfg.limit_max_time = 45.0;
        cfg.review_mode = true;
        cfg.enable_exam_api = false;
        cfg.enable_review_exam_api = true;
        cfg.enable_zhidao_quiz = false;
        cfg.enable_mastery = false;
        cfg.mastery_target = 85.0;
        cfg.enable_hide_window = false;
        cfg.enable_debug_log = true;
        cfg.enable_ppt_reference = false;
        cfg.enable_popup_intercept = false;
        cfg.enable_homework_api = false;
        cfg.homework_threshold = 80.0;
        cfg.homework_max_redo = 5;
        cfg.enable_formal_exam_api = false;
        cfg.log_max_size_mb = 20;
        cfg.log_retention_days = 7;
        cfg.log_max_files = 99;
        cfg.log_compress_after_days = 2;
        cfg.courses = vec![
            CourseItem {
                id: "1".into(),
                title: "共享课".into(),
                url: "https://studyvideoh5.zhihuishu.com/stuStudy?id=1".into(),
                enabled: true,
                teacher: "张老师".into(),
                school: "示例大学".into(),
                progress: "42%".into(),
                course_type: "智慧共享课".into(),
                auto_detected: true,
            },
            CourseItem {
                id: "2".into(),
                title: "暂停课".into(),
                url: "https://wisdom-mooc.zhihuishu.com/play/2".into(),
                enabled: false,
                teacher: String::new(),
                school: String::new(),
                progress: String::new(),
                course_type: "AI 慕课".into(),
                auto_detected: true,
            },
        ];

        let rendered = render_ini_str(&cfg);
        let (parsed, urls) = parse_ini_str(&rendered);
        assert_eq!(parsed.username, "13800138000");
        assert!(parsed.qr_login);
        assert!(parsed.qr_terminal);
        assert!(parsed.qr_unicode);
        assert_eq!(parsed.qr_save_dir, "C:\\temp\\qr");
        assert_eq!(parsed.driver, "Chrome");
        assert_eq!(parsed.proxy, "http://127.0.0.1:7890");
        assert_eq!(parsed.notification_provider, "Telegram");
        assert_eq!(
            parsed.notification_url,
            "https://api.telegram.org/bot123:abc/sendMessage"
        );
        assert_eq!(parsed.notification_tg_chat_id, "123456789");
        assert!(!parsed.notify_on_verify);
        assert!(parsed.notify_on_finish);
        assert!((parsed.limit_speed - 1.5).abs() < 1e-6);
        assert!((parsed.limit_max_time - 45.0).abs() < 1e-6);
        assert!(parsed.review_mode);
        assert!(!parsed.enable_exam_api);
        assert!(parsed.enable_review_exam_api);
        assert!(!parsed.enable_zhidao_quiz);
        assert!(!parsed.enable_mastery);
        assert!((parsed.mastery_target - 85.0).abs() < 1e-6);
        assert!(!parsed.enable_hide_window);
        assert!(parsed.enable_debug_log);
        assert!(!parsed.enable_ppt_reference);
        assert!(!parsed.enable_popup_intercept);
        assert!(!parsed.enable_homework_api);
        assert!((parsed.homework_threshold - 80.0).abs() < 1e-6);
        assert_eq!(parsed.homework_max_redo, 5);
        assert!(!parsed.enable_formal_exam_api);
        assert_eq!(parsed.log_max_size_mb, 20);
        assert_eq!(parsed.log_retention_days, 7);
        assert_eq!(parsed.log_max_files, 99);
        assert_eq!(parsed.log_compress_after_days, 2);
        assert_eq!(urls, vec!["https://studyvideoh5.zhihuishu.com/stuStudy?id=1"]);
    }

    #[test]
    fn keeps_zero_log_limits_and_clamps_idle_timeout() {
        let mut cfg = AutovisorConfig::default();
        cfg.log_retention_days = 0;
        cfg.log_compress_after_days = 0;

        let rendered = render_ini_str(&cfg);
        assert!(rendered.contains("log_retention_days = 0"));
        assert!(rendered.contains("log_compress_after_days = 0"));

        let (parsed, _) = parse_ini_str(&rendered);
        assert_eq!(parsed.log_retention_days, 0);
        assert_eq!(parsed.log_compress_after_days, 0);

        let disk: LlmConfig = serde_json::from_value(serde_json::json!({
            "stream_timeout": 5.0,
            "stream_idle_timeout": 60.0,
        }))
        .unwrap();
        let llm = disk.normalized();
        assert!((llm.stream_idle_timeout - 5.0).abs() < 1e-6);
    }

    #[test]
    fn render_ini_sanitizes_newlines_and_preserves_password_spaces() {
        let mut cfg = AutovisorConfig::default();
        cfg.username = "user\r\n[script-option]\nenableAutoCaptcha = False".to_string();
        cfg.password = "  pass word  ".to_string();
        cfg.proxy = "http://127.0.0.1:7890\n[course-option]\nlimitSpeed = 9.9".to_string();
        let rendered = render_ini_str(&cfg);
        assert!(!rendered.lines().any(|l| l.trim() == "limitSpeed = 9.9"));
        assert!(!rendered.lines().any(|l| l.trim() == "enableAutoCaptcha = False"));
        let (parsed, _) = parse_ini_str(&rendered);
        assert_eq!(parsed.password, "  pass word  ");
        assert!(parsed.enable_auto_captcha);
        assert!((parsed.limit_speed - 1.0).abs() < 1e-6);
    }

    #[test]
    fn infers_homework_exam_testdetail_titles_and_shared_llm_providers() {
        assert_eq!(
            infer_title("https://zhihuishu.com/#/dohomework/1/2/3/4/5", 1),
            "知到作业 1"
        );
        assert_eq!(
            infer_title("https://zhihuishu.com/#/doexam/1/2/3/4/5", 2),
            "知到考试 2"
        );
        assert_eq!(
            infer_title("https://ai-smart-course-student-pro.zhihuishu.com/testDetail/1/2/3/4", 3),
            "AI 测试详情 3"
        );
        assert!(is_generic_title("进入课程"));
        assert!(is_generic_title("开始上课"));
        assert_eq!(infer_llm_provider("https://api.deepseek.com", "openai"), "deepseek");
        assert_eq!(infer_llm_provider("https://api.moonshot.ai/v1", "openai"), "moonshot");
        assert_eq!(infer_llm_provider("https://custom.relay.example", "anthropic"), "anthropic");
        assert_eq!(infer_llm_provider("http://127.0.0.1:11434", ""), "ollama");
    }

    #[test]
    fn roundtrips_llm_json_file() {
        let dir = std::env::temp_dir().join(format!(
            "autovisor_llm_test_{}",
            std::process::id()
        ));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();

        let mut cfg = AutovisorConfig::default();
        cfg.llm = LlmConfig {
            provider: "anthropic".to_string(),
            api_format: "anthropic".to_string(),
            api_key: "sk-ant-test-123".to_string(),
            api_base: "https://api.anthropic.com".to_string(),
            model: "claude-sonnet-4-20250514".to_string(),
            vision_model: "claude-sonnet-4-20250514".to_string(),
            api_version: "2023-06-01".to_string(),
            auto_submit: true,
            stream_timeout: 90.0,
            stream_idle_timeout: 20.0,
            question_bank_token: "bank-key".to_string(),
            question_bank_url: "https://tk.example/query".to_string(),
        };
        let saved = save_config_to_dir(&dir, &cfg).unwrap();
        assert_eq!(saved.llm, cfg.llm);

        let raw = fs::read_to_string(dir.join("data").join("llm.json")).unwrap();
        let disk: serde_json::Value = serde_json::from_str(&raw).unwrap();
        assert_eq!(disk["provider"], "anthropic");
        assert_eq!(disk["api_format"], "anthropic");
        assert_eq!(disk["api_key"], "sk-ant-test-123");
        assert_eq!(disk["api_base"], "https://api.anthropic.com");
        assert_eq!(disk["model"], "claude-sonnet-4-20250514");
        assert_eq!(disk["vision_model"], "claude-sonnet-4-20250514");
        assert_eq!(disk["api_version"], "2023-06-01");
        assert_eq!(disk["auto_submit"], true);
        assert_eq!(disk["stream_timeout"], 90.0);
        assert_eq!(disk["stream_idle_timeout"], 20.0);
        assert_eq!(disk["question_bank_token"], "bank-key");
        assert_eq!(disk["question_bank_url"], "https://tk.example/query");

        // Verify legacy 4-field llm.json still loads cleanly and infers provider/format
        fs::write(
            dir.join("data").join("llm.json"),
            r#"{"api_key":"sk-legacy","api_base":"https://api.openai.com/v1","model":"gpt-4o-mini","auto_submit":false}"#,
        )
        .unwrap();
        let legacy = load_config_from_dir(&dir).unwrap();
        assert_eq!(legacy.llm.provider, "openai");
        assert_eq!(legacy.llm.api_format, "openai");
        assert_eq!(legacy.llm.model, "gpt-4o-mini");

        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn merges_discovered_courses_preserving_selection_and_manual_links() {
        let existing = vec![
            CourseItem {
                id: "old-1".into(),
                title: "旧标题".into(),
                url: "https://studyvideoh5.zhihuishu.com/stuStudy?recruitAndCourseId=aaa".into(),
                enabled: false,
                teacher: String::new(),
                school: String::new(),
                progress: "10%".into(),
                course_type: String::new(),
                auto_detected: true,
            },
            CourseItem {
                id: "manual-1".into(),
                title: "手动课程".into(),
                url: "https://wisdom-mooc.zhihuishu.com/play/custom".into(),
                enabled: true,
                teacher: String::new(),
                school: String::new(),
                progress: String::new(),
                course_type: String::new(),
                auto_detected: false,
            },
        ];
        let discovered = vec![
            CourseItem {
                id: "new-1".into(),
                title: "高等数学".into(),
                url: "https://studyvideoh5.zhihuishu.com/stuStudy?recruitAndCourseId=aaa".into(),
                enabled: true,
                teacher: "李教授".into(),
                school: "清华大学".into(),
                progress: "65%".into(),
                course_type: "智慧共享课".into(),
                auto_detected: true,
            },
            CourseItem {
                id: "new-2".into(),
                title: "大学英语".into(),
                url: "https://fusioncourseh5.zhihuishu.com/stuStudy?recruitAndCourseId=bbb".into(),
                enabled: true,
                teacher: "王老师".into(),
                school: "北京大学".into(),
                progress: "0%".into(),
                course_type: "融合共享课".into(),
                auto_detected: true,
            },
        ];

        let merged = merge_discovered_courses(&existing, discovered);
        assert_eq!(merged.len(), 3);
        assert_eq!(merged[0].id, "old-1");
        assert_eq!(merged[0].title, "高等数学");
        assert!(!merged[0].enabled);
        assert_eq!(merged[0].progress, "65%");
        assert_eq!(merged[1].title, "大学英语");
        assert!(merged[1].enabled);
        assert_eq!(merged[2].id, "manual-1");
        assert!(!merged[2].auto_detected);
    }

    #[test]
    fn notification_provider_defaults_to_windows_and_respects_explicit_disable_and_legacy_bark() {
        let (default_cfg, _) = parse_ini_str("[user-account]\nusername = demo\n");
        assert_eq!(default_cfg.notification_provider, "Windows");

        let (disabled_cfg, _) = parse_ini_str("[notification]\nprovider =\n");
        assert_eq!(disabled_cfg.notification_provider, "");

        let (legacy_bark_cfg, _) =
            parse_ini_str("[notification]\nbarkUrl = https://api.day.app/mykey/\n");
        assert_eq!(legacy_bark_cfg.notification_provider, "Bark");
        assert_eq!(
            legacy_bark_cfg.notification_url,
            "https://api.day.app/mykey/"
        );
    }

    #[test]
    fn shutdown_on_complete_round_trips_in_ini() {
        let (default_cfg, _) = parse_ini_str("[script-option]\nenableAutoCaptcha = True\n");
        assert!(!default_cfg.shutdown_on_complete);

        let mut cfg = AutovisorConfig::default();
        cfg.shutdown_on_complete = true;
        let rendered = render_ini_str(&cfg);
        assert!(rendered.contains("shutdownOnComplete = True"));
        let (parsed, _) = parse_ini_str(&rendered);
        assert!(parsed.shutdown_on_complete);
    }

    #[test]
    fn write_file_atomic_handles_concurrent_writers_without_conflict() {
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let dir = std::env::temp_dir().join(format!("autovisor_t16_atomic_{nanos}"));
        fs::create_dir_all(&dir).unwrap();
        let target = dir.join("concurrent.json");

        let mut handles = Vec::new();
        for tid in 0..8usize {
            let p = target.clone();
            handles.push(std::thread::spawn(move || {
                for iter in 0..15usize {
                    let body = format!("{{\"tid\":{tid},\"iter\":{iter}}}");
                    write_file_atomic(&p, body.as_bytes(), "concurrent.json").unwrap();
                }
            }));
        }
        for h in handles {
            h.join().unwrap();
        }
        let final_text = fs::read_to_string(&target).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&final_text).unwrap();
        assert!(parsed.get("tid").is_some());
        assert!(parsed.get("iter").is_some());

        let _ = fs::remove_dir_all(&dir);
    }
}
