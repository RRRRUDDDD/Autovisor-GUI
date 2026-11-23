mod backend;
mod browser_detect;
mod config_ini;
mod process_tree;
mod runtime;
mod window_close;
mod zhs_session;

use backend::{BackendManager, RunState};
use config_ini::{load_config_from_dir, save_config_to_dir, AutovisorConfig, LlmConfig};
use process_tree::configure_hidden_command;
use runtime::{
    load_close_action, load_prefs, read_log_tail_scoped, resolve_app_paths, save_close_action,
    save_prefs, LogFileItem, RuntimeInfo,
};
use std::fs;
use std::path::PathBuf;
use std::process::Command;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use tauri::menu::{Menu, MenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Emitter, Manager, RunEvent, State, WindowEvent};
use window_close::{CloseAction, CloseDecision, CloseGate};
use zhs_session::{execute_zhs_login, LoginRequest, SchoolItem, SessionResult, ZhsSessionManager};

struct AppState {
    backend: Arc<BackendManager>,
    zhs_session: Arc<ZhsSessionManager>,
    close_gate: Arc<CloseGate>,
    exit_cleaned: Arc<AtomicBool>,
}

fn cleanup_before_exit(
    exit_cleaned: &AtomicBool,
    zhs_session: &ZhsSessionManager,
    backend: &BackendManager,
) {
    if !exit_cleaned.swap(true, Ordering::SeqCst) {
        zhs_session.cancel();
        backend.shutdown();
    }
}

fn data_dir() -> PathBuf {
    resolve_app_paths().data_dir
}

#[tauri::command]
fn get_runtime_info(state: State<'_, AppState>, refresh_browsers: Option<bool>) -> RuntimeInfo {
    runtime::get_runtime_info_with_refresh(
        &state.backend.status(),
        refresh_browsers.unwrap_or(false),
    )
}

#[tauri::command]
async fn restart_backend(
    app: AppHandle,
    state: State<'_, AppState>,
) -> Result<RuntimeInfo, String> {
    let backend = Arc::clone(&state.backend);
    let app_clone = app.clone();
    tauri::async_runtime::spawn_blocking(move || backend.restart(app_clone))
        .await
        .map_err(|e| format!("重启内置引擎异常: {e}"))??;
    Ok(runtime::get_runtime_info(&state.backend.status()))
}

#[tauri::command]
async fn search_schools(query: String, proxy: Option<String>) -> Result<Vec<SchoolItem>, String> {
    let base_dir = data_dir();
    let effective_proxy = proxy
        .map(|p| p.trim().to_string())
        .filter(|p| !p.is_empty())
        .unwrap_or_else(|| {
            load_config_from_dir(&base_dir)
                .map(|c| c.proxy)
                .unwrap_or_default()
        });
    tauri::async_runtime::spawn_blocking(move || {
        zhs_session::search_schools(&query, &effective_proxy)
    })
    .await
    .map_err(|e| format!("查询学校列表失败: {e}"))?
}

#[tauri::command]
async fn zhs_login(
    app: AppHandle,
    state: State<'_, AppState>,
    request: LoginRequest,
) -> Result<SessionResult, String> {
    let base_dir = data_dir();
    let manager = Arc::clone(&state.zhs_session);
    let backend = Arc::clone(&state.backend);
    tauri::async_runtime::spawn_blocking(move || {
        execute_zhs_login(&app, &manager, &backend, &base_dir, request)
    })
    .await
    .map_err(|e| format!("登录任务执行异常: {e}"))?
}

#[tauri::command]
fn cancel_zhs_login(state: State<'_, AppState>) {
    state.zhs_session.cancel();
}

#[tauri::command]
async fn refresh_courses(
    app: AppHandle,
    state: State<'_, AppState>,
) -> Result<SessionResult, String> {
    let base_dir = data_dir();
    let manager = Arc::clone(&state.zhs_session);
    let backend = Arc::clone(&state.backend);
    let req = LoginRequest {
        mode: "cookie".to_string(),
        username: String::new(),
        school_id: String::new(),
        school_name: String::new(),
        student_id: String::new(),
        password: String::new(),
        browser_exe: String::new(),
        proxy: String::new(),
        remember_credentials: false,
    };
    tauri::async_runtime::spawn_blocking(move || {
        execute_zhs_login(&app, &manager, &backend, &base_dir, req)
    })
    .await
    .map_err(|e| format!("刷新课程任务异常: {e}"))?
}

