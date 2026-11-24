use super::model::{
    is_https_url, normalize_driver, normalize_notification_provider, parse_bool, AutovisorConfig,
};

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
    if v {
        "True"
    } else {
        "False"
    }
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
                "cdpurl" if !val.is_empty() => {
                    cfg.cdp_url = val;
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
                "barkurl"
                    if (!has_notification_provider || cfg.notification_provider.is_empty())
                        && cfg.notification_url.is_empty()
                        && !val.is_empty() =>
                {
                    cfg.notification_provider = "Bark".to_string();
                    cfg.notification_url = val;
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
            "course-url" if is_https_url(&val) && !urls.contains(&val) => {
                urls.push(val);
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
        format!("limitSpeed = {:.1}", cfg.limit_speed.clamp(0.5, 1.8)),
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
