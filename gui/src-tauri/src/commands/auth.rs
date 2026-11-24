use crate::config::load_config_from_dir;
use crate::error::AppError;
use crate::runtime::{self, update_prefs, RuntimeInfo};
use crate::zhs::{
    execute_zhs_login, search_schools_async, LoginRequest, SchoolItem, SessionResult,
};
use crate::{data_dir, AppState};
use std::fs;
use std::sync::Arc;
use tauri::{AppHandle, State};

#[tauri::command]
pub async fn search_schools(
    query: String,
    proxy: Option<String>,
) -> Result<Vec<SchoolItem>, AppError> {
    let base_dir = data_dir();
    let effective_proxy = proxy
        .map(|p| p.trim().to_string())
        .filter(|p| !p.is_empty())
        .unwrap_or_else(|| {
            load_config_from_dir(&base_dir)
                .map(|c| c.proxy)
                .unwrap_or_default()
        });
    search_schools_async(&query, &effective_proxy).await
}

#[tauri::command]
pub async fn zhs_login(
    app: AppHandle,
    state: State<'_, AppState>,
    request: LoginRequest,
) -> Result<SessionResult, AppError> {
    let base_dir = data_dir();
    let manager = Arc::clone(&state.zhs_session);
    let backend = Arc::clone(&state.backend);
    tauri::async_runtime::spawn_blocking(move || {
        execute_zhs_login(&app, &manager, &backend, &base_dir, request)
    })
    .await
    .map_err(|e| AppError::Auth(format!("登录任务执行异常: {e}")))?
    .map_err(AppError::Auth)
}

#[tauri::command]
pub fn cancel_zhs_login(state: State<'_, AppState>) {
    state.zhs_session.cancel();
}

#[tauri::command]
pub async fn refresh_courses(
    app: AppHandle,
    state: State<'_, AppState>,
) -> Result<SessionResult, AppError> {
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
    .map_err(|e| AppError::Auth(format!("刷新课程任务异常: {e}")))?
    .map_err(AppError::Auth)
}

#[tauri::command]
pub fn logout_zhs(state: State<'_, AppState>) -> Result<RuntimeInfo, AppError> {
    state.zhs_session.cancel();
    let base_dir = data_dir();
    let cookie_file = base_dir.join("data").join("cookies.json");
    if cookie_file.is_file() {
        let _ = fs::remove_file(&cookie_file);
    }
    update_prefs(|prefs| {
        prefs.session = None;
    })
    .map_err(AppError::Config)?;
    Ok(runtime::get_runtime_info(&state.backend.status()))
}

#[tauri::command]
pub fn clear_saved_login(
    state: State<'_, AppState>,
    mode: Option<String>,
) -> Result<RuntimeInfo, AppError> {
    update_prefs(|prefs| match mode.as_deref().map(str::trim) {
        Some("phone") => {
            if let Some(ref mut saved) = prefs.saved_login {
                saved.remember_phone = false;
                saved.phone_username.clear();
                saved.phone_password.clear();
            }
        }
        Some("student") => {
            if let Some(ref mut saved) = prefs.saved_login {
                saved.remember_student = false;
                saved.school_id.clear();
                saved.school_name.clear();
                saved.student_id.clear();
                saved.student_password.clear();
            }
        }
        _ => {
            prefs.saved_login = None;
        }
    })
    .map_err(AppError::Config)?;
    Ok(runtime::get_runtime_info(&state.backend.status()))
}

#[tauri::command]
pub fn clear_cookies(state: State<'_, AppState>) -> Result<RuntimeInfo, AppError> {
    let base_dir = data_dir();
    let cookie_file = base_dir.join("data").join("cookies.json");
    if cookie_file.is_file() {
        fs::remove_file(&cookie_file).map_err(|e| AppError::io(format!("清除 Cookie 失败: {e}")))?;
    }
    let _ = update_prefs(|prefs| {
        prefs.session = None;
    });
    Ok(runtime::get_runtime_info(&state.backend.status()))
}

#[tauri::command]
pub async fn start_qr_login(
    app: AppHandle,
    state: State<'_, AppState>,
    proxy: String,
    save_dir: Option<String>,
) -> Result<serde_json::Value, AppError> {
    let payload = serde_json::json!({
        "proxy": proxy,
        "saveDir": save_dir.unwrap_or_default(),
    });
    state
        .backend
        .post_json_async(Some(&app), "/api/qr-login/start", &payload, 20)
        .await
}

#[tauri::command]
pub async fn poll_qr_login(
    app: AppHandle,
    state: State<'_, AppState>,
    session_id: String,
) -> Result<serde_json::Value, AppError> {
    let payload = serde_json::json!({ "sessionId": session_id });
    state
        .backend
        .post_json_async(Some(&app), "/api/qr-login/poll", &payload, 20)
        .await
}

#[tauri::command]
pub async fn save_qr_image(
    app: AppHandle,
    state: State<'_, AppState>,
    session_id: String,
    save_dir: String,
) -> Result<serde_json::Value, AppError> {
    let payload = serde_json::json!({
        "sessionId": session_id,
        "saveDir": save_dir,
    });
    state
        .backend
        .post_json_async(Some(&app), "/api/qr-login/save", &payload, 15)
        .await
}

#[tauri::command]
pub async fn cancel_qr_login(
    app: AppHandle,
    state: State<'_, AppState>,
    session_id: String,
) -> Result<(), AppError> {
    let payload = serde_json::json!({ "sessionId": session_id });
    let _ = state
        .backend
        .post_json_async(Some(&app), "/api/qr-login/cancel", &payload, 10)
        .await;
    Ok(())
}
