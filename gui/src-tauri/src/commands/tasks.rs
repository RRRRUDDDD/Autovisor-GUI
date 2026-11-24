use crate::backend::RunState;
use crate::config::load_config_from_dir;
use crate::error::AppError;
use crate::{data_dir, AppState};
use std::path::PathBuf;
use tauri::{AppHandle, State};

#[tauri::command]
pub fn get_run_state(state: State<'_, AppState>) -> RunState {
    state.backend.snapshot()
}

#[tauri::command]
pub async fn start_autovisor(
    app: AppHandle,
    state: State<'_, AppState>,
) -> Result<RunState, AppError> {
    let base_dir = data_dir();
    let cfg = load_config_from_dir(&base_dir).map_err(AppError::Config)?;
    let has_enabled = cfg.courses.iter().any(|c| c.enabled);
    if !has_enabled {
        return Err(AppError::validation("请至少勾选一门课程后再开始刷课"));
    }
    state.backend.start_task_async(app, "study", "").await
}

#[tauri::command]
pub async fn stop_autovisor(
    app: AppHandle,
    state: State<'_, AppState>,
) -> Result<RunState, AppError> {
    Ok(state.backend.stop_task_async(Some(&app)).await)
}

#[tauri::command]
pub async fn cancel_shutdown(
    app: AppHandle,
    state: State<'_, AppState>,
) -> Result<RunState, AppError> {
    state.backend.cancel_shutdown_async(Some(&app)).await
}

#[tauri::command]
pub async fn run_diagnostic(
    app: AppHandle,
    state: State<'_, AppState>,
    kind: String,
    target: String,
) -> Result<RunState, AppError> {
    let cleaned_target = match kind.as_str() {
        "check_browser" => String::new(),
        "check_course" => {
            let trimmed = target.trim();
            if !trimmed.starts_with("https://") {
                return Err(AppError::validation("请提供有效的 https:// 课程播放页链接"));
            }
            trimmed.to_string()
        }
        "import_cookies" => {
            let trimmed = target.trim();
            if trimmed.is_empty() || !PathBuf::from(trimmed).is_file() {
                return Err(AppError::validation("所选 Cookie JSON 文件不存在"));
            }
            trimmed.to_string()
        }
        _ => return Err(AppError::validation(format!("未知诊断类型: {kind}"))),
    };
    state
        .backend
        .start_task_async(app, &kind, &cleaned_target)
        .await
}

#[tauri::command]
pub async fn set_debug_log(
    app: AppHandle,
    state: State<'_, AppState>,
    enabled: bool,
) -> Result<bool, AppError> {
    let payload = serde_json::json!({ "enabled": enabled });
    let resp = state
        .backend
        .post_json_async(Some(&app), "/api/tasks/debug", &payload, 10)
        .await?;
    Ok(resp
        .get("enabled")
        .and_then(|v| v.as_bool())
        .unwrap_or(enabled))
}
