use crate::error::AppError;
use crate::process_tree::configure_hidden_command;
use crate::runtime::{self, save_close_action, RuntimeInfo};
use crate::window_close::CloseAction;
use crate::{data_dir, spawn_cleanup_and_exit, AppState};
use std::fs;
use std::path::PathBuf;
use std::process::Command;
use std::sync::Arc;
use tauri::{Manager, State};

#[tauri::command]
pub fn set_close_behavior(
    state: State<'_, AppState>,
    behavior: String,
) -> Result<RuntimeInfo, AppError> {
    let action = CloseAction::parse(&behavior)
        .ok_or_else(|| AppError::validation("不支持的关闭行为选项"))?;
    save_close_action(action).map_err(AppError::Config)?;
    Ok(runtime::get_runtime_info(&state.backend.status()))
}

#[tauri::command]
pub fn close_prompt_shown(state: State<'_, AppState>, prompt_id: u64) -> bool {
    state.close_gate.acknowledge(prompt_id)
}

#[tauri::command]
pub fn close_choice(
    window: tauri::WebviewWindow,
    state: State<'_, AppState>,
    action: String,
    remember: bool,
) -> Result<RuntimeInfo, AppError> {
    let parsed = match action.as_str() {
        "cancel" if !remember => None,
        "minimize" | "tray" | "exit" => CloseAction::parse(&action),
        _ => return Err(AppError::validation("关闭方式无效")),
    };
    state.close_gate.resolve();
    let Some(close_action) = parsed else {
        return Ok(runtime::get_runtime_info(&state.backend.status()));
    };
    let saved = if remember {
        save_close_action(close_action)
    } else {
        Ok(())
    };
    match close_action {
        CloseAction::Minimize => {
            let _ = window.minimize();
        }
        CloseAction::Tray => {
            let _ = window.hide();
        }
        _ => {
            spawn_cleanup_and_exit(
                window.app_handle().clone(),
                Arc::clone(&state.exit_cleaned),
                Arc::clone(&state.zhs_session),
                Arc::clone(&state.backend),
            );
        }
    }
    saved.map_err(AppError::Config)?;
    Ok(runtime::get_runtime_info(&state.backend.status()))
}

#[tauri::command]
pub fn pick_browser_exe() -> Option<String> {
    #[cfg(windows)]
    {
        rfd::FileDialog::new()
            .set_title("选择浏览器可执行文件 (msedge.exe / chrome.exe)")
            .add_filter("可执行文件", &["exe"])
            .pick_file()
            .map(|p| p.to_string_lossy().to_string())
    }
    #[cfg(not(windows))]
    {
        rfd::FileDialog::new()
            .set_title("选择浏览器可执行文件")
            .pick_file()
            .map(|p| p.to_string_lossy().to_string())
    }
}

#[tauri::command]
pub fn pick_cookie_file() -> Option<String> {
    rfd::FileDialog::new()
        .set_title("选择要导入的智慧树 Cookie JSON 文件")
        .add_filter("JSON 文件", &["json"])
        .pick_file()
        .map(|p| p.to_string_lossy().to_string())
}

#[tauri::command]
pub fn pick_directory() -> Option<String> {
    rfd::FileDialog::new()
        .set_title("选择保存二维码图片的文件夹")
        .pick_folder()
        .map(|p| p.to_string_lossy().to_string())
}

const BLOCKED_OPEN_EXTENSIONS: &[&str] = &[
    "exe", "bat", "cmd", "ps1", "vbs", "vbe", "msi", "msp", "com", "scr", "js", "jse", "wsf",
    "wsh", "lnk", "pif", "reg", "dll", "sh", "app", "command",
];

const ALLOWED_OPEN_FILE_EXTENSIONS: &[&str] = &["log", "txt", "ini", "json"];

fn is_unc_path_str(raw: &str) -> bool {
    let trimmed = raw.trim();
    if trimmed.starts_with(r"\\") || trimmed.starts_with("//") {
        let lower = trimmed.to_ascii_lowercase();
        if lower.starts_with(r"\\?\unc\") {
            return true;
        }
        // Reject raw UNC paths like \\server\share (allow \\?\C:\... only after canonicalize check below)
        if !lower.starts_with(r"\\?\") {
            return true;
        }
        // Even with \\?\, reject anything that isn't a local drive letter like \\?\C:\
        let bytes = trimmed.as_bytes();
        if bytes.len() < 7 || !bytes[4].is_ascii_alphabetic() || bytes[5] != b':' {
            return true;
        }
    }
    false
}

#[cfg(windows)]
fn strip_verbatim_prefix(path: &std::path::Path) -> PathBuf {
    let text = path.to_string_lossy();
    if let Some(rest) = text.strip_prefix(r"\\?\") {
        let bytes = rest.as_bytes();
        if bytes.len() >= 2 && bytes[0].is_ascii_alphabetic() && bytes[1] == b':' {
            return PathBuf::from(rest);
        }
    }
    path.to_path_buf()
}

