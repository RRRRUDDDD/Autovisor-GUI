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
    if base.contains("api.deepseek.com") {
        "deepseek"
    } else if base.contains("api.openai.com") {
        "openai"
    } else if base.contains("anthropic.com") || fmt == "anthropic" {
        "anthropic"
    } else if base.contains("generativelanguage.googleapis.com") || fmt == "gemini" {
        "gemini"
    } else if base.contains("dashscope.aliyuncs.com") {
        "qwen"
    } else if base.contains("open.bigmodel.cn") {
        "zhipu"
    } else if base.contains("api.moonshot.cn") || base.contains("api.moonshot.ai") {
        "moonshot"
    } else if base.contains("volces.com") {
        "doubao"
    } else if base.contains("siliconflow.cn") || base.contains("siliconflow.com") {
        "siliconflow"
    } else if base.contains("hunyuan.cloud.tencent.com") {
        "hunyuan"
    } else if base.contains("spark-api-open.xf-yun.com") {
        "spark"
    } else if base.contains("qianfan.baidubce.com") {
        "qianfan"
    } else if base.contains("minimax.chat") || base.contains("minimaxi.com") {
        "minimax"
    } else if base.contains("lingyiwanwu.com") {
        "yi"
    } else if base.contains("api.x.ai") {
        "xai"
    } else if base.contains("api.groq.com") {
        "groq"
    } else if base.contains("api.mistral.ai") {
        "mistral"
    } else if base.contains("openrouter.ai") {
        "openrouter"
    } else if base.contains(".openai.azure.com") || fmt == "azure" {
        "azure"
    } else if fmt == "ollama" || base.contains(":11434") {
        "ollama"
    } else if base.contains(":1234") {
        "lmstudio"
    } else {
        "custom"
    }
    .to_string()
}

