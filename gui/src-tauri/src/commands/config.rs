use crate::config::{load_config_from_dir, save_config_to_dir, AutovisorConfig, LlmConfig};
use crate::error::AppError;
use crate::{data_dir, AppState};
use std::fs;
use tauri::{AppHandle, State};

#[tauri::command]
pub fn load_config() -> Result<AutovisorConfig, AppError> {
    let base_dir = data_dir();
    load_config_from_dir(base_dir.as_path()).map_err(AppError::Config)
}

#[tauri::command]
pub fn save_config(config: AutovisorConfig) -> Result<AutovisorConfig, AppError> {
    let base_dir = data_dir();
    save_config_to_dir(&base_dir, &config).map_err(AppError::Config)
}

#[tauri::command]
pub async fn test_notification(
    app: AppHandle,
    state: State<'_, AppState>,
    provider: String,
    url: String,
    tg_chat_id: String,
    proxy: String,
) -> Result<String, AppError> {
    let payload = serde_json::json!({
        "provider": provider,
        "url": url,
        "tgChatId": tg_chat_id,
        "proxy": proxy,
    });
    let resp = state
        .backend
        .post_json_async(Some(&app), "/api/notify/test", &payload, 25)
        .await?;
    resp.get("message")
        .and_then(|v| v.as_str())
        .map(String::from)
        .ok_or_else(|| AppError::backend("推送测试返回格式异常"))
}

#[tauri::command]
pub async fn test_llm_connection(
    app: AppHandle,
    state: State<'_, AppState>,
    llm: LlmConfig,
    proxy: String,
) -> Result<String, AppError> {
    let payload = serde_json::json!({
        "llm": llm,
        "proxy": proxy,
    });
    let resp = state
        .backend
        .post_json_async(Some(&app), "/api/llm/test", &payload, 40)
        .await?;
    resp.get("message")
        .and_then(|v| v.as_str())
        .map(String::from)
        .ok_or_else(|| AppError::backend("模型连接测试返回格式异常"))
}

#[tauri::command]
pub async fn test_question_bank(
    app: AppHandle,
    state: State<'_, AppState>,
    token: String,
    url: String,
    proxy: String,
) -> Result<String, AppError> {
    let payload = serde_json::json!({
        "token": token,
        "url": url,
        "proxy": proxy,
    });
    let resp = state
        .backend
        .post_json_async(Some(&app), "/api/question-bank/test", &payload, 25)
        .await?;
    let ok = resp.get("ok").and_then(|v| v.as_bool()).unwrap_or(false);
    let message = resp
        .get("message")
        .and_then(|v| v.as_str())
        .unwrap_or("题库连通性测试返回格式异常");
    if ok {
        Ok(message.to_string())
    } else {
        Err(AppError::backend(message))
    }
}

#[tauri::command]
pub async fn export_question_cache(
    app: AppHandle,
    state: State<'_, AppState>,
    course_id: Option<String>,
) -> Result<Option<String>, AppError> {
    let payload = serde_json::json!({ "courseId": course_id.unwrap_or_default() });
    let resp = state
        .backend
        .post_json_async(Some(&app), "/api/cache/export", &payload, 60)
        .await?;
    let data = resp
        .get("data")
        .cloned()
        .ok_or_else(|| AppError::backend("导出题库缓存返回格式异常"))?;
    let Some(handle) = rfd::AsyncFileDialog::new()
        .set_title("导出 SQLite 题库缓存")
        .add_filter("JSON 文件", &["json"])
        .set_file_name("autovisor-question-bank.json")
        .save_file()
        .await
    else {
        return Ok(None);
    };
    let path = handle.path().to_path_buf();
    tauri::async_runtime::spawn_blocking(move || -> Result<Option<String>, AppError> {
        let text = serde_json::to_string_pretty(&data)
            .map_err(|e| AppError::io(format!("序列化题库缓存失败: {e}")))?;
        fs::write(&path, text).map_err(|e| AppError::io(format!("写入题库缓存失败: {e}")))?;
        Ok(Some(path.to_string_lossy().to_string()))
    })
    .await
    .map_err(|e| AppError::internal(format!("保存题库缓存文件异常: {e}")))?
}

#[tauri::command]
pub async fn import_question_cache(
    app: AppHandle,
    state: State<'_, AppState>,
) -> Result<Option<serde_json::Value>, AppError> {
    let Some(handle) = rfd::AsyncFileDialog::new()
        .set_title("导入 SQLite 题库缓存")
        .add_filter("JSON 文件", &["json"])
        .pick_file()
        .await
    else {
        return Ok(None);
    };
    let path = handle.path().to_path_buf();
    let body = tauri::async_runtime::spawn_blocking(move || -> Result<serde_json::Value, AppError> {
        let raw = fs::read_to_string(&path)
            .map_err(|e| AppError::io(format!("读取题库缓存失败: {e}")))?;
        let parsed: serde_json::Value = serde_json::from_str(raw.trim_start_matches('\u{feff}'))
            .map_err(|e| AppError::validation(format!("解析题库缓存失败: {e}")))?;
        let body = if parsed.get("data").is_some() {
            parsed
        } else {
            serde_json::json!({ "data": parsed })
        };
        Ok(body)
    })
    .await
    .map_err(|e| AppError::internal(format!("读取题库缓存文件异常: {e}")))??;
    let resp = state
        .backend
        .post_json_async(Some(&app), "/api/cache/import", &body, 60)
        .await?;
    Ok(Some(resp))
}

#[tauri::command]
pub async fn clear_question_cache(
    app: AppHandle,
    state: State<'_, AppState>,
    course_id: Option<String>,
) -> Result<serde_json::Value, AppError> {
    let payload = serde_json::json!({ "courseId": course_id.unwrap_or_default() });
    state
        .backend
        .post_json_async(Some(&app), "/api/cache/clear", &payload, 30)
        .await
}

#[tauri::command]
pub async fn fetch_llm_models(
    app: AppHandle,
    state: State<'_, AppState>,
    llm: LlmConfig,
    proxy: String,
) -> Result<Vec<String>, AppError> {
    let payload = serde_json::json!({
        "llm": llm,
        "proxy": proxy,
    });
    let resp = state
        .backend
        .post_json_async(Some(&app), "/api/llm/models", &payload, 30)
        .await?;
    resp.get("models")
        .and_then(|v| v.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|item| item.as_str().map(String::from))
                .collect()
        })
        .ok_or_else(|| AppError::backend("拉取模型列表返回格式异常"))
}
