use crate::error::AppError;
use crate::runtime::{self, read_log_tail_scoped, LogFileItem, RuntimeInfo};
use crate::{data_dir, AppState};
use std::path::PathBuf;
use std::sync::Arc;
use tauri::{AppHandle, State};

#[tauri::command]
pub fn get_runtime_info(
    state: State<'_, AppState>,
    refresh_browsers: Option<bool>,
) -> RuntimeInfo {
    runtime::get_runtime_info_with_refresh(
        &state.backend.status(),
        refresh_browsers.unwrap_or(false),
    )
}

#[tauri::command]
pub async fn restart_backend(
    app: AppHandle,
    state: State<'_, AppState>,
) -> Result<RuntimeInfo, AppError> {
    let backend = Arc::clone(&state.backend);
    let app_clone = app.clone();
    tauri::async_runtime::spawn_blocking(move || backend.restart(app_clone))
        .await
        .map_err(|e| AppError::backend(format!("重启内置引擎异常: {e}")))?
        .map_err(AppError::backend)?;
    Ok(runtime::get_runtime_info(&state.backend.status()))
}

#[tauri::command]
pub async fn check_for_update(
    app: AppHandle,
    state: State<'_, AppState>,
    proxy: String,
) -> Result<serde_json::Value, AppError> {
    let payload = serde_json::json!({
        "proxy": proxy,
        "currentVersion": env!("CARGO_PKG_VERSION"),
    });
    state
        .backend
        .post_json_async(Some(&app), "/api/update/check", &payload, 15)
        .await
}

#[tauri::command]
pub fn list_log_files() -> Vec<LogFileItem> {
    runtime::list_log_files(data_dir().as_path())
}

#[tauri::command]
pub fn read_log_file(path: String) -> Result<String, AppError> {
    read_log_tail_scoped(
        data_dir().as_path(),
        PathBuf::from(path.trim()).as_path(),
        128 * 1024,
    )
    .map_err(AppError::Io)
}