fn normalize_llm_api_format(raw: &str, api_base: &str, provider: &str) -> String {
    let fmt = raw.trim().to_ascii_lowercase();
    match fmt.as_str() {
        "openai" | "openai-responses" | "anthropic" | "gemini" | "azure" | "ollama" => fmt,
        _ => infer_llm_api_format(api_base, provider),
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct LlmConfig {
    #[serde(default)]
    pub provider: String,
    #[serde(default)]
    pub api_format: String,
    #[serde(default)]
    pub api_key: String,
    #[serde(default = "default_llm_api_base")]
    pub api_base: String,
    #[serde(default = "default_llm_model")]
    pub model: String,
    #[serde(default)]
    pub api_version: String,
    #[serde(default)]
    pub auto_submit: bool,
}

impl Default for LlmConfig {
    fn default() -> Self {
        Self {
            provider: default_llm_provider(),
            api_format: default_llm_api_format(),
            api_key: String::new(),
            api_base: default_llm_api_base(),
            model: default_llm_model(),
            api_version: String::new(),
            auto_submit: false,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct LlmDiskConfig {
    #[serde(default)]
    provider: String,
    #[serde(default)]
    api_format: String,
    #[serde(default)]
    api_key: String,
    #[serde(default = "default_llm_api_base")]
    api_base: String,
    #[serde(default = "default_llm_model")]
    model: String,
    #[serde(default)]
    api_version: String,
    #[serde(default)]
    auto_submit: bool,
}

impl From<&LlmConfig> for LlmDiskConfig {
    fn from(v: &LlmConfig) -> Self {
        let base_trim = v.api_base.trim();
        let api_base = if base_trim.is_empty() {
            default_llm_api_base()
        } else {
            base_trim.to_string()
        };
        let model_trim = v.model.trim();
        let model = if model_trim.is_empty() {
            default_llm_model()
        } else {
            model_trim.to_string()
        };
        let raw_provider = v.provider.trim().to_ascii_lowercase();
        let api_format = normalize_llm_api_format(&v.api_format, &api_base, &raw_provider);
        let provider = if raw_provider.is_empty() {
            infer_llm_provider(&api_base, &api_format)
        } else {
            raw_provider
        };
        Self {
            provider,
            api_format,
            api_key: v.api_key.trim().to_string(),
            api_base,
            model,
            api_version: v.api_version.trim().to_string(),
            auto_submit: v.auto_submit,
        }
    }
}

impl From<LlmDiskConfig> for LlmConfig {
    fn from(v: LlmDiskConfig) -> Self {
        let base_trim = v.api_base.trim();
        let api_base = if base_trim.is_empty() {
            default_llm_api_base()
        } else {
            base_trim.to_string()
        };
        let model_trim = v.model.trim();
        let model = if model_trim.is_empty() {
            default_llm_model()
        } else {
            model_trim.to_string()
        };
        let raw_provider = v.provider.trim().to_ascii_lowercase();
        let api_format = normalize_llm_api_format(&v.api_format, &api_base, &raw_provider);
        let provider = if raw_provider.is_empty() {
            infer_llm_provider(&api_base, &api_format)
        } else {
            raw_provider
        };
        Self {
            provider,
            api_format,
            api_key: v.api_key.trim().to_string(),
            api_base,
            model,
            api_version: v.api_version.trim().to_string(),
            auto_submit: v.auto_submit,
        }
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
    let kind = if lower.contains("ai-smart-course-student-pro.zhihuishu.com") {
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
        let line = raw_line.trim();
        if line.is_empty() || line.starts_with(';') || line.starts_with('#') {
            continue;
        }
        if line.starts_with('[') && line.ends_with(']') {
            section = line[1..line.len() - 1].trim().to_ascii_lowercase();
            continue;
        }
        let Some((k, v)) = line.split_once('=') else {
            continue;
        };
        let key = k.trim().to_ascii_lowercase();
        let val = v.trim().to_string();

        match section.as_str() {
            "user-account" => match key.as_str() {
                "username" => cfg.username = val,
                "password" => cfg.password = val,
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
    let mut lines = vec![
        "[user-account]".to_string(),
        ";配置账号密码，留空则打开网页后需手动登录".to_string(),
        format!("username = {}", cfg.username.trim()),
        format!("password = {}", cfg.password.trim()),
        ";可选: 启用终端二维码登录(默认:False)".to_string(),
        format!("qrLogin = {}", format_bool(cfg.qr_login)),
        format!("qrTerminal = {}", format_bool(cfg.qr_terminal)),
        format!("qrUnicode = {}", format_bool(cfg.qr_unicode)),
        format!("qrSaveDir = {}", cfg.qr_save_dir.trim()),
        String::new(),
        "[browser-option]".to_string(),
        ";配置浏览器,可选 Edge 或 Chrome".to_string(),
        format!("driver = {}", normalize_driver(&cfg.driver)),
        ";指定浏览器所在路径,不填则使用默认路径".to_string(),
        format!("EXE_PATH = {}", cfg.exe_path.trim()),
        ";可选: 代理服务器地址 (支持 http:// / https:// / socks5://)".to_string(),
        format!("proxy = {}", cfg.proxy.trim()),
        format!(
            "attachExistingChrome = {}",
            format_bool(cfg.attach_existing_chrome)
        ),
        format!(
            "cdpUrl = {}",
            if cfg.cdp_url.trim().is_empty() {
                "http://127.0.0.1:9222"
            } else {
                cfg.cdp_url.trim()
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
        String::new(),
        "[notification]".to_string(),
        ";通知服务商（默认:Windows，留空表示禁用通知）。可选值: Windows / ServerChan / Qmsg / Bark / Telegram"
            .to_string(),
        format!(
            "provider = {}",
            normalize_notification_provider(&cfg.notification_provider)
        ),
        format!("url = {}", cfg.notification_url.trim()),
        format!("tg_chat_id = {}", cfg.notification_tg_chat_id.trim()),
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
        String::new(),
        "[course-url]".to_string(),
        ";配置要学习的课程链接,支持智慧共享课、翻转课、融合共享课、AI慕课(wisdom-mooc)、AI智慧课".to_string(),
    ];

    let enabled_urls: Vec<&str> = cfg
        .courses
        .iter()
        .filter(|c| c.enabled && is_https_url(&c.url))
        .map(|c| c.url.trim())
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
    let (mut cfg, ini_urls) = if config_path.is_file() {
        let bytes = fs::read(&config_path).map_err(|e| format!("读取 config.ini 失败: {e}"))?;
        let text = String::from_utf8_lossy(&bytes).to_string();
        parse_ini_str(&text)
    } else {
        (AutovisorConfig::default(), Vec::new())
    };

    // Merge companion metadata (gui_courses.json) to preserve custom titles and unchecked courses
    let meta_path = base_dir.join("data").join("gui_courses.json");
    let saved_courses: Vec<CourseItem> = fs::read_to_string(&meta_path)
        .ok()
        .and_then(|raw| serde_json::from_str::<Vec<CourseItem>>(&raw).ok())
        .unwrap_or_default();

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
    if let Ok(raw) = fs::read_to_string(&mirrors_path) {
        if let Ok(parsed) = serde_json::from_str::<BTreeMap<String, String>>(raw.trim_start_matches('\u{feff}')) {
            if !parsed.is_empty() {
                cfg.mirrors = parsed;
            }
        }
    }

    // Load data/llm.json (universal LLM quiz solver)
    let llm_path = base_dir.join("data").join("llm.json");
    if let Ok(raw) = fs::read_to_string(&llm_path) {
        if let Ok(parsed) = serde_json::from_str::<LlmDiskConfig>(raw.trim_start_matches('\u{feff}')) {
            cfg.llm = LlmConfig::from(parsed);
        }
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

fn write_file_atomic(target: &Path, content: &[u8], label: &str) -> Result<(), String> {
    if let Some(parent) = target.parent() {
        let _ = fs::create_dir_all(parent);
    }
    let file_name = target
        .file_name()
        .and_then(|s| s.to_str())
        .unwrap_or("config");
    let tmp_path = target.with_file_name(format!(".tmp-{file_name}-{}", std::process::id()));
    fs::write(&tmp_path, content).map_err(|e| format!("写入 {label} 失败: {e}"))?;
    if fs::rename(&tmp_path, target).is_err() {
        let _ = fs::remove_file(target);
        if fs::rename(&tmp_path, target).is_err() {
            let _ = fs::remove_file(&tmp_path);
            fs::write(target, content).map_err(|e| format!("写入 {label} 失败: {e}"))?;
        }
    }
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
    let llm_disk = LlmDiskConfig::from(&cfg.llm);
    let llm_json = serde_json::to_string_pretty(&llm_disk)
        .map_err(|e| format!("序列化大模型配置失败: {e}"))?;
    let llm_text = format!("{llm_json}\n");
    write_file_atomic(&llm_path, llm_text.as_bytes(), "llm.json")?;

    load_config_from_dir(base_dir)
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
        assert_eq!(urls, vec!["https://studyvideoh5.zhihuishu.com/stuStudy?id=1"]);
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
            api_version: "2023-06-01".to_string(),
            auto_submit: true,
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
        assert_eq!(disk["api_version"], "2023-06-01");
        assert_eq!(disk["auto_submit"], true);

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
}