pub(crate) fn validate_open_path(raw: &str, allowed_roots: &[PathBuf]) -> Result<PathBuf, String> {
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return Err("路径不能为空".to_string());
    }
    if trimmed.chars().any(|c| c.is_control()) {
        return Err("路径包含非法控制字符".to_string());
    }
    if is_unc_path_str(trimmed) {
        return Err("禁止打开网络 UNC 路径".to_string());
    }

    let path = PathBuf::from(trimmed);
    if !path.exists() {
        return Err(format!("路径不存在: {}", path.display()));
    }
    let canonical = fs::canonicalize(&path).map_err(|e| format!("解析路径失败: {e}"))?;
    let canonical_str = canonical.to_string_lossy();
    if canonical_str.to_ascii_lowercase().starts_with(r"\\?\unc\") {
        return Err("禁止打开网络 UNC 路径".to_string());
    }

    let mut canonical_roots: Vec<PathBuf> = Vec::new();
    for root in allowed_roots {
        if let Ok(canon_root) = fs::canonicalize(root) {
            canonical_roots.push(canon_root);
        }
    }
    if canonical_roots.is_empty()
        || !canonical_roots
            .iter()
            .any(|root| canonical.starts_with(root))
    {
        return Err("路径超出允许访问的数据目录范围".to_string());
    }

    if canonical.is_file() {
        let ext = canonical
            .extension()
            .and_then(|e| e.to_str())
            .map(|e| e.to_ascii_lowercase())
            .unwrap_or_default();
        if BLOCKED_OPEN_EXTENSIONS.contains(&ext.as_str()) {
            return Err(format!("禁止打开可执行文件类型: .{ext}"));
        }
        if !ALLOWED_OPEN_FILE_EXTENSIONS.contains(&ext.as_str()) {
            return Err("仅允许打开目录或 .log/.txt/.ini/.json 文件".to_string());
        }
    } else if !canonical.is_dir() {
        return Err("仅允许打开目录或常规文本/配置文件".to_string());
    }

    Ok(canonical)
}

pub(crate) fn validate_open_url(raw: &str) -> Result<String, String> {
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return Err("链接不能为空".to_string());
    }
    if trimmed.chars().any(|c| c.is_control() || c.is_whitespace()) {
        return Err("链接包含非法字符或空白".to_string());
    }
    let parsed = url::Url::parse(trimmed).map_err(|_| "无效的 URL 格式".to_string())?;
    if !matches!(parsed.scheme(), "http" | "https") {
        return Err("仅允许打开 http(s) 链接".to_string());
    }
    match parsed.host() {
        Some(url::Host::Domain(domain)) => {
            if domain.is_empty()
                || !domain
                    .chars()
                    .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '-' | '_'))
            {
                return Err("链接主机名包含非法字符".to_string());
            }
        }
        Some(url::Host::Ipv4(_)) | Some(url::Host::Ipv6(_)) => {}
        None => return Err("链接缺少有效主机名".to_string()),
    }
    Ok(parsed.as_str().to_string())
}

#[tauri::command]
pub fn open_path(target: String) -> Result<(), AppError> {
    let base_dir = data_dir();
    let allowed_roots = vec![base_dir.clone(), base_dir.join("logs")];
    let canonical = validate_open_path(&target, &allowed_roots).map_err(AppError::Validation)?;
    #[cfg(windows)]
    let open_target = strip_verbatim_prefix(&canonical);
    #[cfg(not(windows))]
    let open_target = canonical;

    #[cfg(windows)]
    let mut cmd = {
        let mut c = Command::new("explorer");
        c.arg(&open_target);
        c
    };
    #[cfg(target_os = "macos")]
    let mut cmd = {
        let mut c = Command::new("open");
        c.arg(&open_target);
        c
    };
    #[cfg(all(unix, not(target_os = "macos")))]
    let mut cmd = {
        let mut c = Command::new("xdg-open");
        c.arg(&open_target);
        c
    };
    configure_hidden_command(&mut cmd);
    cmd.spawn()
        .map_err(|e| AppError::io(format!("打开路径失败: {e}")))?;
    Ok(())
}

