pub mod ini;
pub mod llm;
pub mod model;
pub mod storage;

pub use llm::LlmConfig;
pub use model::{merge_discovered_courses, AutovisorConfig, CourseItem};
pub use storage::{load_config_from_dir, save_config_to_dir, write_file_atomic};

#[cfg(test)]
mod tests {
    use super::ini::{parse_ini_str, render_ini_str};
    use super::llm::infer_llm_provider;
    use super::model::{infer_title, is_generic_title};
    use super::*;
    use std::fs;

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
        let cfg = AutovisorConfig {
            log_retention_days: 0,
            log_compress_after_days: 0,
            ..Default::default()
        };

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
        let cfg = AutovisorConfig {
            username: "user\r\n[script-option]\nenableAutoCaptcha = False".to_string(),
            password: "  pass word  ".to_string(),
            proxy: "http://127.0.0.1:7890\n[course-option]\nlimitSpeed = 9.9".to_string(),
            ..Default::default()
        };
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
            infer_title(
                "https://ai-smart-course-student-pro.zhihuishu.com/testDetail/1/2/3/4",
                3
            ),
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
        let dir = std::env::temp_dir().join(format!("autovisor_llm_test_{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();

        let cfg = AutovisorConfig {
            llm: LlmConfig {
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
            },
            ..Default::default()
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

        let cfg = AutovisorConfig {
            shutdown_on_complete: true,
            ..Default::default()
        };
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
