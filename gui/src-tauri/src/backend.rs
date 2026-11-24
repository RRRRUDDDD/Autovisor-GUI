use crate::error::AppError;
use crate::process_tree::{configure_hidden_command, prepare_process_tree_command, Job};
use crate::runtime::{resolve_app_paths, BackendLaunchSpec};
use serde::{Deserialize, Serialize};
use std::collections::hash_map::RandomState;
use std::fs::OpenOptions;
use std::hash::{BuildHasher, Hasher};
use std::io::{BufRead, BufReader, Read, Write};
use std::path::PathBuf;
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc;
use std::sync::{Arc, Condvar, Mutex, MutexGuard};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tauri::{AppHandle, Emitter};

fn emit_to_app<S: Serialize + Clone>(app: Option<&AppHandle>, event: &str, payload: &S) {
    if let Some(handle) = app {
        let _ = handle.emit(event, payload);
    }
}

const HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(25);
const READY_WAIT_TIMEOUT: Duration = Duration::from_secs(20);
const MAX_STDERR_BYTES: u64 = 2 * 1024 * 1024;
const LOG_LIMIT: usize = 800;

pub(crate) fn lock_unpoisoned<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct LogEntry {
    pub seq: u64,
    pub timestamp: f64,
    pub level: String,
    pub message: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct CompletedLesson {
    pub title: String,
    pub time: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ShutdownInfo {
    pub state: String, // scheduled | cancelled | failed
    #[serde(default)]
    pub delay_seconds: Option<u64>,
    #[serde(default)]
    pub scheduled_at: Option<f64>,
    #[serde(default)]
    pub shutdown_at: Option<f64>,
    #[serde(default)]
    pub error: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct RunState {
    #[serde(default)]
    pub run_id: u64,
    #[serde(default)]
    pub revision: u64,
    pub status: String, // idle | running | stopping | completed | time_limit | error | cancelled
    pub mode: String,   // study | check_browser | check_course | import_cookies
    #[serde(default)]
    pub review_mode: bool,
    #[serde(default)]
    pub shutdown_on_complete: bool,
    #[serde(default)]
    pub shutdown: Option<ShutdownInfo>,
    pub started_at: Option<f64>,
    pub finished_at: Option<f64>,
    pub exit_code: Option<i32>,
    pub stage: String,
    pub course_index: u32,
    pub course_total: u32,
    pub current_course: String,
    pub catalog_type: String,
    pub current_lesson: String,
    pub lesson_progress: f64,
    pub progress_desc: String,
    pub remaining_minutes: Option<f64>,
    pub course_elapsed_min: f64,
    pub completed_lessons: Vec<CompletedLesson>,
    pub needs_attention: String,
    pub error_message: String,
    pub log_file_path: String,
    pub logs: Vec<LogEntry>,
    pub logs_truncated: bool,
}

impl Default for RunState {
    fn default() -> Self {
        Self {
            run_id: 0,
            revision: 0,
            status: "idle".to_string(),
            mode: "study".to_string(),
            review_mode: false,
            shutdown_on_complete: false,
            shutdown: None,
            started_at: None,
            finished_at: None,
            exit_code: None,
            stage: "就绪".to_string(),
            course_index: 0,
            course_total: 0,
            current_course: String::new(),
            catalog_type: String::new(),
            current_lesson: String::new(),
            lesson_progress: 0.0,
            progress_desc: String::new(),
            remaining_minutes: None,
            course_elapsed_min: 0.0,
            completed_lessons: Vec::new(),
            needs_attention: String::new(),
            error_message: String::new(),
            log_file_path: String::new(),
            logs: Vec::new(),
            logs_truncated: false,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct BackendStatusSnapshot {
    pub phase: String, // starting | ready | stopping | stopped | failed
    pub version: String,
    pub backend_version: String,
    pub error: String,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct HandshakePayload {
    event: String,
    version: u32,
    port: u16,
    #[serde(default)]
    app_version: String,
}

struct BackendInner {
    generation: u64,
    phase: String,
    version: String,
    backend_version: String,
    error: String,
    port: Option<u16>,
    token: String,
    child: Option<Child>,
    child_stdin: Option<ChildStdin>,
    job: Option<Arc<Job>>,
    last_state: RunState,
    shutting_down: bool,
}

pub struct BackendManager {
    inner: Arc<Mutex<BackendInner>>,
    ready_cvar: Arc<Condvar>,
    lifecycle_lock: Mutex<()>,
    api_client: reqwest::blocking::Client,
    async_api_client: reqwest::Client,
    sse_client: reqwest::blocking::Client,
}

impl BackendManager {
    pub fn new() -> Self {
        let api_client = reqwest::blocking::Client::builder()
            .no_proxy()
            .connect_timeout(Duration::from_secs(3))
            .timeout(Duration::from_secs(15))
            .build()
            .expect("failed to build loopback api client");

        let async_api_client = reqwest::Client::builder()
            .no_proxy()
            .connect_timeout(Duration::from_secs(3))
            .timeout(Duration::from_secs(15))
            .build()
            .expect("failed to build async loopback api client");

        let sse_client = reqwest::blocking::Client::builder()
            .no_proxy()
            .connect_timeout(Duration::from_secs(3))
            .build()
            .expect("failed to build loopback sse client");

        Self {
            inner: Arc::new(Mutex::new(BackendInner {
                generation: 0,
                phase: "starting".to_string(),
                version: env!("CARGO_PKG_VERSION").to_string(),
                backend_version: "3.20.0(Autovisor GUI Edition)".to_string(),
                error: String::new(),
                port: None,
                token: String::new(),
                child: None,
                child_stdin: None,
                job: None,
                last_state: RunState::default(),
                shutting_down: false,
            })),
            ready_cvar: Arc::new(Condvar::new()),
            lifecycle_lock: Mutex::new(()),
            api_client,
            async_api_client,
            sse_client,
        }
    }

    pub fn status(&self) -> BackendStatusSnapshot {
        let guard = lock_unpoisoned(&self.inner);
        BackendStatusSnapshot {
            phase: guard.phase.clone(),
            version: guard.version.clone(),
            backend_version: guard.backend_version.clone(),
            error: guard.error.clone(),
        }
    }

    pub fn snapshot(&self) -> RunState {
        let guard = lock_unpoisoned(&self.inner);
        guard.last_state.clone()
    }

    /// 在后台线程中无感拉起内置后端并建立 SSE 状态订阅。
    pub fn start_async(self: &Arc<Self>, app: AppHandle) {
        let this = Arc::clone(self);
        thread::spawn(move || {
            let _ = this.spawn_and_connect(Some(app));
        });
    }

    /// 重启内置后端（用于异常自愈或用户手动刷新服务）。
    pub fn restart(self: &Arc<Self>, app: AppHandle) -> Result<BackendStatusSnapshot, String> {
        let _lifecycle = lock_unpoisoned(&self.lifecycle_lock);
        self.terminate_current_process_locked(false);
        self.spawn_and_connect_locked(Some(app))?;
        Ok(self.status())
    }

    fn spawn_and_connect(&self, app: Option<AppHandle>) -> Result<(u16, String), String> {
        let _lifecycle = lock_unpoisoned(&self.lifecycle_lock);
        self.spawn_and_connect_locked(app)
    }

    fn spawn_and_connect_locked(&self, app: Option<AppHandle>) -> Result<(u16, String), String> {
        let paths = resolve_app_paths();
        let launch = match paths.backend_launch {
            Some(spec) => spec,
            None => {
                let msg = "未找到内置学习引擎组件（runtime/autovisor-backend.exe），请重新解压完整程序包。".to_string();
                self.set_failed_for_generation(None, &msg, app.as_ref());
                return Err(msg);
            }
        };

        let token = generate_token();
        let job = match Job::create() {
            Ok(j) => Arc::new(j),
            Err(e) => {
                self.set_failed_for_generation(None, &e, app.as_ref());
                return Err(e);
            }
        };

        let generation = {
            let mut guard = lock_unpoisoned(&self.inner);
            if guard.shutting_down {
                return Err("程序正在退出".to_string());
            }
            if guard.phase == "ready" {
                if let Some(p) = guard.port {
                    return Ok((p, guard.token.clone()));
                }
            }
            guard.generation += 1;
            guard.phase = "starting".to_string();
            guard.error.clear();
            guard.port = None;
            guard.token = token.clone();
            guard.generation
        };
        self.emit_status(app.as_ref());

        let mut cmd = match &launch {
            BackendLaunchSpec::FrozenExe(exe) => {
                #[cfg(unix)]
                {
                    use std::os::unix::fs::PermissionsExt;
                    if let Ok(meta) = std::fs::metadata(exe) {
                        let mode = meta.permissions().mode();
                        if mode & 0o111 == 0 {
                            let mut perms = meta.permissions();
                            perms.set_mode(mode | 0o755);
                            let _ = std::fs::set_permissions(exe, perms);
                        }
                    }
                }
                let mut c = Command::new(exe);
                c.current_dir(&paths.data_dir);
                c
            }
            BackendLaunchSpec::PythonScript { python, script } => {
                let mut c = Command::new(python);
                c.arg(script).current_dir(&paths.data_dir);
                c
            }
        };

        cmd.arg("--data-dir")
            .arg(&paths.data_dir)
            .arg("--resources-dir")
            .arg(&paths.resources_dir)
            .arg("--host")
            .arg("127.0.0.1")
            .arg("--port")
            .arg("0")
            .arg("--auth-stdin")
            .env("PYTHONUNBUFFERED", "1")
            .env("PYTHONIOENCODING", "utf-8")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        configure_hidden_command(&mut cmd);
        prepare_process_tree_command(&mut cmd);

        let mut child = match cmd.spawn() {
            Ok(c) => c,
            Err(e) => {
                let msg = format!("启动内置学习引擎失败: {e}");
                self.set_failed_for_generation(Some(generation), &msg, app.as_ref());
                return Err(msg);
            }
        };

        if let Err(e) = job.assign(child.id()) {
            let _ = child.kill();
            let _ = child.wait();
            let msg = format!("绑定内置学习引擎进程树失败: {e}");
            self.set_failed_for_generation(Some(generation), &msg, app.as_ref());
            return Err(msg);
        }

        let mut stdin = child.stdin.take();
        if let Some(ref mut stdin_stream) = stdin {
            let handshake_payload = serde_json::json!({ "authToken": &token }).to_string();
            if let Err(e) = writeln!(stdin_stream, "{handshake_payload}")
                .and_then(|_| stdin_stream.flush())
            {
                let msg = format!("向内置学习引擎传递认证令牌失败: {e}");
                drop(stdin);
                job.terminate_with_child(Some(&mut child));
                let _ = child.kill();
                let _ = child.wait();
                self.set_failed_for_generation(Some(generation), &msg, app.as_ref());
                return Err(msg);
            }
        } else {
            let msg = "无法获取内置学习引擎输入管道".to_string();
            job.terminate_with_child(Some(&mut child));
            let _ = child.kill();
            let _ = child.wait();
            self.set_failed_for_generation(Some(generation), &msg, app.as_ref());
            return Err(msg);
        }
        let stdout = child.stdout.take();
        let stderr = child.stderr.take();

        // 后台持续排空 stderr 并追加/轮转落盘到 logs/backend-stderr.log，避免管道满阻塞
        if let Some(err_stream) = stderr {
            let log_path = paths.data_dir.join("logs").join("backend-stderr.log");
            thread::spawn(move || drain_stderr_with_limit(err_stream, log_path, MAX_STDERR_BYTES));
        }

        // 在独立线程读取单行握手 JSON，主线程带超时等待
        let (tx, rx) = mpsc::channel::<Result<String, String>>();
        if let Some(out_stream) = stdout {
            thread::spawn(move || {
                let mut reader = BufReader::new(out_stream);
                let mut line = String::new();
                match reader.read_line(&mut line) {
                    Ok(0) => {
                        let _ = tx.send(Err("内置学习引擎在就绪前意外退出".to_string()));
                    }
                    Ok(_) => {
                        let _ = tx.send(Ok(line));
                        // 继续排空后续可能的 stdout 字节，避免阻塞子进程
                        let mut sink = [0u8; 1024];
                        while matches!(reader.read(&mut sink), Ok(n) if n > 0) {}
                    }
                    Err(e) => {
                        let _ = tx.send(Err(format!("读取内置学习引擎握手失败: {e}")));
                    }
                }
            });
        } else {
            let msg = "无法获取内置学习引擎输出管道".to_string();
            job.terminate_with_child(Some(&mut child));
            let _ = child.kill();
            let _ = child.wait();
            self.set_failed_for_generation(Some(generation), &msg, app.as_ref());
            return Err(msg);
        }

        {
            let mut guard = lock_unpoisoned(&self.inner);
            if guard.generation != generation || guard.shutting_down {
                drop(guard);
                drop(stdin);
                job.terminate_with_child(Some(&mut child));
                let _ = child.kill();
                let _ = child.wait();
                return Err("内置学习引擎状态已变更".to_string());
            }
            guard.child = Some(child);
            guard.child_stdin = stdin;
            guard.job = Some(Arc::clone(&job));
        }

        let handshake_deadline = Instant::now() + HANDSHAKE_TIMEOUT;
        let line = loop {
            if lock_unpoisoned(&self.inner).shutting_down {
                self.terminate_for_generation_locked(generation, true);
                return Err("程序正在退出".to_string());
            }
            let remaining = handshake_deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                let msg = "内置学习引擎启动超时，请检查杀毒软件是否拦截。".to_string();
                self.terminate_for_generation_locked(generation, false);
                self.set_failed_for_generation(Some(generation), &msg, app.as_ref());
                return Err(msg);
            }
            let slice = remaining.min(Duration::from_millis(100));
            match rx.recv_timeout(slice) {
                Ok(Ok(l)) => break l,
                Ok(Err(e)) => {
                    if lock_unpoisoned(&self.inner).shutting_down {
                        self.terminate_for_generation_locked(generation, true);
                        return Err("程序正在退出".to_string());
                    }
                    self.terminate_for_generation_locked(generation, false);
                    self.set_failed_for_generation(Some(generation), &e, app.as_ref());
                    return Err(e);
                }
                Err(mpsc::RecvTimeoutError::Timeout) => continue,
                Err(mpsc::RecvTimeoutError::Disconnected) => {
                    if lock_unpoisoned(&self.inner).shutting_down {
                        self.terminate_for_generation_locked(generation, true);
                        return Err("程序正在退出".to_string());
                    }
                    let msg = "内置学习引擎在完成握手前已退出".to_string();
                    self.terminate_for_generation_locked(generation, false);
                    self.set_failed_for_generation(Some(generation), &msg, app.as_ref());
                    return Err(msg);
                }
            }
        };

        let handshake: HandshakePayload =
            match serde_json::from_str::<HandshakePayload>(line.trim()) {
                Ok(h) if h.event == "autovisor-backend-ready" && h.version == 1 && h.port > 0 => h,
                Ok(_) => {
                    let msg = "内置学习引擎握手协议不匹配".to_string();
                    self.terminate_for_generation_locked(generation, false);
                    self.set_failed_for_generation(Some(generation), &msg, app.as_ref());
                    return Err(msg);
                }
                Err(e) => {
                    let msg = format!("解析内置学习引擎握手失败: {e}");
                    self.terminate_for_generation_locked(generation, false);
                    self.set_failed_for_generation(Some(generation), &msg, app.as_ref());
                    return Err(msg);
                }
            };

        // 校验 /api/health
        let health_url = format!("http://127.0.0.1:{}/api/health", handshake.port);
        let health_ok = self
            .api_client
            .get(&health_url)
            .header("X-Autovisor-Token", &token)
            .send()
            .map(|r| r.status().is_success())
            .unwrap_or(false);

        if !health_ok {
            let msg = "内置学习引擎健康检查失败".to_string();
            self.terminate_for_generation_locked(generation, false);
            self.set_failed_for_generation(Some(generation), &msg, app.as_ref());
            return Err(msg);
        }

        {
            let mut guard = lock_unpoisoned(&self.inner);
            if guard.generation != generation || guard.shutting_down {
                drop(guard);
                self.terminate_for_generation_locked(generation, false);
                return Err("内置学习引擎状态已变更".to_string());
            }
            guard.phase = "ready".to_string();
            guard.error.clear();
            guard.port = Some(handshake.port);
            if !handshake.app_version.is_empty() {
                guard.backend_version = handshake.app_version;
            }
            self.ready_cvar.notify_all();
        }
        self.emit_status(app.as_ref());

        // 建立 SSE 实时状态长连接
        let inner_ref = Arc::clone(&self.inner);
        let cvar_ref = Arc::clone(&self.ready_cvar);
        let sse_client = self.sse_client.clone();
        let port = handshake.port;
        let sse_token = token.clone();
        let sse_app = app.clone();
        thread::spawn(move || {
            run_sse_loop(
                inner_ref,
                cvar_ref,
                sse_client,
                generation,
                port,
                sse_token,
                sse_app,
            );
        });

        Ok((handshake.port, token))
    }

    fn wait_for_ready_or_needs_spawn(
        &self,
        timeout: Duration,
    ) -> Result<Option<(u16, String)>, String> {
        let deadline = Instant::now() + timeout;
        let mut guard = lock_unpoisoned(&self.inner);
        loop {
            if guard.shutting_down {
                return Err("程序正在退出".to_string());
            }
            if guard.phase == "ready" {
                if let Some(p) = guard.port {
                    return Ok(Some((p, guard.token.clone())));
                }
            }
            if guard.phase == "failed" || guard.phase == "stopped" {
                return Ok(None);
            }

            let now = Instant::now();
            if now >= deadline {
                let msg = if guard.error.is_empty() {
                    "等待内置学习引擎就绪超时，请稍后重试".to_string()
                } else {
                    guard.error.clone()
                };
                return Err(msg);
            }

            let remaining = deadline.saturating_duration_since(now);
            let (next_guard, _) = self
                .ready_cvar
                .wait_timeout(guard, remaining)
                .unwrap_or_else(|e| e.into_inner());
            guard = next_guard;
        }
    }

    fn ensure_ready(&self, app: Option<&AppHandle>) -> Result<(u16, String), String> {
        match self.wait_for_ready_or_needs_spawn(READY_WAIT_TIMEOUT)? {
            Some(ready) => Ok(ready),
            None => self.spawn_and_connect(app.cloned()),
        }
    }

    async fn ensure_ready_async(
        self: &Arc<Self>,
        app: Option<&AppHandle>,
    ) -> Result<(u16, String), AppError> {
        {
            let guard = lock_unpoisoned(&self.inner);
            if guard.shutting_down {
                return Err(AppError::backend("程序正在退出"));
            }
            if guard.phase == "ready" {
                if let Some(p) = guard.port {
                    return Ok((p, guard.token.clone()));
                }
            }
        }
        let this = Arc::clone(self);
        let app_owned = app.cloned();
        tauri::async_runtime::spawn_blocking(move || this.ensure_ready(app_owned.as_ref()))
            .await
            .map_err(|e| AppError::backend(format!("等待内置学习引擎异常: {e}")))?
            .map_err(AppError::backend)
    }

    pub fn post_json(
        &self,
        app: Option<&AppHandle>,
        path: &str,
        payload: &serde_json::Value,
        timeout_secs: u64,
    ) -> Result<serde_json::Value, String> {
        let (port, token) = self.ensure_ready(app)?;
        let url = format!("http://127.0.0.1:{port}{path}");
        let resp = self
            .api_client
            .post(&url)
            .header("X-Autovisor-Token", &token)
            .timeout(Duration::from_secs(timeout_secs))
            .json(payload)
            .send()
            .map_err(|e| format!("连接内置学习引擎失败: {e}"))?;

        let status_code = resp.status();
        let body_text = resp
            .text()
            .map_err(|e| format!("读取内置学习引擎响应失败: {e}"))?;
        let parsed = serde_json::from_str::<serde_json::Value>(&body_text).ok();

        if !status_code.is_success() {
            let msg = parsed
                .as_ref()
                .and_then(|v| v.get("error").and_then(|e| e.as_str()).map(String::from))
                .unwrap_or_else(|| format!("请求失败（HTTP {status_code}）"));
            return Err(msg);
        }

        parsed.ok_or_else(|| "内置学习引擎返回了非 JSON 响应".to_string())
    }

    pub async fn post_json_async(
        self: &Arc<Self>,
        app: Option<&AppHandle>,
        path: &str,
        payload: &serde_json::Value,
        timeout_secs: u64,
    ) -> Result<serde_json::Value, AppError> {
        let (port, token) = self.ensure_ready_async(app).await?;
        let url = format!("http://127.0.0.1:{port}{path}");
        let resp = self
            .async_api_client
            .post(&url)
            .header("X-Autovisor-Token", &token)
            .timeout(Duration::from_secs(timeout_secs))
            .json(payload)
            .send()
            .await
            .map_err(|e| AppError::backend(format!("连接内置学习引擎失败: {e}")))?;

        let status_code = resp.status();
        let body_text = resp
            .text()
            .await
            .map_err(|e| AppError::backend(format!("读取内置学习引擎响应失败: {e}")))?;
        let parsed = serde_json::from_str::<serde_json::Value>(&body_text).ok();

        if !status_code.is_success() {
            let msg = parsed
                .as_ref()
                .and_then(|v| v.get("error").and_then(|e| e.as_str()).map(String::from))
                .unwrap_or_else(|| format!("请求失败（HTTP {status_code}）"));
            return Err(AppError::backend(msg));
        }

        parsed.ok_or_else(|| AppError::backend("内置学习引擎返回了非 JSON 响应"))
    }

    pub async fn start_task_async(
        self: &Arc<Self>,
        app: AppHandle,
        mode: &str,
        target: &str,
    ) -> Result<RunState, AppError> {
        let (port, token) = self.ensure_ready_async(Some(&app)).await?;
        let url = format!("http://127.0.0.1:{port}/api/tasks/start");
        let resp = self
            .async_api_client
            .post(&url)
            .header("X-Autovisor-Token", &token)
            .json(&serde_json::json!({
                "mode": mode,
                "target": target,
            }))
            .send()
            .await
            .map_err(|e| AppError::backend(format!("连接内置学习引擎失败: {e}")))?;

        let status_code = resp.status();
        let body_text = resp
            .text()
            .await
            .map_err(|e| AppError::backend(format!("读取内置学习引擎响应失败: {e}")))?;

        if !status_code.is_success() {
            let msg = serde_json::from_str::<serde_json::Value>(&body_text)
                .ok()
                .and_then(|v| v.get("error").and_then(|e| e.as_str()).map(String::from))
                .unwrap_or_else(|| format!("启动任务失败（HTTP {status_code}）"));
            return Err(AppError::backend(msg));
        }

        let state: RunState = serde_json::from_str(&body_text)
            .map_err(|e| AppError::backend(format!("解析任务状态失败: {e}")))?;
        let (final_state, should_emit) = {
            let mut guard = lock_unpoisoned(&self.inner);
            if is_stale_state_snapshot(&guard.last_state, &state) {
                (guard.last_state.clone(), false)
            } else {
                guard.last_state = state.clone();
                (state, true)
            }
        };
        if should_emit {
            emit_to_app(Some(&app), "autovisor-state", &final_state);
        }
        Ok(final_state)
    }

    pub async fn stop_task_async(self: &Arc<Self>, app: Option<&AppHandle>) -> RunState {
        let target = {
            let guard = lock_unpoisoned(&self.inner);
            if guard.phase == "ready" {
                guard.port.map(|p| (p, guard.token.clone()))
            } else {
                None
            }
        };

        if let Some((port, token)) = target {
            let url = format!("http://127.0.0.1:{port}/api/tasks/stop");
            if let Ok(resp) = self
                .async_api_client
                .post(&url)
                .header("X-Autovisor-Token", &token)
                .json(&serde_json::json!({}))
                .send()
                .await
            {
                if resp.status().is_success() {
                    if let Ok(state) = resp.json::<RunState>().await {
                        {
                            let mut guard = lock_unpoisoned(&self.inner);
                            guard.last_state = state.clone();
                        }
                        emit_to_app(app, "autovisor-state", &state);
                        return state;
                    }
                }
            }
        }

        // 兜底：若 HTTP 停止失败，则在阻塞池中强制终止当前后端进程树，下一次任务会自动拉起新实例
        let this = Arc::clone(self);
        let _ = tauri::async_runtime::spawn_blocking(move || {
            this.terminate_current_process(false);
        })
        .await;
        let snapshot = {
            let mut guard = lock_unpoisoned(&self.inner);
            if guard.last_state.status == "running" || guard.last_state.status == "stopping" {
                guard.last_state.status = "cancelled".to_string();
                guard.last_state.stage = "已停止".to_string();
                guard.last_state.finished_at = Some(now_secs());
                guard.last_state.needs_attention.clear();
            }
            guard.last_state.clone()
        };
        emit_to_app(app, "autovisor-state", &snapshot);
        snapshot
    }

    pub async fn cancel_shutdown_async(
        self: &Arc<Self>,
        app: Option<&AppHandle>,
    ) -> Result<RunState, AppError> {
        let (port, token) = self.ensure_ready_async(app).await?;
        let url = format!("http://127.0.0.1:{port}/api/tasks/cancel-shutdown");
        let resp = self
            .async_api_client
            .post(&url)
            .header("X-Autovisor-Token", &token)
            .json(&serde_json::json!({}))
            .send()
            .await
            .map_err(|e| AppError::backend(format!("连接内置学习引擎失败: {e}")))?;

        let status_code = resp.status();
        let body_text = resp
            .text()
            .await
            .map_err(|e| AppError::backend(format!("读取内置学习引擎响应失败: {e}")))?;

        if !status_code.is_success() {
            let msg = serde_json::from_str::<serde_json::Value>(&body_text)
                .ok()
                .and_then(|v| v.get("error").and_then(|e| e.as_str()).map(String::from))
                .unwrap_or_else(|| format!("取消关机失败（HTTP {status_code}）"));
            return Err(AppError::backend(msg));
        }

        let state: RunState = serde_json::from_str(&body_text)
            .map_err(|e| AppError::backend(format!("解析任务状态失败: {e}")))?;
        {
            let mut guard = lock_unpoisoned(&self.inner);
            guard.last_state = state.clone();
        }
        emit_to_app(app, "autovisor-state", &state);
        Ok(state)
    }

    pub fn shutdown(&self) {
        let (target, startup_stdin, startup_job) = {
            let mut guard = lock_unpoisoned(&self.inner);
            guard.shutting_down = true;
            guard.phase = "stopping".to_string();
            self.ready_cvar.notify_all();
            let target = guard.port.map(|p| (p, guard.token.clone()));
            // 若后端尚在启动握手期（port 未就绪），先立即释放 stdin 并终止启动中的进程树，
            // 避免下方获取 lifecycle_lock 被 spawn_and_connect_locked 阻塞长达 25s。
            let (stdin, job) = if target.is_none() {
                (guard.child_stdin.take(), guard.job.clone())
            } else {
                (None, None)
            };
            (target, stdin, job)
        };
        drop(startup_stdin);
        if let Some(job) = startup_job {
            job.terminate();
        }

        // 1. 尝试通过 HTTP 通知优雅关闭
        if let Some((port, token)) = target {
            let url = format!("http://127.0.0.1:{port}/api/shutdown");
            let _ = self
                .api_client
                .post(&url)
                .header("X-Autovisor-Token", &token)
                .timeout(Duration::from_secs(2))
                .json(&serde_json::json!({}))
                .send();
        }

        // 2. 关闭 stdin 管道（触发 stdin-watchdog）并等待优雅退出，超时由 Job 兜底强杀
        self.terminate_current_process(true);
    }

    fn terminate_current_process(&self, mark_stopped: bool) {
        let _lifecycle = lock_unpoisoned(&self.lifecycle_lock);
        self.terminate_current_process_locked(mark_stopped);
    }

    fn terminate_current_process_locked(&self, mark_stopped: bool) {
        let (child, stdin, job) = {
            let mut guard = lock_unpoisoned(&self.inner);
            guard.port = None;
            if mark_stopped || !guard.shutting_down {
                guard.phase = "stopped".to_string();
            }
            self.ready_cvar.notify_all();
            (guard.child.take(), guard.child_stdin.take(), guard.job.take())
        };
        reap_backend_process(child, stdin, job, Duration::from_secs(3));
    }

    fn terminate_for_generation_locked(&self, generation: u64, mark_stopped: bool) {
        let taken = {
            let mut guard = lock_unpoisoned(&self.inner);
            if guard.generation != generation {
                return;
            }
            guard.port = None;
            if mark_stopped || !guard.shutting_down {
                guard.phase = "stopped".to_string();
            }
            self.ready_cvar.notify_all();
            (guard.child.take(), guard.child_stdin.take(), guard.job.take())
        };
        reap_backend_process(taken.0, taken.1, taken.2, Duration::from_secs(3));
    }

    fn mark_failed_for_generation(&self, generation: Option<u64>, message: &str) -> bool {
        let mut guard = lock_unpoisoned(&self.inner);
        if guard.shutting_down {
            return false;
        }
        if let Some(gen_id) = generation {
            if guard.generation != gen_id {
                return false;
            }
        }
        guard.phase = "failed".to_string();
        guard.error = message.to_string();
        guard.port = None;
        self.ready_cvar.notify_all();
        true
    }

    fn set_failed_for_generation(
        &self,
        generation: Option<u64>,
        message: &str,
        app: Option<&AppHandle>,
    ) {
        if self.mark_failed_for_generation(generation, message) {
            self.emit_status(app);
        }
    }

    fn emit_status(&self, app: Option<&AppHandle>) {
        emit_to_app(app, "autovisor-backend-status", &self.status());
    }
}

fn reap_backend_process(
    mut child: Option<Child>,
    stdin: Option<ChildStdin>,
    job: Option<Arc<Job>>,
    grace_wait: Duration,
) {
    // 关闭 stdin 触发优雅退出
    drop(stdin);

    if let Some(ref mut c) = child {
        let deadline = Instant::now() + grace_wait;
        loop {
            match c.try_wait() {
                Ok(Some(_)) => break,
                Ok(None) if Instant::now() < deadline => {
                    thread::sleep(Duration::from_millis(50));
                }
                _ => break,
            }
        }
    }

    // 兜底：彻底销毁 Job Object / 进程组下的整棵进程树（含 Playwright 浏览器）
    if let Some(j) = job {
        j.terminate_with_child(child.as_mut());
    }
    if let Some(mut c) = child {
        let _ = c.kill();
        let _ = c.wait();
    }
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct LogDeltaPayload {
    #[serde(default)]
    run_id: u64,
    #[serde(default)]
    revision: u64,
    #[serde(default)]
    entries: Vec<LogEntry>,
    #[serde(default)]
    logs_truncated: Option<bool>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct RunStateDelta {
    #[serde(default)]
    run_id: u64,
    #[serde(default)]
    revision: u64,
    status: String,
    mode: String,
    #[serde(default)]
    review_mode: bool,
    #[serde(default)]
    shutdown_on_complete: bool,
    #[serde(default)]
    shutdown: Option<ShutdownInfo>,
    started_at: Option<f64>,
    finished_at: Option<f64>,
    exit_code: Option<i32>,
    stage: String,
    course_index: u32,
    course_total: u32,
    current_course: String,
    catalog_type: String,
    current_lesson: String,
    lesson_progress: f64,
    progress_desc: String,
    remaining_minutes: Option<f64>,
    course_elapsed_min: f64,
    completed_lessons: Vec<CompletedLesson>,
    needs_attention: String,
    error_message: String,
    log_file_path: String,
    #[serde(default)]
    logs: Option<Vec<LogEntry>>,
    #[serde(default)]
    logs_truncated: bool,
}

fn is_terminal_run_status(status: &str) -> bool {
    matches!(status, "completed" | "time_limit" | "error" | "cancelled")
}

fn is_stale_state_snapshot(current: &RunState, incoming: &RunState) -> bool {
    if incoming.run_id > 0 && current.run_id > incoming.run_id {
        return true;
    }
    let same_run = if incoming.run_id > 0 && current.run_id > 0 {
        current.run_id == incoming.run_id
    } else {
        incoming.started_at.is_some() && current.started_at == incoming.started_at
    };
    if !same_run {
        return false;
    }
    if incoming.revision > 0 && current.revision > incoming.revision {
        return true;
    }
    if is_terminal_run_status(&current.status)
        && matches!(incoming.status.as_str(), "running" | "stopping")
    {
        return true;
    }
    let cur_last_seq = current.logs.last().map(|e| e.seq).unwrap_or(0);
    let inc_last_seq = incoming.logs.last().map(|e| e.seq).unwrap_or(0);
    cur_last_seq > inc_last_seq
}

fn apply_sse_event(
    state: &mut RunState,
    event_name: &str,
    payload: &str,
) -> Option<serde_json::Value> {
    match event_name {
        "snapshot" => {
            let full = serde_json::from_str::<RunState>(payload).ok()?;
            if is_stale_state_snapshot(state, &full) {
                return None;
            }
            *state = full.clone();
            serde_json::to_value(&full).ok()
        }
        "log" => {
            let delta = serde_json::from_str::<LogDeltaPayload>(payload).ok()?;
            if delta.run_id > 0 && state.run_id > 0 && delta.run_id < state.run_id {
                return None;
            }
            let run_advanced = delta.run_id > 0 && state.run_id > 0 && delta.run_id > state.run_id;
            if run_advanced {
                state.run_id = delta.run_id;
                state.revision = delta.revision;
                state.logs.clear();
                state.logs_truncated = false;
            } else {
                if delta.run_id > state.run_id {
                    state.run_id = delta.run_id;
                }
                if delta.revision > state.revision {
                    state.revision = delta.revision;
                }
            }
            let prev_truncated = state.logs_truncated;
            let mut last_seq = state.logs.last().map(|e| e.seq).unwrap_or(0);
            let mut appended = Vec::with_capacity(delta.entries.len());
            for entry in delta.entries {
                if entry.seq > last_seq {
                    last_seq = entry.seq;
                    state.logs.push(entry.clone());
                    appended.push(entry);
                }
            }
            if state.logs.len() > LOG_LIMIT {
                let overflow = state.logs.len() - LOG_LIMIT;
                state.logs.drain(0..overflow);
                state.logs_truncated = true;
            }
            if let Some(trunc) = delta.logs_truncated {
                state.logs_truncated = state.logs_truncated || trunc;
            }
            let became_truncated = state.logs_truncated && !prev_truncated;
            if !run_advanced && appended.is_empty() && !became_truncated {
                return None;
            }
            Some(serde_json::json!({
                "__delta": "log",
                "runId": state.run_id,
                "revision": state.revision,
                "entries": appended,
                "logsTruncated": state.logs_truncated,
            }))
        }
        _ => {
            let delta = serde_json::from_str::<RunStateDelta>(payload).ok()?;
            if delta.run_id > 0 && state.run_id > 0 {
                if delta.run_id < state.run_id {
                    return None;
                }
                if delta.run_id == state.run_id
                    && delta.revision > 0
                    && state.revision > delta.revision
                {
                    return None;
                }
            }
            let run_advanced = delta.run_id > 0 && state.run_id > 0 && delta.run_id > state.run_id;
            if run_advanced {
                state.run_id = delta.run_id;
                state.revision = delta.revision;
                if delta.logs.is_none() {
                    state.logs.clear();
                    state.logs_truncated = false;
                }
            } else {
                if delta.run_id > 0 {
                    state.run_id = delta.run_id;
                }
                if delta.revision > state.revision {
                    state.revision = delta.revision;
                }
            }
            state.status = delta.status;
            state.mode = delta.mode;
            state.review_mode = delta.review_mode;
            state.shutdown_on_complete = delta.shutdown_on_complete;
            state.shutdown = delta.shutdown;
            state.started_at = delta.started_at;
            state.finished_at = delta.finished_at;
            state.exit_code = delta.exit_code;
            state.stage = delta.stage;
            state.course_index = delta.course_index;
            state.course_total = delta.course_total;
            state.current_course = delta.current_course;
            state.catalog_type = delta.catalog_type;
            state.current_lesson = delta.current_lesson;
            state.lesson_progress = delta.lesson_progress;
            state.progress_desc = delta.progress_desc;
            state.remaining_minutes = delta.remaining_minutes;
            state.course_elapsed_min = delta.course_elapsed_min;
            state.completed_lessons = delta.completed_lessons;
            state.needs_attention = delta.needs_attention;
            state.error_message = delta.error_message;
            state.log_file_path = delta.log_file_path;
            if let Some(full_logs) = delta.logs {
                state.logs = full_logs;
                state.logs_truncated = delta.logs_truncated;
                serde_json::to_value(&*state).ok()
            } else {
                state.logs_truncated = state.logs_truncated || delta.logs_truncated;
                let mut val = serde_json::from_str::<serde_json::Value>(payload).ok()?;
                if let Some(obj) = val.as_object_mut() {
                    obj.insert("__delta".to_string(), serde_json::json!("state"));
                }
                Some(val)
            }
        }
    }
}

fn run_sse_loop(
    inner: Arc<Mutex<BackendInner>>,
    ready_cvar: Arc<Condvar>,
    client: reqwest::blocking::Client,
    generation: u64,
    port: u16,
    token: String,
    app: Option<AppHandle>,
) {
    let url = format!("http://127.0.0.1:{port}/api/events");
    let resp = match client
        .get(&url)
        .header("X-Autovisor-Token", &token)
        .send()
    {
        Ok(r) if r.status().is_success() => r,
        _ => {
            handle_sse_disconnect(&inner, &ready_cvar, generation, app.as_ref());
            return;
        }
    };

    let mut reader = BufReader::new(resp);
    let mut line = String::new();
    let mut current_event = String::new();
    loop {
        line.clear();
        match reader.read_line(&mut line) {
            Ok(0) => break,
            Ok(_) => {
                let trimmed = line.trim();
                if trimmed.is_empty() {
                    current_event.clear();
                    continue;
                }
                if trimmed.starts_with(':') {
                    continue;
                }
                if let Some(ev_name) = trimmed.strip_prefix("event:") {
                    current_event = ev_name.trim().to_string();
                    continue;
                }
                if let Some(json_str) = trimmed.strip_prefix("data:") {
                    let payload = json_str.trim();
                    let event_kind = if current_event.is_empty() {
                        "state"
                    } else {
                        current_event.as_str()
                    };
                    let outgoing = {
                        let mut guard = lock_unpoisoned(&inner);
                        if guard.generation != generation {
                            return;
                        }
                        apply_sse_event(&mut guard.last_state, event_kind, payload)
                    };
                    if let Some(val) = outgoing {
                        emit_to_app(app.as_ref(), "autovisor-state", &val);
                    }
                }
            }
            Err(_) => break,
        }
    }

    handle_sse_disconnect(&inner, &ready_cvar, generation, app.as_ref());
}

fn handle_sse_disconnect(
    inner: &Arc<Mutex<BackendInner>>,
    ready_cvar: &Arc<Condvar>,
    generation: u64,
    app: Option<&AppHandle>,
) {
    let (status_to_emit, child, stdin, job) = {
        let mut guard = lock_unpoisoned(inner);
        if guard.generation != generation || guard.shutting_down || guard.phase != "ready" {
            return;
        }
        guard.phase = "failed".to_string();
        guard.port = None;
        guard.error = "内置学习引擎连接中断，下次启动任务时将自动重新拉起".to_string();
        if guard.last_state.status == "running" || guard.last_state.status == "stopping" {
            guard.last_state.status = "error".to_string();
            guard.last_state.stage = "连接中断".to_string();
            guard.last_state.finished_at = Some(now_secs());
            guard.last_state.error_message = guard.error.clone();
        }
        let child = guard.child.take();
        let stdin = guard.child_stdin.take();
        let job = guard.job.take();
        (
            (
                BackendStatusSnapshot {
                    phase: guard.phase.clone(),
                    version: guard.version.clone(),
                    backend_version: guard.backend_version.clone(),
                    error: guard.error.clone(),
                },
                guard.last_state.clone(),
            ),
            child,
            stdin,
            job,
        )
    };
    // 先安全终止并回收旧子进程，再唤醒等待中的重启/就绪方，避免新旧进程并发占用 stderr 日志
    reap_backend_process(child, stdin, job, Duration::from_secs(1));
    ready_cvar.notify_all();
    emit_to_app(app, "autovisor-backend-status", &status_to_emit.0);
    emit_to_app(app, "autovisor-state", &status_to_emit.1);
}

fn rotate_stderr_log(log_path: &PathBuf) -> bool {
    let mut rotated = log_path.clone().into_os_string();
    rotated.push(".1");
    let rotated_path = PathBuf::from(rotated);
    let _ = std::fs::remove_file(&rotated_path);
    std::fs::rename(log_path, &rotated_path).is_ok()
}

fn open_stderr_append(log_path: &PathBuf) -> Option<(std::fs::File, u64)> {
    let file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(log_path)
        .ok()?;
    let size = file.metadata().map(|m| m.len()).unwrap_or(0);
    Some((file, size))
}

fn drain_stderr_with_limit<R: Read>(mut reader: R, log_path: PathBuf, max_bytes: u64) {
    if let Some(parent) = log_path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let (mut file, mut written) = match open_stderr_append(&log_path) {
        Some((f, len)) => (Some(f), len),
        None => (None, 0),
    };
    let mut next_rotate_at = max_bytes;
    let mut buf = [0u8; 4096];
    loop {
        match reader.read(&mut buf) {
            Ok(0) | Err(_) => break,
            Ok(n) => {
                if max_bytes > 0 && written + n as u64 > next_rotate_at {
                    // 释放当前句柄后轮转到 .1 并重新打开
                    drop(file.take());
                    let rotated = rotate_stderr_log(&log_path);
                    match open_stderr_append(&log_path) {
                        Some((f, len)) => {
                            file = Some(f);
                            written = len;
                            // 若 Windows 下因文件被其他进程短暂占用导致 rename 失败，退避下一次轮转阈值避免每 chunk 死循环重开
                            next_rotate_at = if rotated {
                                max_bytes
                            } else {
                                len.saturating_add((max_bytes / 4).max(4096))
                            };
                        }
                        None => {
                            file = None;
                            written = 0;
                            next_rotate_at = max_bytes;
                        }
                    }
                }
                if let Some(ref mut f) = file {
                    if f.write_all(&buf[..n]).is_ok() {
                        let _ = f.flush();
                        written += n as u64;
                    }
                }
            }
        }
    }
}

fn generate_token() -> String {
    let mut bytes = [0u8; 32];
    if getrandom::fill(&mut bytes).is_ok() {
        let mut out = String::with_capacity(64);
        for b in bytes {
            out.push_str(&format!("{b:02x}"));
        }
        return out;
    }
    // 极端情况下 OS CSPRNG 不可用时的保底回退
    let mut out = String::with_capacity(64);
    for salt in 0u64..4 {
        let mut h = RandomState::new().build_hasher();
        h.write_u64(salt);
        h.write_u32(std::process::id());
        if let Ok(d) = SystemTime::now().duration_since(UNIX_EPOCH) {
            h.write_u128(d.as_nanos());
        }
        out.push_str(&format!("{:016x}", h.finish()));
    }
    out
}

fn now_secs() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

#[cfg(test)]
mod tests {
    use super::{
        apply_sse_event, drain_stderr_with_limit, generate_token, is_stale_state_snapshot,
        lock_unpoisoned, BackendManager, LogEntry, RunState,
    };
    use std::io::Cursor;
    use std::sync::{Arc, Mutex};
    use std::thread;
    use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

    #[test]
    fn generate_token_is_64_hex_chars_and_unique() {
        let t1 = generate_token();
        let t2 = generate_token();
        assert_eq!(t1.len(), 64);
        assert_eq!(t2.len(), 64);
        assert!(t1.chars().all(|c| c.is_ascii_hexdigit()));
        assert_ne!(t1, t2);
    }

    #[test]
    fn lock_unpoisoned_recovers_after_panic() {
        let m = Arc::new(Mutex::new(42u32));
        let m_clone = Arc::clone(&m);
        let _ = thread::spawn(move || {
            let mut g = m_clone.lock().unwrap();
            *g = 99;
            panic!("intentional poison");
        })
        .join();
        assert!(m.is_poisoned());
        let g = lock_unpoisoned(&m);
        assert_eq!(*g, 99);
    }

    #[test]
    fn ensure_ready_wakes_immediately_via_condvar() {
        let mgr = Arc::new(BackendManager::new());
        let mgr_notify = Arc::clone(&mgr);
        let start = Instant::now();
        let t = thread::spawn(move || {
            thread::sleep(Duration::from_millis(20));
            let mut guard = lock_unpoisoned(&mgr_notify.inner);
            guard.phase = "ready".to_string();
            guard.port = Some(18888);
            guard.token = "tok".to_string();
            mgr_notify.ready_cvar.notify_all();
        });
        let (port, tok) = mgr
            .wait_for_ready_or_needs_spawn(Duration::from_secs(5))
            .expect("should become ready")
            .expect("should be Ready");
        t.join().unwrap();
        assert_eq!(port, 18888);
        assert_eq!(tok, "tok");
        assert!(start.elapsed() < Duration::from_millis(500));
    }

    #[test]
    fn stale_generation_failure_does_not_clobber_newer_generation() {
        let mgr = BackendManager::new();
        {
            let mut guard = lock_unpoisoned(&mgr.inner);
            guard.generation = 2;
            guard.phase = "ready".to_string();
            guard.port = Some(19000);
        }
        assert!(!mgr.mark_failed_for_generation(Some(1), "old failure"));
        let st = mgr.status();
        assert_eq!(st.phase, "ready");
        assert!(st.error.is_empty());
    }

    #[test]
    fn drain_stderr_appends_and_rotates_without_truncating_on_restart() {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let dir = std::env::temp_dir().join(format!("autovisor_stderr_test_{nanos}"));
        std::fs::create_dir_all(&dir).unwrap();
        let log_path = dir.join("backend-stderr.log");
        let rotated_path = dir.join("backend-stderr.log.1");

        // Run 1 + Run 2 append instead of truncating
        drain_stderr_with_limit(Cursor::new(b"first-run\n"), log_path.clone(), 64);
        drain_stderr_with_limit(Cursor::new(b"second-run\n"), log_path.clone(), 64);
        let combined = std::fs::read_to_string(&log_path).unwrap();
        assert!(combined.contains("first-run"));
        assert!(combined.contains("second-run"));

        // Exceed limit -> rotates previous content to .1
        let big = vec![b'x'; 50];
        drain_stderr_with_limit(Cursor::new(big), log_path.clone(), 64);
        assert!(rotated_path.is_file());
        let old = std::fs::read_to_string(&rotated_path).unwrap();
        assert!(old.contains("first-run"));
        let current = std::fs::read_to_string(&log_path).unwrap();
        assert_eq!(current.len(), 50);

        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn apply_sse_event_handles_snapshot_state_delta_log_delta_and_legacy_full_state() {
        let mut state = RunState::default();

        // 1. Initial snapshot replaces full state + logs
        let snap_json = serde_json::json!({
            "status": "running",
            "mode": "study",
            "reviewMode": false,
            "shutdownOnComplete": false,
            "shutdown": null,
            "startedAt": 100.0,
            "finishedAt": null,
            "exitCode": null,
            "stage": "初始化",
            "courseIndex": 1,
            "courseTotal": 2,
            "currentCourse": "高数",
            "catalogType": "",
            "currentLesson": "",
            "lessonProgress": 0.0,
            "progressDesc": "",
            "remainingMinutes": null,
            "courseElapsedMin": 0.0,
            "completedLessons": [],
            "needsAttention": "",
            "errorMessage": "",
            "logFilePath": "",
            "logs": [{ "seq": 1, "timestamp": 100.0, "level": "info", "message": "boot" }],
            "logsTruncated": false
        })
        .to_string();
        let out = apply_sse_event(&mut state, "snapshot", &snap_json).unwrap();
        assert!(out.get("__delta").is_none());
        assert_eq!(state.status, "running");
        assert_eq!(state.logs.len(), 1);

        // 2. Delta state without logs preserves existing logs and emits lightweight __delta="state"
        let state_delta_json = serde_json::json!({
            "status": "running",
            "mode": "study",
            "reviewMode": false,
            "shutdownOnComplete": false,
            "shutdown": null,
            "startedAt": 100.0,
            "finishedAt": null,
            "exitCode": null,
            "stage": "播放视频",
            "courseIndex": 1,
            "courseTotal": 2,
            "currentCourse": "高数",
            "catalogType": "共享课",
            "currentLesson": "1.1 极限",
            "lessonProgress": 55.0,
            "progressDesc": "当前进度",
            "remainingMinutes": 8.0,
            "courseElapsedMin": 2.0,
            "completedLessons": [],
            "needsAttention": "",
            "errorMessage": "",
            "logFilePath": "",
            "logsTruncated": false
        })
        .to_string();
        let delta_out = apply_sse_event(&mut state, "state", &state_delta_json).unwrap();
        assert_eq!(delta_out.get("__delta").and_then(|v| v.as_str()), Some("state"));
        assert!(delta_out.get("logs").is_none());
        assert_eq!(state.lesson_progress, 55.0);
        assert_eq!(state.logs.len(), 1);

        // 3. Log delta appends deduplicated entries and emits __delta="log"
        let log_delta_json = serde_json::json!({
            "entries": [
                { "seq": 1, "timestamp": 100.0, "level": "info", "message": "dup" },
                { "seq": 2, "timestamp": 101.0, "level": "info", "message": "playing" }
            ],
            "logsTruncated": false
        })
        .to_string();
        let log_out = apply_sse_event(&mut state, "log", &log_delta_json).unwrap();
        assert_eq!(log_out.get("__delta").and_then(|v| v.as_str()), Some("log"));
        assert_eq!(log_out["entries"].as_array().unwrap().len(), 1);
        assert_eq!(state.logs.len(), 2);
        assert_eq!(state.logs[1].message, "playing");

        // 4. Legacy full `event: state` with `logs` array still works seamlessly
        let legacy_full = serde_json::json!({
            "status": "completed",
            "mode": "study",
            "reviewMode": false,
            "shutdownOnComplete": false,
            "shutdown": null,
            "startedAt": 100.0,
            "finishedAt": 200.0,
            "exitCode": 0,
            "stage": "已结束",
            "courseIndex": 2,
            "courseTotal": 2,
            "currentCourse": "高数",
            "catalogType": "共享课",
            "currentLesson": "1.1 极限",
            "lessonProgress": 100.0,
            "progressDesc": "",
            "remainingMinutes": 0.0,
            "courseElapsedMin": 10.0,
            "completedLessons": [],
            "needsAttention": "",
            "errorMessage": "",
            "logFilePath": "",
            "logs": [{ "seq": 9, "timestamp": 200.0, "level": "info", "message": "done" }],
            "logsTruncated": false
        })
        .to_string();
        let legacy_out = apply_sse_event(&mut state, "state", &legacy_full).unwrap();
        assert!(legacy_out.get("__delta").is_none());
        assert_eq!(state.status, "completed");
        assert_eq!(state.logs.len(), 1);
        assert_eq!(state.logs[0].seq, 9);
    }

    #[test]
    fn stale_start_snapshot_does_not_overwrite_newer_sse_state_or_logs() {
        let mut current = RunState {
            run_id: 3,
            revision: 5,
            status: "running".to_string(),
            started_at: Some(100.0),
            logs: vec![
                LogEntry { seq: 1, timestamp: 100.0, level: "info".to_string(), message: "boot".to_string() },
                LogEntry { seq: 2, timestamp: 101.0, level: "info".to_string(), message: "course".to_string() },
            ],
            ..Default::default()
        };

        // Stale HTTP start_task response (same run_id, lower revision & seq=1) must be ignored
        let stale = RunState {
            run_id: 3,
            revision: 1,
            status: "running".to_string(),
            started_at: Some(100.0),
            logs: vec![
                LogEntry { seq: 1, timestamp: 100.0, level: "info".to_string(), message: "boot".to_string() },
            ],
            ..Default::default()
        };
        assert!(is_stale_state_snapshot(&current, &stale));

        // Duplicate log delta without new truncation must not emit an empty frame
        let dup_log = serde_json::json!({
            "runId": 3,
            "revision": 5,
            "entries": [{ "seq": 2, "timestamp": 101.0, "level": "info", "message": "dup" }],
            "logsTruncated": false
        })
        .to_string();
        assert!(apply_sse_event(&mut current, "log", &dup_log).is_none());

        // New run_id=4 snapshot must NOT be treated as stale
        let mut next_run = stale.clone();
        next_run.run_id = 4;
        next_run.revision = 6;
        assert!(!is_stale_state_snapshot(&current, &next_run));

        // If a newer run's log delta arrives before its snapshot, old logs and revision must reset
        let next_run_log = serde_json::json!({
            "runId": 4,
            "revision": 1,
            "entries": [{ "seq": 1, "timestamp": 200.0, "level": "info", "message": "new run boot" }],
            "logsTruncated": false
        })
        .to_string();
        let rolled = apply_sse_event(&mut current, "log", &next_run_log)
            .expect("new run log delta should reset logs and emit");
        assert_eq!(current.run_id, 4);
        assert_eq!(current.revision, 1);
        assert_eq!(current.logs.len(), 1);
        assert_eq!(current.logs[0].seq, 1);
        assert_eq!(current.logs[0].message, "new run boot");
        assert_eq!(rolled["entries"].as_array().unwrap().len(), 1);
    }

    #[test]
    fn shutdown_sets_flag_and_wakes_immediately_when_not_started() {
        let mgr = BackendManager::new();
        let start = Instant::now();
        mgr.shutdown();
        assert!(start.elapsed() < Duration::from_secs(1));
        assert!(lock_unpoisoned(&mgr.inner).shutting_down);
    }
}
