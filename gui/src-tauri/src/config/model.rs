use super::llm::LlmConfig;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, HashMap, HashSet};

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

pub(crate) fn parse_bool(raw: &str, default: bool) -> bool {
    match raw.trim().to_ascii_lowercase().as_str() {
        "true" | "1" | "yes" => true,
        "false" | "0" | "no" => false,
        _ => default,
    }
}

pub(crate) fn normalize_driver(raw: &str) -> String {
    match raw.trim().to_ascii_lowercase().as_str() {
        "chrome" => "Chrome".to_string(),
        "chromium" => "Chromium".to_string(),
        _ => "Edge".to_string(),
    }
}

pub(crate) fn normalize_notification_provider(raw: &str) -> String {
    match raw.trim().to_ascii_lowercase().as_str() {
        "windows" => "Windows".to_string(),
        "serverchan" => "ServerChan".to_string(),
        "qmsg" => "Qmsg".to_string(),
        "bark" => "Bark".to_string(),
        "telegram" => "Telegram".to_string(),
        _ => String::new(),
    }
}

pub(crate) fn is_https_url(raw: &str) -> bool {
    let s = raw.trim();
    s.starts_with("https://") && s.len() > 10 && !s.contains(char::is_whitespace)
}

pub(crate) fn infer_title(url: &str, idx: usize) -> String {
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

fn canonical_course_url(raw: &str) -> String {
    let mut s = raw.trim().to_string();
    if s.ends_with("#/") {
        s.truncate(s.len() - 2);
    } else if s.ends_with('#') {
        s.truncate(s.len() - 1);
    }
    s
}

pub(crate) fn is_generic_title(title: &str) -> bool {
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
    let mut by_url: HashMap<String, &CourseItem> = HashMap::new();
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