#[tauri::command]
fn logout_zhs(state: State<'_, AppState>) -> Result<RuntimeInfo, String> {
    state.zhs_session.cancel();
    let base_dir = data_dir();
    let cookie_file = base_dir.join("data").join("cookies.json");
    if cookie_file.is_file() {
        let _ = fs::remove_file(&cookie_file);
    }
    let mut prefs = load_prefs();
    prefs.session = None;
    save_prefs(&prefs)?;
    Ok(runtime::get_runtime_info(&state.backend.status()))
}

#[tauri::command]
fn set_close_behavior(
    state: State<'_, AppState>,
    behavior: String,
) -> Result<RuntimeInfo, String> {
    let action = CloseAction::parse(&behavior).ok_or_else(|| "不支持的关闭行为选项".to_string())?;
    save_close_action(action)?;
    Ok(runtime::get_runtime_info(&state.backend.status()))
}

#[tauri::command]
fn close_prompt_shown(state: State<'_, AppState>, prompt_id: u64) -> bool {
    state.close_gate.acknowledge(prompt_id)
}

#[tauri::command]
fn close_choice(
    window: tauri::WebviewWindow,
    state: State<'_, AppState>,
    action: String,
    remember: bool,
) -> Result<RuntimeInfo, String> {
    let parsed = match action.as_str() {
        "cancel" if !remember => None,
        "minimize" | "tray" | "exit" => CloseAction::parse(&action),
        _ => return Err("关闭方式无效".to_string()),
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
            cleanup_before_exit(&state.exit_cleaned, &state.zhs_session, &state.backend);
            window.app_handle().exit(0);
        }
    }
    saved?;
    Ok(runtime::get_runtime_info(&state.backend.status()))
}

