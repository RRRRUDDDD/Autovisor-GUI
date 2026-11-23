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
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tauri::{AppHandle, Emitter};

const HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(25);
const READY_WAIT_TIMEOUT: Duration = Duration::from_secs(20);

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
    pub status: String, // idle | running | completed | time_limit | error | cancelled
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
    api_client: reqwest::blocking::Client,
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
                backend_version: "3.19.0(Autovisor GUI Edition)".to_string(),
                error: String::new(),
                port: None,
                token: String::new(),
                child: None,
                child_stdin: None,
                job: None,
                last_state: RunState::default(),
                shutting_down: false,
            })),
            api_client,
            sse_client,
        }
    }

    pub fn status(&self) -> BackendStatusSnapshot {
        let guard = self.inner.lock().unwrap();
        BackendStatusSnapshot {
            phase: guard.phase.clone(),
            version: guard.version.clone(),
            backend_version: guard.backend_version.clone(),
            error: guard.error.clone(),
        }
    }

    pub fn snapshot(&self) -> RunState {
        let guard = self.inner.lock().unwrap();
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
        self.terminate_current_process(false);
        self.spawn_and_connect(Some(app))?;
        Ok(self.status())
    }

    fn spawn_and_connect(&self, app: Option<AppHandle>) -> Result<(u16, String), String> {
        let paths = resolve_app_paths();
        let launch = match paths.backend_launch {
            Some(spec) => spec,
            None => {
                let msg = "未找到内置学习引擎组件（runtime/autovisor-backend.exe），请重新解压完整程序包。".to_string();
                self.set_failed(&msg, app.as_ref());
                return Err(msg);
            }
        };

        let token = generate_token();
        let job = Arc::new(Job::create()?);

        let generation = {
            let mut guard = self.inner.lock().unwrap();
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
            .env("AUTOVISOR_BACKEND_TOKEN", &token)
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
                self.set_failed(&msg, app.as_ref());
                return Err(msg);
            }
        };

        if let Err(e) = job.assign(child.id()) {
            let _ = child.kill();
            let msg = format!("绑定内置学习引擎进程树失败: {e}");
            self.set_failed(&msg, app.as_ref());
            return Err(msg);
        }

        let stdin = child.stdin.take();
        let stdout = child.stdout.take();
        let stderr = child.stderr.take();

        // 后台持续排空 stderr 并落盘到 logs/backend-stderr.log，避免管道满阻塞
        if let Some(err_stream) = stderr {
            let log_path = paths.data_dir.join("logs").join("backend-stderr.log");
            thread::spawn(move || drain_stderr(err_stream, log_path));
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
            job.terminate();
            self.set_failed(&msg, app.as_ref());
            return Err(msg);
        }

        {
            let mut guard = self.inner.lock().unwrap();
            guard.child = Some(child);
            guard.child_stdin = stdin;
            guard.job = Some(Arc::clone(&job));
        }

        let line = match rx.recv_timeout(HANDSHAKE_TIMEOUT) {
            Ok(Ok(l)) => l,
            Ok(Err(e)) => {
                self.terminate_current_process(false);
                self.set_failed(&e, app.as_ref());
                return Err(e);
            }
            Err(_) => {
                let msg = "内置学习引擎启动超时，请检查杀毒软件是否拦截。".to_string();
                self.terminate_current_process(false);
                self.set_failed(&msg, app.as_ref());
                return Err(msg);
            }
        };

        let handshake: HandshakePayload =
            match serde_json::from_str::<HandshakePayload>(line.trim()) {
                Ok(h) if h.event == "autovisor-backend-ready" && h.version == 1 && h.port > 0 => h,
                Ok(_) => {
                let msg = "内置学习引擎握手协议不匹配".to_string();
                self.terminate_current_process(false);
                self.set_failed(&msg, app.as_ref());
                return Err(msg);
            }
            Err(e) => {
                let msg = format!("解析内置学习引擎握手失败: {e}");
                self.terminate_current_process(false);
                self.set_failed(&msg, app.as_ref());
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
            self.terminate_current_process(false);
            self.set_failed(&msg, app.as_ref());
            return Err(msg);
        }

        {
            let mut guard = self.inner.lock().unwrap();
            if guard.generation != generation || guard.shutting_down {
                return Err("内置学习引擎状态已变更".to_string());
            }
            guard.phase = "ready".to_string();
            guard.error.clear();
            guard.port = Some(handshake.port);
            if !handshake.app_version.is_empty() {
                guard.backend_version = handshake.app_version;
            }
        }
        self.emit_status(app.as_ref());

        // 建立 SSE 实时状态长连接
        let inner_ref = Arc::clone(&self.inner);
        let sse_client = self.sse_client.clone();
        let port = handshake.port;
        let sse_token = token.clone();
        let sse_app = app.clone();
        thread::spawn(move || {
            run_sse_loop(inner_ref, sse_client, generation, port, sse_token, sse_app);
        });

        Ok((handshake.port, token))
    }

    fn ensure_ready(&self, app: Option<&AppHandle>) -> Result<(u16, String), String> {
        let deadline = Instant::now() + READY_WAIT_TIMEOUT;
        loop {
            let (phase, port, token, error) = {
                let guard = self.inner.lock().unwrap();
                (
                    guard.phase.clone(),
                    guard.port,
                    guard.token.clone(),
                    guard.error.clone(),
                )
            };

            if phase == "ready" {
                if let Some(p) = port {
                    return Ok((p, token));
                }
            }

            if phase == "failed" || phase == "stopped" {
                // 自动自愈重启一次
                return self.spawn_and_connect(app.cloned());
            }

            if Instant::now() >= deadline {
                let msg = if error.is_empty() {
                    "等待内置学习引擎就绪超时，请稍后重试".to_string()
                } else {
                    error
                };
                return Err(msg);
            }

            thread::sleep(Duration::from_millis(120));
        }
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

    pub fn start_task(
        &self,
        app: AppHandle,
        mode: &str,
        target: &str,
    ) -> Result<RunState, String> {
        let (port, token) = self.ensure_ready(Some(&app))?;
        let url = format!("http://127.0.0.1:{port}/api/tasks/start");
        let resp = self
            .api_client
            .post(&url)
            .header("X-Autovisor-Token", &token)
            .json(&serde_json::json!({
                "mode": mode,
                "target": target,
            }))
            .send()
            .map_err(|e| format!("连接内置学习引擎失败: {e}"))?;

        let status_code = resp.status();
        let body_text = resp
            .text()
            .map_err(|e| format!("读取内置学习引擎响应失败: {e}"))?;

        if !status_code.is_success() {
            let msg = serde_json::from_str::<serde_json::Value>(&body_text)
                .ok()
                .and_then(|v| v.get("error").and_then(|e| e.as_str()).map(String::from))
                .unwrap_or_else(|| format!("启动任务失败（HTTP {status_code}）"));
            return Err(msg);
        }

        let state: RunState = serde_json::from_str(&body_text)
            .map_err(|e| format!("解析任务状态失败: {e}"))?;
        {
            let mut guard = self.inner.lock().unwrap();
            guard.last_state = state.clone();
        }
        let _ = app.emit("autovisor-state", &state);
        Ok(state)
    }

    pub fn stop_task(&self, app: Option<&AppHandle>) -> RunState {
        let target = {
            let guard = self.inner.lock().unwrap();
            if guard.phase == "ready" {
                guard.port.map(|p| (p, guard.token.clone()))
            } else {
                None
            }
        };

        if let Some((port, token)) = target {
            let url = format!("http://127.0.0.1:{port}/api/tasks/stop");
            if let Ok(resp) = self
                .api_client
                .post(&url)
                .header("X-Autovisor-Token", &token)
                .json(&serde_json::json!({}))
                .send()
            {
                if resp.status().is_success() {
                    if let Ok(state) = resp.json::<RunState>() {
                        {
                            let mut guard = self.inner.lock().unwrap();
                            guard.last_state = state.clone();
                        }
                        if let Some(handle) = app {
                            let _ = handle.emit("autovisor-state", &state);
                        }
                        return state;
                    }
                }
            }
        }

        // 兜底：若 HTTP 停止失败，则强制终止当前后端进程树，下一次任务会自动拉起新实例
        self.terminate_current_process(false);
        let snapshot = {
            let mut guard = self.inner.lock().unwrap();
            if guard.last_state.status == "running" {
                guard.last_state.status = "cancelled".to_string();
                guard.last_state.stage = "已停止".to_string();
                guard.last_state.finished_at = Some(now_secs());
                guard.last_state.needs_attention.clear();
            }
            guard.last_state.clone()
        };
        if let Some(handle) = app {
            let _ = handle.emit("autovisor-state", &snapshot);
        }
        snapshot
    }

    pub fn cancel_shutdown(&self, app: Option<&AppHandle>) -> Result<RunState, String> {
        let (port, token) = self.ensure_ready(app)?;
        let url = format!("http://127.0.0.1:{port}/api/tasks/cancel-shutdown");
        let resp = self
            .api_client
            .post(&url)
            .header("X-Autovisor-Token", &token)
            .json(&serde_json::json!({}))
            .send()
            .map_err(|e| format!("连接内置学习引擎失败: {e}"))?;

        let status_code = resp.status();
        let body_text = resp
            .text()
            .map_err(|e| format!("读取内置学习引擎响应失败: {e}"))?;

        if !status_code.is_success() {
            let msg = serde_json::from_str::<serde_json::Value>(&body_text)
                .ok()
                .and_then(|v| v.get("error").and_then(|e| e.as_str()).map(String::from))
                .unwrap_or_else(|| format!("取消关机失败（HTTP {status_code}）"));
            return Err(msg);
        }

        let state: RunState = serde_json::from_str(&body_text)
            .map_err(|e| format!("解析任务状态失败: {e}"))?;
        {
            let mut guard = self.inner.lock().unwrap();
            guard.last_state = state.clone();
        }
        if let Some(handle) = app {
            let _ = handle.emit("autovisor-state", &state);
        }
        Ok(state)
    }

    pub fn shutdown(&self) {
        let target = {
            let mut guard = self.inner.lock().unwrap();
            guard.shutting_down = true;
            guard.phase = "stopping".to_string();
            guard.port.map(|p| (p, guard.token.clone()))
        };

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
        let (mut child, stdin, job) = {
            let mut guard = self.inner.lock().unwrap();
            guard.port = None;
            if mark_stopped {
                guard.phase = "stopped".to_string();
            } else if !guard.shutting_down {
                guard.phase = "stopped".to_string();
            }
            (guard.child.take(), guard.child_stdin.take(), guard.job.take())
        };

        // 关闭 stdin 触发优雅退出
        drop(stdin);

        if let Some(ref mut c) = child {
            let deadline = Instant::now() + Duration::from_secs(3);
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

        // 兜底：彻底销毁 Job Object 下的整棵进程树（含 Playwright 浏览器）
        if let Some(j) = job {
            j.terminate();
        }
        if let Some(mut c) = child {
            let _ = c.kill();
            let _ = c.wait();
        }
    }

    fn set_failed(&self, message: &str, app: Option<&AppHandle>) {
        {
            let mut guard = self.inner.lock().unwrap();
            if guard.shutting_down {
                return;
            }
            guard.phase = "failed".to_string();
            guard.error = message.to_string();
            guard.port = None;
        }
        self.emit_status(app);
    }

    fn emit_status(&self, app: Option<&AppHandle>) {
        if let Some(handle) = app {
            let _ = handle.emit("autovisor-backend-status", self.status());
        }
    }
}

fn run_sse_loop(
    inner: Arc<Mutex<BackendInner>>,
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
            handle_sse_disconnect(&inner, generation, app.as_ref());
            return;
        }
    };

    let mut reader = BufReader::new(resp);
    let mut line = String::new();
    loop {
        line.clear();
        match reader.read_line(&mut line) {
            Ok(0) => break,
            Ok(_) => {
                let trimmed = line.trim();
                if let Some(json_str) = trimmed.strip_prefix("data:") {
                    let payload = json_str.trim();
                    if let Ok(state) = serde_json::from_str::<RunState>(payload) {
                        {
                            let mut guard = inner.lock().unwrap();
                            if guard.generation != generation {
                                return;
                            }
                            guard.last_state = state.clone();
                        }
                        if let Some(ref handle) = app {
                            let _ = handle.emit("autovisor-state", &state);
                        }
                    }
                }
            }
            Err(_) => break,
        }
    }

    handle_sse_disconnect(&inner, generation, app.as_ref());
}