#[tauri::command]
pub fn open_url(url: String) -> Result<(), AppError> {
    let validated = validate_open_url(&url).map_err(AppError::Validation)?;
    #[cfg(windows)]
    {
        use std::ffi::OsStr;
        use std::os::windows::ffi::OsStrExt;
        use windows::core::{w, PCWSTR};
        use windows::Win32::UI::Shell::ShellExecuteW;
        use windows::Win32::UI::WindowsAndMessaging::SW_SHOWNORMAL;

        let wide_url: Vec<u16> = OsStr::new(&validated)
            .encode_wide()
            .chain(std::iter::once(0))
            .collect();
        let ret = unsafe {
            ShellExecuteW(
                None,
                w!("open"),
                PCWSTR(wide_url.as_ptr()),
                PCWSTR::null(),
                PCWSTR::null(),
                SW_SHOWNORMAL,
            )
        };
        if (ret.0 as isize) <= 32 {
            return Err(AppError::io(format!("打开网页失败 (code={})", ret.0 as isize)));
        }
        Ok(())
    }
    #[cfg(target_os = "macos")]
    {
        let mut cmd = Command::new("open");
        cmd.arg(&validated);
        configure_hidden_command(&mut cmd);
        cmd.spawn()
            .map_err(|e| AppError::io(format!("打开网页失败: {e}")))?;
        Ok(())
    }
    #[cfg(all(unix, not(target_os = "macos")))]
    {
        let mut cmd = Command::new("xdg-open");
        cmd.arg(&validated);
        configure_hidden_command(&mut cmd);
        cmd.spawn()
            .map_err(|e| AppError::io(format!("打开网页失败: {e}")))?;
        Ok(())
    }
    #[cfg(not(any(windows, unix)))]
    {
        let _ = validated;
        Err(AppError::internal("当前平台不支持打开网页"))
    }
}

#[cfg(test)]
mod security_tests {
    use super::{validate_open_path, validate_open_url};
    use std::fs;
    use std::path::PathBuf;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn make_temp_dir(name: &str) -> PathBuf {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let dir = std::env::temp_dir().join(format!("autovisor_t11_{name}_{nanos}"));
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn validate_open_url_allows_valid_http_https_with_ampersand_query() {
        let ok = validate_open_url("https://studyvideoh5.zhihuishu.com/learnPage?a=1&b=2")
            .expect("valid https url with query params should be allowed");
        assert_eq!(ok, "https://studyvideoh5.zhihuishu.com/learnPage?a=1&b=2");

        let ok_http = validate_open_url("http://127.0.0.1:8080/path?x=1&y=2")
            .expect("valid http loopback url should be allowed");
        assert_eq!(ok_http, "http://127.0.0.1:8080/path?x=1&y=2");
    }

    #[test]
    fn validate_open_url_rejects_injection_and_non_http_schemes() {
        assert!(validate_open_url("https://example.com &calc.exe").is_err());
        assert!(validate_open_url("https://example.com&calc.exe").is_err());
        assert!(validate_open_url("https://example.com/\r\n&calc.exe").is_err());
        assert!(validate_open_url("file:///C:/Windows/System32/cmd.exe").is_err());
        assert!(validate_open_url("javascript:alert(1)").is_err());
        assert!(validate_open_url("calc.exe").is_err());
        assert!(validate_open_url("https://").is_err());
    }

    #[test]
    fn validate_open_path_rejects_unc_escape_and_executables() {
        let root = make_temp_dir("allowed");
        let outside = make_temp_dir("outside");
        let logs_dir = root.join("logs");
        fs::create_dir_all(&logs_dir).unwrap();

        let good_log = logs_dir.join("Log1.txt");
        let good_ini = root.join("config.ini");
        let bad_exe = root.join("payload.exe");
        let bad_bat = root.join("run.bat");
        let outside_txt = outside.join("secret.txt");

        fs::write(&good_log, "ok").unwrap();
        fs::write(&good_ini, "[user]\n").unwrap();
        fs::write(&bad_exe, "MZ").unwrap();
        fs::write(&bad_bat, "@echo off").unwrap();
        fs::write(&outside_txt, "nope").unwrap();

        let roots = vec![root.clone(), logs_dir.clone()];

        assert!(validate_open_path(good_log.to_str().unwrap(), &roots).is_ok());
        assert!(validate_open_path(good_ini.to_str().unwrap(), &roots).is_ok());
        assert!(validate_open_path(logs_dir.to_str().unwrap(), &roots).is_ok());

        // Reject UNC paths
        assert!(validate_open_path(r"\\evil\share\file.txt", &roots).is_err());
        assert!(validate_open_path("//evil/share/file.txt", &roots).is_err());

        // Reject out-of-scope paths
        assert!(validate_open_path(outside_txt.to_str().unwrap(), &roots).is_err());
        #[cfg(windows)]
        {
            assert!(validate_open_path(r"C:\Windows\System32\cmd.exe", &roots).is_err());
        }

        // Reject executables even inside allowed root
        assert!(validate_open_path(bad_exe.to_str().unwrap(), &roots).is_err());
        assert!(validate_open_path(bad_bat.to_str().unwrap(), &roots).is_err());

        let _ = fs::remove_dir_all(&root);
        let _ = fs::remove_dir_all(&outside);
    }
}