#[tauri::command]
fn pick_browser_exe() -> Option<String> {
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
fn pick_cookie_file() -> Option<String> {
    rfd::FileDialog::new()
        .set_title("选择要导入的智慧树 Cookie JSON 文件")
        .add_filter("JSON 文件", &["json"])
        .pick_file()
        .map(|p| p.to_string_lossy().to_string())
}

#[tauri::command]
fn pick_directory() -> Option<String> {
    rfd::FileDialog::new()
        .set_title("选择保存二维码图片的文件夹")
        .pick_folder()
        .map(|p| p.to_string_lossy().to_string())
}

#[tauri::command]
fn load_config() -> Result<AutovisorConfig, String> {
    let base_dir = data_dir();
    load_config_from_dir(base_dir.as_path())
}

#[tauri::command]
fn save_config(config: AutovisorConfig) -> Result<AutovisorConfig, String> {
    let base_dir = data_dir();
    save_config_to_dir(&base_dir, &config)
}

#[tauri::command]
fn get_run_state(state: State<'_, AppState>) -> RunState {
    state.backend.snapshot()
}

#[tauri::command]
async fn start_autovisor(
    app: AppHandle,
    state: State<'_, AppState>,
) -> Result<RunState, String> {
    let base_dir = data_dir();
    let cfg = load_config_from_dir(&base_dir)?;
    let has_enabled = cfg.courses.iter().any(|c| c.enabled);
    if !has_enabled {
        return Err("请至少勾选一门课程后再开始刷课".to_string());
    }
    let _ = save_config_to_dir(&base_dir, &cfg);
    let backend = Arc::clone(&state.backend);
    tauri::async_runtime::spawn_blocking(move || backend.start_task(app, "study", ""))
        .await
        .map_err(|e| format!("启动学习任务异常: {e}"))?
}

#[tauri::command]
async fn stop_autovisor(app: AppHandle, state: State<'_, AppState>) -> Result<RunState, String> {
    let backend = Arc::clone(&state.backend);
    tauri::async_runtime::spawn_blocking(move || backend.stop_task(Some(&app)))
        .await
        .map_err(|e| format!("停止任务异常: {e}"))
}

#[tauri::command]
async fn cancel_shutdown(app: AppHandle, state: State<'_, AppState>) -> Result<RunState, String> {
    let backend = Arc::clone(&state.backend);
    tauri::async_runtime::spawn_blocking(move || backend.cancel_shutdown(Some(&app)))
        .await
        .map_err(|e| format!("取消关机异常: {e}"))?
}

#[tauri::command]
async fn run_diagnostic(
    app: AppHandle,
    state: State<'_, AppState>,
    kind: String,
    target: String,
) -> Result<RunState, String> {
    let cleaned_target = match kind.as_str() {
        "check_browser" => String::new(),
        "check_course" => {
            let trimmed = target.trim();
            if !trimmed.starts_with("https://") {
                return Err("请提供有效的 https:// 课程播放页链接".to_string());
            }
            trimmed.to_string()
        }
        "import_cookies" => {
            let trimmed = target.trim();
            if trimmed.is_empty() || !PathBuf::from(trimmed).is_file() {
                return Err("所选 Cookie JSON 文件不存在".to_string());
            }
            trimmed.to_string()
        }
        _ => return Err(format!("未知诊断类型: {kind}")),
    };
    let backend = Arc::clone(&state.backend);
    tauri::async_runtime::spawn_blocking(move || backend.start_task(app, &kind, &cleaned_target))
        .await
        .map_err(|e| format!("执行诊断任务异常: {e}"))?
}

#[tauri::command]
async fn test_notification(
    app: AppHandle,
    state: State<'_, AppState>,
    provider: String,
    url: String,
    tg_chat_id: String,
    proxy: String,
) -> Result<String, String> {
    let backend = Arc::clone(&state.backend);
    let payload = serde_json::json!({
        "provider": provider,
        "url": url,
        "tgChatId": tg_chat_id,
        "proxy": proxy,
    });
    let resp = tauri::async_runtime::spawn_blocking(move || {
        backend.post_json(Some(&app), "/api/notify/test", &payload, 25)
    })
    .await
    .map_err(|e| format!("推送测试执行异常: {e}"))??;
    resp.get("message")
        .and_then(|v| v.as_str())
        .map(String::from)
        .ok_or_else(|| "推送测试返回格式异常".to_string())
}

#[tauri::command]
async fn test_llm_connection(
    app: AppHandle,
    state: State<'_, AppState>,
    llm: LlmConfig,
    proxy: String,
) -> Result<String, String> {
    let backend = Arc::clone(&state.backend);
    let payload = serde_json::json!({
        "llm": llm,
        "proxy": proxy,
    });
    let resp = tauri::async_runtime::spawn_blocking(move || {
        backend.post_json(Some(&app), "/api/llm/test", &payload, 40)
    })
    .await
    .map_err(|e| format!("模型连接测试异常: {e}"))??;
    resp.get("message")
        .and_then(|v| v.as_str())
        .map(String::from)
        .ok_or_else(|| "模型连接测试返回格式异常".to_string())
}

#[tauri::command]
async fn test_question_bank(
    app: AppHandle,
    state: State<'_, AppState>,
    token: String,
    url: String,
    proxy: String,
) -> Result<String, String> {
    let backend = Arc::clone(&state.backend);
    let payload = serde_json::json!({
        "token": token,
        "url": url,
        "proxy": proxy,
    });
    let resp = tauri::async_runtime::spawn_blocking(move || {
        backend.post_json(Some(&app), "/api/question-bank/test", &payload, 25)
    })
    .await
    .map_err(|e| format!("题库连通性测试异常: {e}"))??;
    let ok = resp.get("ok").and_then(|v| v.as_bool()).unwrap_or(false);
    let message = resp
        .get("message")
        .and_then(|v| v.as_str())
        .unwrap_or("题库连通性测试返回格式异常");
    if ok {
        Ok(message.to_string())
    } else {
        Err(message.to_string())
    }
}

#[tauri::command]
async fn export_question_cache(
    app: AppHandle,
    state: State<'_, AppState>,
    course_id: Option<String>,
) -> Result<Option<String>, String> {
    let backend = Arc::clone(&state.backend);
    let payload = serde_json::json!({ "courseId": course_id.unwrap_or_default() });
    let resp = tauri::async_runtime::spawn_blocking(move || {
        backend.post_json(Some(&app), "/api/cache/export", &payload, 60)
    })
    .await
    .map_err(|e| format!("导出题库缓存异常: {e}"))??;
    let data = resp
        .get("data")
        .cloned()
        .ok_or_else(|| "导出题库缓存返回格式异常".to_string())?;
    let Some(path) = rfd::FileDialog::new()
        .set_title("导出 SQLite 题库缓存")
        .add_filter("JSON 文件", &["json"])
        .set_file_name("autovisor-question-bank.json")
        .save_file()
    else {
        return Ok(None);
    };
    let text = serde_json::to_string_pretty(&data)
        .map_err(|e| format!("序列化题库缓存失败: {e}"))?;
    fs::write(&path, text).map_err(|e| format!("写入题库缓存失败: {e}"))?;
    Ok(Some(path.to_string_lossy().to_string()))
}

#[tauri::command]
async fn import_question_cache(
    app: AppHandle,
    state: State<'_, AppState>,
) -> Result<Option<serde_json::Value>, String> {
    let Some(path) = rfd::FileDialog::new()
        .set_title("导入 SQLite 题库缓存")
        .add_filter("JSON 文件", &["json"])
        .pick_file()
    else {
        return Ok(None);
    };
    let raw = fs::read_to_string(&path).map_err(|e| format!("读取题库缓存失败: {e}"))?;
    let parsed: serde_json::Value = serde_json::from_str(raw.trim_start_matches('\u{feff}'))
        .map_err(|e| format!("解析题库缓存失败: {e}"))?;
    let body = if parsed.get("data").is_some() {
        parsed
    } else {
        serde_json::json!({ "data": parsed })
    };
    let backend = Arc::clone(&state.backend);
    let resp = tauri::async_runtime::spawn_blocking(move || {
        backend.post_json(Some(&app), "/api/cache/import", &body, 60)
    })
    .await
    .map_err(|e| format!("导入题库缓存异常: {e}"))??;
    Ok(Some(resp))
}

#[tauri::command]
async fn fetch_llm_models(
    app: AppHandle,
    state: State<'_, AppState>,
    llm: LlmConfig,
    proxy: String,
) -> Result<Vec<String>, String> {
    let backend = Arc::clone(&state.backend);
    let payload = serde_json::json!({
        "llm": llm,
        "proxy": proxy,
    });
    let resp = tauri::async_runtime::spawn_blocking(move || {
        backend.post_json(Some(&app), "/api/llm/models", &payload, 30)
    })
    .await
    .map_err(|e| format!("拉取模型列表异常: {e}"))??;
    resp.get("models")
        .and_then(|v| v.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|item| item.as_str().map(String::from))
                .collect()
        })
        .ok_or_else(|| "拉取模型列表返回格式异常".to_string())
}