fn handle_sse_disconnect(inner: &Arc<Mutex<BackendInner>>, generation: u64, app: Option<&AppHandle>) {
    let status_to_emit = {
        let mut guard = inner.lock().unwrap();
        if guard.generation != generation || guard.shutting_down || guard.phase != "ready" {
            return;
        }
        guard.phase = "failed".to_string();
        guard.port = None;
        guard.error = "内置学习引擎连接中断，下次启动任务时将自动重新拉起".to_string();
        if guard.last_state.status == "running" {
            guard.last_state.status = "error".to_string();
            guard.last_state.stage = "连接中断".to_string();
            guard.last_state.finished_at = Some(now_secs());
            guard.last_state.error_message = guard.error.clone();
        }
        (
            BackendStatusSnapshot {
                phase: guard.phase.clone(),
                version: guard.version.clone(),
                backend_version: guard.backend_version.clone(),
                error: guard.error.clone(),
            },
            guard.last_state.clone(),
        )
    };
    if let Some(handle) = app {
        let _ = handle.emit("autovisor-backend-status", &status_to_emit.0);
        let _ = handle.emit("autovisor-state", &status_to_emit.1);
    }
}

fn drain_stderr<R: Read>(mut reader: R, log_path: PathBuf) {
    if let Some(parent) = log_path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let mut file = OpenOptions::new()
        .create(true)
        .write(true)
        .truncate(true)
        .open(&log_path)
        .ok();
    let mut written: u64 = 0;
    const MAX_STDERR_BYTES: u64 = 2 * 1024 * 1024;
    let mut buf = [0u8; 4096];
    loop {
        match reader.read(&mut buf) {
            Ok(0) | Err(_) => break,
            Ok(n) => {
                if let Some(ref mut f) = file {
                    if written < MAX_STDERR_BYTES {
                        let _ = f.write_all(&buf[..n]);
                        let _ = f.flush();
                        written += n as u64;
                    }
                }
            }
        }
    }
}

fn generate_token() -> String {
    let mut out = String::with_capacity(32);
    for salt in 0u64..2 {
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