#[tauri::command]
async fn set_debug_log(
    app: AppHandle,
    state: State<'_, AppState>,
    enabled: bool,
) -> Result<bool, String> {
    let backend = Arc::clone(&state.backend);
    let payload = serde_json::json!({ "enabled": enabled });
    let resp = tauri::async_runtime::spawn_blocking(move || {
        backend.post_json(Some(&app), "/api/tasks/debug", &payload, 10)
    })
    .await
    .map_err(|e| format!("切换调试日志异常: {e}"))??;
    Ok(resp
        .get("enabled")
        .and_then(|v| v.as_bool())
        .unwrap_or(enabled))
}

#[tauri::command]
async fn start_qr_login(
    app: AppHandle,
    state: State<'_, AppState>,
    proxy: String,
    save_dir: Option<String>,
) -> Result<serde_json::Value, String> {
    let backend = Arc::clone(&state.backend);
    let payload = serde_json::json!({
        "proxy": proxy,
        "saveDir": save_dir.unwrap_or_default(),
    });
    tauri::async_runtime::spawn_blocking(move || {
        backend.post_json(Some(&app), "/api/qr-login/start", &payload, 20)
    })
    .await
    .map_err(|e| format!("获取登录二维码异常: {e}"))?
}

#[tauri::command]
async fn poll_qr_login(
    app: AppHandle,
    state: State<'_, AppState>,
    session_id: String,
) -> Result<serde_json::Value, String> {
    let backend = Arc::clone(&state.backend);
    let payload = serde_json::json!({ "sessionId": session_id });
    tauri::async_runtime::spawn_blocking(move || {
        backend.post_json(Some(&app), "/api/qr-login/poll", &payload, 20)
    })
    .await
    .map_err(|e| format!("轮询二维码状态异常: {e}"))?
}

#[tauri::command]
async fn save_qr_image(
    app: AppHandle,
    state: State<'_, AppState>,
    session_id: String,
    save_dir: String,
) -> Result<serde_json::Value, String> {
    let backend = Arc::clone(&state.backend);
    let payload = serde_json::json!({
        "sessionId": session_id,
        "saveDir": save_dir,
    });
    tauri::async_runtime::spawn_blocking(move || {
        backend.post_json(Some(&app), "/api/qr-login/save", &payload, 15)
    })
    .await
    .map_err(|e| format!("保存二维码图片异常: {e}"))?
}

#[tauri::command]
async fn cancel_qr_login(
    app: AppHandle,
    state: State<'_, AppState>,
    session_id: String,
) -> Result<(), String> {
    let backend = Arc::clone(&state.backend);
    let payload = serde_json::json!({ "sessionId": session_id });
    let _ = tauri::async_runtime::spawn_blocking(move || {
        backend.post_json(Some(&app), "/api/qr-login/cancel", &payload, 10)
    })
    .await;
    Ok(())
}

#[tauri::command]
async fn check_for_update(
    app: AppHandle,
    state: State<'_, AppState>,
    proxy: String,
) -> Result<serde_json::Value, String> {
    let backend = Arc::clone(&state.backend);
    let payload = serde_json::json!({
        "proxy": proxy,
        "currentVersion": env!("CARGO_PKG_VERSION"),
    });
    tauri::async_runtime::spawn_blocking(move || {
        backend.post_json(Some(&app), "/api/update/check", &payload, 15)
    })
    .await
    .map_err(|e| format!("检查更新执行异常: {e}"))?
}

#[tauri::command]
fn clear_cookies(state: State<'_, AppState>) -> Result<RuntimeInfo, String> {
    let base_dir = data_dir();
    let cookie_file = base_dir.join("data").join("cookies.json");
    if cookie_file.is_file() {
        fs::remove_file(&cookie_file).map_err(|e| format!("清除 Cookie 失败: {e}"))?;
    }
    let mut prefs = load_prefs();
    prefs.session = None;
    let _ = save_prefs(&prefs);
    Ok(runtime::get_runtime_info(&state.backend.status()))
}

#[tauri::command]
fn list_log_files() -> Vec<LogFileItem> {
    runtime::list_log_files(data_dir().as_path())
}

#[tauri::command]
fn read_log_file(path: String) -> Result<String, String> {
    read_log_tail_scoped(
        data_dir().as_path(),
        PathBuf::from(path.trim()).as_path(),
        128 * 1024,
    )
}

#[tauri::command]
fn open_path(target: String) -> Result<(), String> {
    let path = PathBuf::from(target.trim());
    if !path.exists() {
        return Err(format!("路径不存在: {}", path.display()));
    }
    #[cfg(windows)]
    let mut cmd = {
        let mut c = Command::new("explorer");
        c.arg(&path);
        c
    };
    #[cfg(target_os = "macos")]
    let mut cmd = {
        let mut c = Command::new("open");
        c.arg(&path);
        c
    };
    #[cfg(all(unix, not(target_os = "macos")))]
    let mut cmd = {
        let mut c = Command::new("xdg-open");
        c.arg(&path);
        c
    };
    configure_hidden_command(&mut cmd);
    cmd.spawn().map_err(|e| format!("打开路径失败: {e}"))?;
    Ok(())
}

#[tauri::command]
fn open_url(url: String) -> Result<(), String> {
    let trimmed = url.trim();
    if !trimmed.starts_with("https://") && !trimmed.starts_with("http://") {
        return Err("仅允许打开 http(s) 链接".to_string());
    }
    #[cfg(windows)]
    let mut cmd = {
        let mut c = Command::new("cmd");
        c.args(["/C", "start", "", trimmed]);
        c
    };
    #[cfg(target_os = "macos")]
    let mut cmd = {
        let mut c = Command::new("open");
        c.arg(trimmed);
        c
    };
    #[cfg(all(unix, not(target_os = "macos")))]
    let mut cmd = {
        let mut c = Command::new("xdg-open");
        c.arg(trimmed);
        c
    };
    configure_hidden_command(&mut cmd);
    cmd.spawn().map_err(|e| format!("打开网页失败: {e}"))?;
    Ok(())
}

fn show_main_window(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
}

pub fn run() {
    let backend = Arc::new(BackendManager::new());
    let zhs_session = Arc::new(ZhsSessionManager::new());
    let close_gate = Arc::new(CloseGate::default());
    let exit_cleaned = Arc::new(AtomicBool::new(false));

    let backend_for_setup = Arc::clone(&backend);
    let backend_for_exit = Arc::clone(&backend);
    let backend_for_tray = Arc::clone(&backend);
    let backend_for_run = Arc::clone(&backend);
    let session_for_exit = Arc::clone(&zhs_session);
    let session_for_tray = Arc::clone(&zhs_session);
    let session_for_run = Arc::clone(&zhs_session);
    let gate_for_window = Arc::clone(&close_gate);
    let cleaned_for_exit = Arc::clone(&exit_cleaned);
    let cleaned_for_tray = Arc::clone(&exit_cleaned);
    let cleaned_for_run = Arc::clone(&exit_cleaned);

    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            show_main_window(app);
        }))
        .manage(AppState {
            backend,
            zhs_session,
            close_gate,
            exit_cleaned,
        })
        .setup(move |app| {
            // 应用启动时在后台静默拉起内置无感后端并建立 SSE 状态流
            backend_for_setup.start_async(app.handle().clone());

            let show_item = MenuItem::with_id(app, "show", "显示主窗口", true, None::<&str>)?;
            let quit_item = MenuItem::with_id(app, "quit", "退出程序", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&show_item, &quit_item])?;

            let mut tray_builder = TrayIconBuilder::new()
                .menu(&menu)
                .show_menu_on_left_click(false)
                .tooltip("知到 · 智慧树自动化学习助手")
                .on_menu_event(move |app, event| match event.id.as_ref() {
                    "show" => show_main_window(app),
                    "quit" => {
                        cleanup_before_exit(
                            &cleaned_for_tray,
                            &session_for_tray,
                            &backend_for_tray,
                        );
                        app.exit(0);
                    }
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        show_main_window(tray.app_handle());
                    }
                });

            if let Some(icon) = app.default_window_icon() {
                tray_builder = tray_builder.icon(icon.clone());
            }
            let _ = tray_builder.build(app);
            Ok(())
        })
        .on_window_event(move |window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                let action = load_close_action();
                match gate_for_window.on_close_requested(action) {
                    CloseDecision::Allow => {
                        cleanup_before_exit(
                            &cleaned_for_exit,
                            &session_for_exit,
                            &backend_for_exit,
                        );
                    }
                    CloseDecision::Minimize => {
                        api.prevent_close();
                        let _ = window.minimize();
                    }
                    CloseDecision::Hide => {
                        api.prevent_close();
                        let _ = window.hide();
                    }
                    CloseDecision::Prompt(id) => {
                        api.prevent_close();
                        let _ = window.emit_to("main", window_close::PROMPT_EVENT, id);
                        let app = window.app_handle().clone();
                        let gate = Arc::clone(&gate_for_window);
                        let cleaned = Arc::clone(&cleaned_for_exit);
                        let session = Arc::clone(&session_for_exit);
                        let backend = Arc::clone(&backend_for_exit);
                        std::thread::spawn(move || {
                            std::thread::sleep(window_close::PROMPT_ACK_TIMEOUT);
                            if gate.expire(id) {
                                cleanup_before_exit(&cleaned, &session, &backend);
                                app.exit(0);
                            }
                        });
                    }
                }
            }
        })
        .invoke_handler(tauri::generate_handler![
            get_runtime_info,
            restart_backend,
            search_schools,
            zhs_login,
            cancel_zhs_login,
            refresh_courses,
            logout_zhs,
            set_close_behavior,
            close_prompt_shown,
            close_choice,
            pick_browser_exe,
            pick_cookie_file,
            pick_directory,
            load_config,
            save_config,
            get_run_state,
            start_autovisor,
            stop_autovisor,
            cancel_shutdown,
            run_diagnostic,
            test_notification,
            test_llm_connection,
            test_question_bank,
            export_question_cache,
            import_question_cache,
            fetch_llm_models,
            set_debug_log,
            start_qr_login,
            poll_qr_login,
            save_qr_image,
            cancel_qr_login,
            check_for_update,
            clear_cookies,
            list_log_files,
            read_log_file,
            open_path,
            open_url,
        ])
        .build(tauri::generate_context!())
        .expect("error while building Autovisor GUI");

    app.run(move |_handle, event| {
        if matches!(event, RunEvent::ExitRequested { .. } | RunEvent::Exit) {
            cleanup_before_exit(&cleaned_for_run, &session_for_run, &backend_for_run);
        }
    });
}
