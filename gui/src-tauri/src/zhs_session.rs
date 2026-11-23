use crate::backend::BackendManager;
use crate::browser_detect::{detect_installed_browsers, resolve_browser_choice};
use crate::config_ini::{load_config_from_dir, merge_discovered_courses, save_config_to_dir, CourseItem};
use crate::runtime::{load_prefs, save_prefs, SessionProfile};
use crate::process_tree::{configure_hidden_command, prepare_process_tree_command, Job};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::fs;
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tauri::{AppHandle, Emitter};
use tungstenite::stream::MaybeTlsStream;
use tungstenite::{Message, WebSocket};

const LOGIN_ENTRY_URL: &str =
    "https://passport.zhihuishu.com/login?service=https%3A%2F%2Fonlineweb.zhihuishu.com%2Fonlinestuh5";
const NEW_LOGIN_ENTRY_URL: &str =
    "https://login.zhihuishu.com/?origin=zhs&service=https%3A%2F%2Fonlineweb.zhihuishu.com%2Fonlinestuh5";
const STUDENT_HOME_URL: &str = "https://onlineweb.zhihuishu.com/onlinestuh5";
const ZHS_TAB_SWITCH_JS: &str = include_str!("../../../resources/scripts/zhs_tab_switch.js");
const ZHS_DOM_EXTRACT_JS: &str = include_str!("../../../resources/scripts/zhs_dom_extract.js");

const SCHOOL_LIST_URL: &str = "https://passport.zhihuishu.com/user/getAllSchool";

static SCHOOL_CACHE: OnceLock<Mutex<Option<Vec<SchoolItem>>>> = OnceLock::new();

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SchoolItem {
    pub school_id: String,
    pub name: String,
    pub letter: String,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct LoginRequest {
    pub mode: String, // "phone" | "student" | "qrcode" | "cookie"
    #[serde(default)]
    pub username: String,
    #[serde(default)]
    pub school_id: String,
    #[serde(default)]
    pub school_name: String,
    #[serde(default)]
    pub student_id: String,
    #[serde(default)]
    pub password: String,
    #[serde(default)]
    pub browser_exe: String,
    #[serde(default)]
    pub proxy: String,
    #[serde(default)]
    pub remember_credentials: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct LoginStatusEvent {
    pub phase: String, // "launching" | "waiting_user" | "fetching_courses" | "done" | "error"
    pub message: String,
    pub needs_action: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SessionResult {
    pub profile: SessionProfile,
    pub courses: Vec<CourseItem>,
    pub cookie_count: usize,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct PlaywrightCookie {
    name: String,
    value: String,
    domain: String,
    path: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    expires: Option<f64>,
    #[serde(default)]
    http_only: bool,
    #[serde(default)]
    secure: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    same_site: Option<String>,
}

pub struct ZhsSessionManager {
    active_job: Mutex<Option<Arc<Job>>>,
    cancelled: Arc<AtomicBool>,
}

impl ZhsSessionManager {
    pub fn new() -> Self {
        Self {
            active_job: Mutex::new(None),
            cancelled: Arc::new(AtomicBool::new(false)),
        }
    }

    pub fn cancel(&self) {
        self.cancelled.store(true, Ordering::SeqCst);
        let job = {
            let mut guard = self.active_job.lock().unwrap();
            guard.take()
        };
        if let Some(j) = job {
            j.terminate();
        }
    }

    fn begin(&self, job: Arc<Job>) {
        self.cancelled.store(false, Ordering::SeqCst);
        let prev = {
            let mut guard = self.active_job.lock().unwrap();
            guard.replace(job)
        };
        if let Some(old) = prev {
            old.terminate();
        }
    }

    fn finish(&self) {
        let job = {
            let mut guard = self.active_job.lock().unwrap();
            guard.take()
        };
        if let Some(j) = job {
            j.terminate();
        }
    }

    fn is_cancelled(&self) -> bool {
        self.cancelled.load(Ordering::SeqCst)
    }
}

fn now_epoch_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

fn emit_status(app: &AppHandle, phase: &str, message: &str, needs_action: bool) {
    let _ = app.emit(
        "autovisor-login-status",
        &LoginStatusEvent {
            phase: phase.to_string(),
            message: message.to_string(),
            needs_action,
        },
    );
}

/// Fetches (and caches) Zhihuishu's school list for Student ID login autocomplete.
pub fn search_schools(query: &str, proxy: &str) -> Result<Vec<SchoolItem>, String> {
    let cache_mutex = SCHOOL_CACHE.get_or_init(|| Mutex::new(None));
    let schools: Vec<SchoolItem> = {
        let guard = cache_mutex.lock().unwrap();
        if let Some(ref list) = *guard {
            list.clone()
        } else {
            drop(guard);
            let mut builder = reqwest::blocking::Client::builder()
                .timeout(Duration::from_secs(12))
                .user_agent("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36");
            let trimmed_proxy = proxy.trim();
            if !trimmed_proxy.is_empty() {
                if let Ok(p) = reqwest::Proxy::all(trimmed_proxy) {
                    builder = builder.proxy(p);
                }
            }
            let client = builder
                .build()
                .map_err(|e| format!("初始化 HTTP 客户端失败: {e}"))?;
            let resp = client
                .post(SCHOOL_LIST_URL)
                .header("Referer", "https://passport.zhihuishu.com/login")
                .send()
                .map_err(|e| format!("获取智慧树学校列表失败: {e}"))?;
            let body: Value = resp
                .json()
                .map_err(|e| format!("解析智慧树学校列表失败: {e}"))?;
            let mut parsed = Vec::new();
            if let Some(arr) = body.get("listSchool").and_then(|v| v.as_array()) {
                for item in arr {
                    let name = item
                        .get("name")
                        .and_then(|v| v.as_str())
                        .unwrap_or("")
                        .trim()
                        .to_string();
                    let school_id = match item.get("schoolId") {
                        Some(Value::Number(n)) => n.to_string(),
                        Some(Value::String(s)) => s.trim().to_string(),
                        _ => String::new(),
                    };
                    let letter = item
                        .get("letter")
                        .and_then(|v| v.as_str())
                        .unwrap_or("")
                        .trim()
                        .to_lowercase();
                    if !name.is_empty() && !school_id.is_empty() {
                        parsed.push(SchoolItem {
                            school_id,
                            name,
                            letter,
                        });
                    }
                }
            }
            if parsed.is_empty() {
                return Err("智慧树学校列表为空，请稍后重试".to_string());
            }
            let mut guard = cache_mutex.lock().unwrap();
            *guard = Some(parsed.clone());
            parsed
        }
    };

    let q = query.trim().to_lowercase();
    if q.is_empty() {
        return Ok(schools.into_iter().take(25).collect());
    }

    let mut exact = Vec::new();
    let mut prefix = Vec::new();
    let mut contains = Vec::new();
    for s in schools {
        let name_lower = s.name.to_lowercase();
        if name_lower == q {
            exact.push(s);
        } else if name_lower.starts_with(&q) || s.letter == q {
            prefix.push(s);
        } else if name_lower.contains(&q) {
            contains.push(s);
        }
    }
    let mut out = Vec::with_capacity(30);
    for bucket in [exact, prefix, contains] {
        for item in bucket {
            if out.len() >= 30 {
                break;
            }
            out.push(item);
        }
        if out.len() >= 30 {
            break;
        }
    }
    Ok(out)
}

struct TempProfileGuard {
    dir: PathBuf,
}

impl TempProfileGuard {
    fn create() -> Result<Self, String> {
        let stamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_millis())
            .unwrap_or(0);
        let dir = std::env::temp_dir().join(format!(
            "autovisor-gui-cdp-{}-{}",
            std::process::id(),
            stamp
        ));
        fs::create_dir_all(&dir).map_err(|e| format!("创建临时浏览器配置目录失败: {e}"))?;
        Ok(Self { dir })
    }
}

impl Drop for TempProfileGuard {
    fn drop(&mut self) {
        for _ in 0..5 {
            if fs::remove_dir_all(&self.dir).is_ok() || !self.dir.exists() {
                break;
            }
            thread::sleep(Duration::from_millis(120));
        }
    }
}

fn find_free_port() -> Result<u16, String> {
    let listener =
        TcpListener::bind("127.0.0.1:0").map_err(|e| format!("分配本地调试端口失败: {e}"))?;
    let port = listener
        .local_addr()
        .map_err(|e| format!("获取本地调试端口失败: {e}"))?
        .port();
    drop(listener);
    Ok(port)
}

#[derive(Debug, Clone)]
struct CapturedResponse {
    url: String,
    finished: bool,
    parsed: bool,
}

fn is_static_asset_url(url_lower: &str) -> bool {
    let path = url_lower.split('?').next().unwrap_or(url_lower);
    [
        ".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".woff",
        ".woff2", ".ttf", ".eot", ".mp4", ".m3u8", ".ts", ".map",
    ]
    .iter()
    .any(|ext| path.ends_with(ext))
}

fn decode_base64_utf8(input: &str) -> Option<String> {
    fn val(b: u8) -> Option<u8> {
        match b {
            b'A'..=b'Z' => Some(b - b'A'),
            b'a'..=b'z' => Some(b - b'a' + 26),
            b'0'..=b'9' => Some(b - b'0' + 52),
            b'+' | b'-' => Some(62),
            b'/' | b'_' => Some(63),
            _ => None,
        }
    }
    let mut out = Vec::with_capacity(input.len() * 3 / 4);
    let mut buf: u32 = 0;
    let mut bits: u8 = 0;
    for b in input.bytes() {
        if b.is_ascii_whitespace() || b == b'=' {
            continue;
        }
        let v = val(b)?;
        buf = (buf << 6) | u32::from(v);
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push(((buf >> bits) & 0xff) as u8);
        }
    }
    String::from_utf8(out).ok()
}

struct CdpClient {
    ws: WebSocket<MaybeTlsStream<TcpStream>>,
    next_id: u64,
    responses: HashMap<String, CapturedResponse>,
}

impl CdpClient {
    fn connect(ws_url: &str) -> Result<Self, String> {
        let (ws, _) =
            tungstenite::connect(ws_url).map_err(|e| format!("连接浏览器调试通道失败: {e}"))?;
        if let MaybeTlsStream::Plain(ref stream) = *ws.get_ref() {
            let _ = stream.set_read_timeout(Some(Duration::from_millis(250)));
            let _ = stream.set_write_timeout(Some(Duration::from_secs(5)));
        }
        Ok(Self {
            ws,
            next_id: 1,
            responses: HashMap::new(),
        })
    }

    fn handle_event(&mut self, msg: &Value) {
        let Some(method) = msg.get("method").and_then(|v| v.as_str()) else {
            return;
        };
        let Some(params) = msg.get("params") else {
            return;
        };
        match method {
            "Network.responseReceived" => {
                let req_id = params
                    .get("requestId")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                let resource_type = params
                    .get("type")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                let resp = params.get("response");
                let url = resp
                    .and_then(|r| r.get("url"))
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                let url_lower = url.to_ascii_lowercase();
                let mime = resp
                    .and_then(|r| r.get("mimeType"))
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_ascii_lowercase();
                let status = resp
                    .and_then(|r| r.get("status"))
                    .and_then(|v| v.as_u64())
                    .unwrap_or(0);
                if !req_id.is_empty()
                    && status >= 200
                    && status < 300
                    && url_lower.contains("zhihuishu.com")
                    && !is_static_asset_url(&url_lower)
                    && (mime.contains("json")
                        || mime.contains("text/plain")
                        || matches!(resource_type, "XHR" | "Fetch")
                        || url_lower.contains("course")
                        || url_lower.contains("userinfo")
                        || url_lower.contains("student"))
                {
                    self.responses.insert(
                        req_id.to_string(),
                        CapturedResponse {
                            url: url.to_string(),
                            finished: false,
                            parsed: false,
                        },
                    );
                }
            }
            "Network.loadingFinished" => {
                if let Some(req_id) = params.get("requestId").and_then(|v| v.as_str()) {
                    if let Some(entry) = self.responses.get_mut(req_id) {
                        entry.finished = true;
                    }
                }
            }
            _ => {}
        }
    }

    fn send(&mut self, method: &str, params: Value) -> Result<Value, String> {
        let id = self.next_id;
        self.next_id += 1;
        let payload = json!({
            "id": id,
            "method": method,
            "params": params,
        });
        self.ws
            .send(Message::Text(payload.to_string()))
            .map_err(|e| format!("CDP 发送 {method} 失败: {e}"))?;

        let deadline = Instant::now() + Duration::from_secs(15);
        while Instant::now() < deadline {
            match self.ws.read() {
                Ok(Message::Text(text)) => {
                    if let Ok(val) = serde_json::from_str::<Value>(&text) {
                        if val.get("id").and_then(|v| v.as_u64()) == Some(id) {
                            if let Some(err) = val.get("error") {
                                return Err(format!("CDP {method} 返回错误: {err}"));
                            }
                            return Ok(val.get("result").cloned().unwrap_or(Value::Null));
                        }
                        self.handle_event(&val);
                    }
                }
                Ok(Message::Ping(data)) => {
                    let _ = self.ws.send(Message::Pong(data));
                }
                Ok(Message::Close(_)) => {
                    return Err("浏览器调试连接已关闭".to_string());
                }
                Ok(_) => {}
                Err(tungstenite::Error::Io(ref io_err))
                    if io_err.kind() == std::io::ErrorKind::WouldBlock
                        || io_err.kind() == std::io::ErrorKind::TimedOut =>
                {
                    continue;
                }
                Err(e) => return Err(format!("读取浏览器调试通道失败: {e}")),
            }
        }
        Err(format!("等待 CDP {method} 响应超时"))
    }

    fn drain_events(&mut self, duration: Duration) {
        let deadline = Instant::now() + duration;
        while Instant::now() < deadline {
            match self.ws.read() {
                Ok(Message::Text(text)) => {
                    if let Ok(val) = serde_json::from_str::<Value>(&text) {
                        self.handle_event(&val);
                    }
                }
                Ok(Message::Ping(data)) => {
                    let _ = self.ws.send(Message::Pong(data));
                }
                Ok(Message::Close(_)) => break,
                Ok(_) => {}
                Err(tungstenite::Error::Io(ref io_err))
                    if io_err.kind() == std::io::ErrorKind::WouldBlock
                        || io_err.kind() == std::io::ErrorKind::TimedOut =>
                {
                    thread::sleep(Duration::from_millis(40));
                }
                Err(_) => break,
            }
        }
    }

    fn eval_js(&mut self, expression: &str) -> Result<Value, String> {
        let res = self.send(
            "Runtime.evaluate",
            json!({
                "expression": expression,
                "returnByValue": true,
                "awaitPromise": true,
            }),
        )?;
        Ok(res
            .get("result")
            .and_then(|r| r.get("value"))
            .cloned()
            .unwrap_or(Value::Null))
    }

    fn get_zhihuishu_cookies(&mut self) -> Result<Vec<PlaywrightCookie>, String> {
        let res = self.send("Network.getAllCookies", json!({}))?;
        let raw_cookies = res
            .get("cookies")
            .and_then(|v| v.as_array())
            .cloned()
            .unwrap_or_default();
        Ok(normalize_cdp_cookies(&raw_cookies))
    }

    fn set_cookies(&mut self, cookies: &[PlaywrightCookie]) -> Result<(), String> {
        let mut cdp_cookies = Vec::new();
        for c in cookies {
            if c.name.trim().is_empty() || c.domain.trim().is_empty() {
                continue;
            }
            let mut obj = json!({
                "name": c.name,
                "value": c.value,
                "domain": c.domain,
                "path": if c.path.is_empty() { "/" } else { &c.path },
                "secure": c.secure,
                "httpOnly": c.http_only,
            });
            if let Some(exp) = c.expires {
                if exp > 0.0 {
                    obj["expires"] = json!(exp);
                }
            }
            if let Some(ref ss) = c.same_site {
                if matches!(ss.as_str(), "Strict" | "Lax" | "None") {
                    obj["sameSite"] = json!(ss);
                }
            }
            cdp_cookies.push(obj);
        }
        if !cdp_cookies.is_empty() {
            let _ = self.send("Network.setCookies", json!({ "cookies": cdp_cookies }))?;
        }
        Ok(())
    }

    fn get_response_body(&mut self, request_id: &str) -> Option<String> {
        let res = self
            .send("Network.getResponseBody", json!({ "requestId": request_id }))
            .ok()?;
        let body = res.get("body")?.as_str()?;
        let base64_encoded = res
            .get("base64Encoded")
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        if base64_encoded {
            return decode_base64_utf8(body);
        }
        Some(body.to_string())
    }
}

fn is_zhihuishu_domain(domain: &str) -> bool {
    let norm = domain.trim().trim_start_matches('.').to_ascii_lowercase();
    norm == "zhihuishu.com" || norm.ends_with(".zhihuishu.com")
}

fn is_login_url(url: &str) -> bool {
    let lower = url.to_ascii_lowercase();
    lower.contains("passport.zhihuishu.com") || lower.contains("login.zhihuishu.com")
}

fn normalize_cdp_cookies(raw_cookies: &[Value]) -> Vec<PlaywrightCookie> {
    let now = now_epoch_secs() as f64;
    let mut out = Vec::new();
    for item in raw_cookies {
        let name = item
            .get("name")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim()
            .to_string();
        let domain = item
            .get("domain")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim()
            .to_string();
        if name.is_empty() || !is_zhihuishu_domain(&domain) {
            continue;
        }
        let expires = item
            .get("expires")
            .and_then(|v| v.as_f64())
            .filter(|&e| e > 0.0);
        if let Some(exp) = expires {
            if exp <= now {
                continue;
            }
        }
        let value = item
            .get("value")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let path = item
            .get("path")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .unwrap_or("/")
            .to_string();
        let http_only = item
            .get("httpOnly")
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        let secure = item
            .get("secure")
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        let same_site = item
            .get("sameSite")
            .and_then(|v| v.as_str())
            .filter(|s| matches!(*s, "Strict" | "Lax" | "None"))
            .map(ToString::to_string);

        out.push(PlaywrightCookie {
            name,
            value,
            domain,
            path,
            expires,
            http_only,
            secure,
            same_site,
        });
    }
    out
}

fn save_cookies_atomic(base_dir: &Path, cookies: &[PlaywrightCookie]) -> Result<(), String> {
    let data_dir = base_dir.join("data");
    fs::create_dir_all(&data_dir).map_err(|e| format!("创建 data 目录失败: {e}"))?;
    let target = data_dir.join("cookies.json");
    let tmp = data_dir.join(format!(".cookies-{}.tmp", std::process::id()));
    let json_text =
        serde_json::to_string(cookies).map_err(|e| format!("序列化 Cookie 失败: {e}"))?;
    fs::write(&tmp, json_text.as_bytes()).map_err(|e| format!("写入临时 Cookie 文件失败: {e}"))?;
    fs::rename(&tmp, &target).or_else(|_| {
        let _ = fs::remove_file(&target);
        fs::rename(&tmp, &target)
    }).map_err(|e| format!("保存 Cookie 文件失败: {e}"))?;
    Ok(())
}

fn load_saved_cookies(base_dir: &Path) -> Result<Vec<PlaywrightCookie>, String> {
    let path = base_dir.join("data").join("cookies.json");
    if !path.is_file() {
        return Err("本地暂无保存的登录凭证".to_string());
    }
    let raw = fs::read_to_string(&path).map_err(|e| format!("读取本地 Cookie 失败: {e}"))?;
    let parsed: Vec<PlaywrightCookie> =
        serde_json::from_str(&raw).map_err(|e| format!("本地 Cookie 文件格式无效: {e}"))?;
    if parsed.is_empty() {
        return Err("本地 Cookie 为空".to_string());
    }
    Ok(parsed)
}

fn resolve_session_browser(base_dir: &Path, requested_exe: &str) -> Result<(String, String, String), String> {
    let detected = detect_installed_browsers();
    let cfg = load_config_from_dir(base_dir).unwrap_or_default();
    let preferred_exe = if !requested_exe.trim().is_empty() {
        requested_exe.trim()
    } else {
        cfg.exe_path.trim()
    };
    if let Some((driver, exe_path)) = resolve_browser_choice(&cfg.driver, preferred_exe, &detected) {
        let display_name = detected
            .iter()
            .find(|b| b.exe_path.eq_ignore_ascii_case(&exe_path))
            .map(|b| b.name.clone())
            .unwrap_or_else(|| driver.clone());
        return Ok((driver, exe_path, display_name));
    }
    Err("未在当前设备上检测到 Edge / Chrome 等 Chromium 内核浏览器，请先安装 Chrome 或 Edge 后重试".to_string())
}

fn normalize_chromium_proxy(raw: &str) -> Option<String> {
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return None;
    }
    let (scheme, rest) = trimmed.split_once("://")?;
    let scheme_lower = scheme.to_ascii_lowercase();
    let chromium_scheme = match scheme_lower.as_str() {
        "http" => "http",
        "https" => "https",
        "socks5" | "socks5h" => "socks5",
        _ => return None,
    };
    let host_port = rest.rsplit_once('@').map(|(_, h)| h).unwrap_or(rest);
    let host_port = host_port.trim_end_matches('/');
    if host_port.is_empty() {
        return None;
    }
    Some(format!("{chromium_scheme}://{host_port}"))
}

fn launch_cdp_browser(
    exe_path: &str,
    port: u16,
    profile_dir: &Path,
    headless: bool,
    initial_url: &str,
    proxy: &str,
    manager: &ZhsSessionManager,
) -> Result<Child, String> {
    let job = Arc::new(Job::create()?);
    let mut cmd = Command::new(exe_path);
    cmd.arg(format!("--remote-debugging-port={port}"))
        .arg(format!("--user-data-dir={}", profile_dir.display()))
        .arg("--no-first-run")
        .arg("--no-default-browser-check")
        .arg("--disable-blink-features=AutomationControlled")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());

    if let Some(proxy_arg) = normalize_chromium_proxy(proxy) {
        cmd.arg(format!("--proxy-server={proxy_arg}"));
    }

    if headless {
        cmd.arg("--headless=new")
            .arg("--disable-gpu")
            .arg("--window-size=1440,900");
        configure_hidden_command(&mut cmd);
    } else {
        cmd.arg("--window-size=1080,760")
            .arg("--window-position=140,80");
    }
    prepare_process_tree_command(&mut cmd);
    cmd.arg(initial_url);

    let child = cmd
        .spawn()
        .map_err(|e| format!("启动浏览器失败 ({exe_path}): {e}"))?;
    if let Err(e) = job.assign(child.id()) {
        job.terminate();
        return Err(format!("托管浏览器进程树失败: {e}"));
    }
    manager.begin(job);
    Ok(child)
}

fn wait_for_cdp_ws_url(
    port: u16,
    child: &mut Child,
    manager: &ZhsSessionManager,
) -> Result<String, String> {
    let client = reqwest::blocking::Client::builder()
        .no_proxy()
        .timeout(Duration::from_secs(2))
        .build()
        .map_err(|e| format!("创建本地调试客户端失败: {e}"))?;

    let list_url = format!("http://127.0.0.1:{port}/json/list");
    let deadline = Instant::now() + Duration::from_secs(20);
    while Instant::now() < deadline {
        if manager.is_cancelled() {
            return Err("已取消登录".to_string());
        }
        if let Ok(Some(_)) = child.try_wait() {
            return Err("浏览器已关闭".to_string());
        }
        if let Ok(resp) = client.get(&list_url).send() {
            if let Ok(targets) = resp.json::<Vec<Value>>() {
                if let Some(ws) = targets
                    .iter()
                    .find(|t| t.get("type").and_then(|v| v.as_str()) == Some("page"))
                    .and_then(|t| t.get("webSocketDebuggerUrl"))
                    .and_then(|v| v.as_str())
                {
                    return Ok(ws.to_string());
                }
            }
        }
        thread::sleep(Duration::from_millis(200));
    }
    Err("等待浏览器调试端口就绪超时，请确认浏览器可以正常启动".to_string())
}

fn normalize_zhs_url(raw: &str) -> Option<String> {
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return None;
    }
    let mut full = if trimmed.starts_with("//") {
        format!("https:{trimmed}")
    } else if trimmed.starts_with("http://") {
        trimmed.replacen("http://", "https://", 1)
    } else if trimmed.starts_with("https://") {
        trimmed.to_string()
    } else {
        return None;
    };
    if full.ends_with("#/") {
        full.truncate(full.len() - 2);
    } else if full.ends_with('#') {
        full.truncate(full.len() - 1);
    }
    let lower = full.to_ascii_lowercase();
    let is_course_host = lower.contains("studyvideoh5.zhihuishu.com")
        || lower.contains("fusioncourseh5.zhihuishu.com")
        || lower.contains("studywisdomh5.zhihuishu.com")
        || lower.contains("wisdom-mooc.zhihuishu.com")
        || lower.contains("ai-smart-course-student-pro.zhihuishu.com")
        || lower.contains("hike.zhihuishu.com");
    if is_course_host {
        Some(full)
    } else {
        None
    }
}

fn infer_course_type_label(url: &str) -> &'static str {
    let lower = url.to_ascii_lowercase();
    if lower.contains("ai-smart-course-student-pro.zhihuishu.com") {
        "AI 智慧课"
    } else if lower.contains("wisdom-mooc.zhihuishu.com") {
        "AI 慕课"
    } else if lower.contains("fusioncourseh5.zhihuishu.com") {
        "融合共享课"
    } else if lower.contains("studywisdomh5.zhihuishu.com") || lower.contains("hike.zhihuishu.com") {
        "翻转 / 智慧课"
    } else if lower.contains("studyvideoh5.zhihuishu.com") {
        "智慧共享课"
    } else {
        "智慧树课程"
    }
}

fn is_generic_course_title(title: &str) -> bool {
    let t = title.trim();
    t.is_empty()
        || matches!(
            t,
            "智慧共享课"
                | "融合共享课"
                | "翻转 / 智慧课"
                | "校内学分课"
                | "AI 慕课"
                | "AI 智慧课"
                | "智慧树课程"
                | "进入学习"
                | "继续学习"
                | "开始学习"
                | "去学习"
                | "立即学习"
                | "查看详情"
                | "进入课程"
                | "开始上课"
        )
}

fn should_skip_course_container_key(key: &str) -> bool {
    let lower = key.trim().to_ascii_lowercase();
    if lower.is_empty() {
        return false;
    }
    [
        "recommend",
        "hotcourse",
        "banner",
        "advert",
        "adlist",
        "guesslike",
        "popular",
        "endcourse",
        "finishcourse",
        "finishedcourse",
        "overcourse",
        "historycourse",
        "pastcourse",
        "closedcourse",
    ]
    .iter()
    .any(|bad| lower.contains(bad))
}

fn get_string_field(obj: &serde_json::Map<String, Value>, keys: &[&str]) -> String {
    for key in keys {
        if let Some(val) = obj.get(*key) {
            match val {
                Value::String(s) if !s.trim().is_empty() => return s.trim().to_string(),
                Value::Number(n) => return n.to_string(),
                _ => {}
            }
        }
    }
    String::new()
}

fn format_progress_value(obj: &serde_json::Map<String, Value>) -> String {
    for key in [
        "progress",
        "studyProcess",
        "studyProgress",
        "lessonProgress",
        "learnProgress",
        "studyRate",
        "progressRate",
        "completeRate",
        "progressPercent",
        "finishedPercent",
        "schedule",
        "process",
        "finishPercent",
    ] {
        if let Some(val) = obj.get(key) {
            match val {
                Value::String(s) => {
                    let t = s.trim();
                    if t.is_empty() {
                        continue;
                    }
                    if t.ends_with('%') {
                        return t.to_string();
                    }
                    if let Ok(num) = t.parse::<f64>() {
                        let pct = if num > 0.0 && num <= 1.0 && t.contains('.') {
                            num * 100.0
                        } else {
                            num
                        };
                        return format!("{:.0}%", pct.clamp(0.0, 100.0));
                    }
                    return t.to_string();
                }
                Value::Number(n) => {
                    if let Some(num) = n.as_f64() {
                        let pct = if num > 0.0 && num <= 1.0 && !n.is_u64() {
                            num * 100.0
                        } else {
                            num
                        };
                        return format!("{:.0}%", pct.clamp(0.0, 100.0));
                    }
                }
                _ => {}
            }
        }
    }
    String::new()
}

fn is_truthy_flag(obj: &serde_json::Map<String, Value>, keys: &[&str]) -> bool {
    for key in keys {
        if let Some(val) = obj.get(*key) {
            match val {
                Value::Bool(true) => return true,
                Value::Number(n) if n.as_i64() == Some(1) => return true,
                Value::String(s)
                    if matches!(s.trim().to_ascii_lowercase().as_str(), "1" | "true") =>
                {
                    return true;
                }
                _ => {}
            }
        }
    }
    false
}

fn build_course_from_json_object(
    obj: &serde_json::Map<String, Value>,
    parent_key: &str,
    source_url: &str,
) -> Option<CourseItem> {
    let raw_title = get_string_field(obj, &["courseName", "courseTitle", "name", "title"]);
    if raw_title.chars().count() > 120 {
        return None;
    }

    let parent_lower = parent_key.to_ascii_lowercase();
    let source_lower = source_url.to_ascii_lowercase();

    // Check direct URL fields first
    let mut url = None;
    for key in [
        "studyUrl",
        "playUrl",
        "pcUrl",
        "courseUrl",
        "linkUrl",
        "videoUrl",
        "jumpUrl",
        "h5Url",
        "newCourseUrl",
        "learnUrl",
        "studentUrl",
        "coursePlayUrl",
        "detailUrl",
        "webUrl",
        "href",
        "url",
    ] {
        if let Some(raw) = obj.get(key).and_then(|v| v.as_str()) {
            if let Some(norm) = normalize_zhs_url(raw) {
                url = Some(norm);
                break;
            }
        }
    }

    let has_course_marker = obj.contains_key("courseName")
        || obj.contains_key("courseTitle")
        || obj.contains_key("recruitAndCourseId")
        || obj.contains_key("courseId")
        || obj.contains_key("courseType")
        || obj.contains_key("teacherName")
        || obj.contains_key("schoolName")
        || parent_lower.contains("course");

    // Fallback 1: construct playable URL from Zhihuishu's encrypted secret / recruitAndCourseId
    if url.is_none() && has_course_marker {
        let secret = get_string_field(obj, &["secret", "recruitAndCourseId", "secretStr"]);
        if !secret.is_empty()
            && secret.len() >= 4
            && !secret.chars().any(char::is_whitespace)
        {
            let is_fusion_flag = is_truthy_flag(obj, &["fusionCourseFlag", "isFusion", "fusionFlag"]);
            let course_type_num = obj
                .get("courseType")
                .and_then(|v| {
                    v.as_i64()
                        .or_else(|| v.as_str().and_then(|s| s.trim().parse::<i64>().ok()))
                })
                .unwrap_or(0);
            let mode_hint = format!(
                "{} {} {} {} {} {} {} {}",
                get_string_field(obj, &["courseType"]),
                get_string_field(obj, &["shareType"]),
                get_string_field(obj, &["courseModel"]),
                get_string_field(obj, &["typeName"]),
                get_string_field(obj, &["courseTag"]),
                raw_title,
                parent_lower,
                source_lower
            )
            .to_ascii_lowercase();
            let is_fusion = is_fusion_flag
                || course_type_num == 4
                || mode_hint.contains("fusion")
                || mode_hint.contains("融合");
            let constructed = if is_fusion {
                format!("https://fusioncourseh5.zhihuishu.com/stuStudy?recruitAndCourseId={secret}")
            } else {
                format!("https://studyvideoh5.zhihuishu.com/stuStudy?recruitAndCourseId={secret}")
            };
            url = Some(constructed);
        }
    }

    // Fallback 2: AI 智慧课 (ai-smart-course-student-pro.zhihuishu.com)
    if url.is_none() {
        let course_id = get_string_field(obj, &["courseId", "aiCourseId"]);
        let class_id = get_string_field(obj, &["classId", "recruitId"]);
        let ai_hint = format!(
            "{} {} {} {} {}",
            get_string_field(obj, &["courseType"]),
            get_string_field(obj, &["typeName"]),
            get_string_field(obj, &["courseModel"]),
            parent_lower,
            source_lower
        )
        .to_ascii_lowercase();
        let is_ai_smart = obj.contains_key("aiCourseId")
            || ai_hint.contains("ai-smart")
            || ai_hint.contains("aicourse")
            || ai_hint.contains("ai智慧")
            || ai_hint.contains("ai 智慧")
            || (ai_hint.contains("ai") && !ai_hint.contains("aided"));
        if !course_id.is_empty() && !class_id.is_empty() && is_ai_smart {
            url = Some(format!(
                "https://ai-smart-course-student-pro.zhihuishu.com/learnPage?courseId={course_id}&classId={class_id}"
            ));
        }
    }

    // Fallback 3: 校内学分课 / 翻转课 (hike.zhihuishu.com)
    if url.is_none() && has_course_marker {
        let course_id = get_string_field(obj, &["courseId"]);
        let pc_url_lower = get_string_field(obj, &["pcUrl", "studyUrl", "url"]).to_ascii_lowercase();
        let is_hike = parent_lower.contains("startinng")
            || parent_lower.contains("hike")
            || parent_lower.contains("aided")
            || parent_lower.contains("schoolcourse")
            || parent_lower.contains("creditcourse")
            || source_lower.contains("hikeservice.zhihuishu.com")
            || source_lower.contains("hike.zhihuishu.com")
            || source_lower.contains("/aided/")
            || obj.contains_key("courseNature")
            || obj.contains_key("aidedTeaching")
            || pc_url_lower.contains("hike");
        if !course_id.is_empty() && is_hike {
            url = Some(format!(
                "https://hike.zhihuishu.com/aidedteaching/sourceLearning/sourceLearning?courseId={course_id}"
            ));
        }
    }

    let final_url = url?;
    let course_type = infer_course_type_label(&final_url).to_string();
    let title = if raw_title.is_empty() {
        course_type.clone()
    } else {
        raw_title
    };
    let teacher = get_string_field(
        obj,
        &[
            "teacherName",
            "speakerName",
            "mainTeacher",
            "lecturerName",
            "masterTeacherName",
            "teacherNames",
            "teacher",
        ],
    );
    let school = get_string_field(
        obj,
        &["schoolName", "collegeName", "universityName", "orgName", "school"],
    );
    let progress = format_progress_value(obj);

    Some(CourseItem {
        id: format!("auto-{}", short_hash(&final_url)),
        title,
        url: final_url,
        enabled: true,
        teacher,
        school,
        progress,
        course_type,
        auto_detected: true,
    })
}

fn short_hash(input: &str) -> String {
    let mut h: u64 = 0xcbf29ce484222325;
    for b in input.as_bytes() {
        h ^= u64::from(*b);
        h = h.wrapping_mul(0x100000001b3);
    }
    format!("{h:016x}")
}

fn extract_courses_and_profile_from_json_with_context(
    val: &Value,
    parent_key: &str,
    source_url: &str,
    courses: &mut Vec<CourseItem>,
    display_name: &mut String,
    school_name: &mut String,
) {
    match val {
        Value::Array(arr) => {
            for item in arr {
                extract_courses_and_profile_from_json_with_context(
                    item,
                    parent_key,
                    source_url,
                    courses,
                    display_name,
                    school_name,
                );
            }
        }
        Value::Object(map) => {
            let built_course = build_course_from_json_object(map, parent_key, source_url);
            let looks_like_course = built_course.is_some()
                || map.contains_key("courseName")
                || map.contains_key("courseTitle")
                || map.contains_key("recruitAndCourseId")
                || map.contains_key("secret")
                || map.contains_key("courseId");
            if let Some(course) = built_course {
                courses.push(course);
            } else if !looks_like_course {
                if display_name.is_empty() {
                    let candidate =
                        get_string_field(map, &["realName", "nickName", "userName", "studentName"]);
                    if !candidate.is_empty() && candidate.chars().count() <= 32 {
                        *display_name = candidate;
                    }
                }
                if school_name.is_empty() {
                    let sch = get_string_field(map, &["schoolName", "collegeName", "universityName"]);
                    if !sch.is_empty() && sch.chars().count() <= 48 {
                        *school_name = sch;
                    }
                }
            }

            for (k, v) in map {
                if should_skip_course_container_key(k) {
                    continue;
                }
                match v {
                    Value::Array(_) | Value::Object(_) => {
                        extract_courses_and_profile_from_json_with_context(
                            v,
                            k,
                            source_url,
                            courses,
                            display_name,
                            school_name,
                        );
                    }
                    Value::String(s)
                        if matches!(k.as_str(), "result" | "data" | "rt" | "content") =>
                    {
                        let trimmed = s.trim();
                        if (trimmed.starts_with('{') && trimmed.ends_with('}'))
                            || (trimmed.starts_with('[') && trimmed.ends_with(']'))
                        {
                            if let Ok(nested) = serde_json::from_str::<Value>(trimmed) {
                                extract_courses_and_profile_from_json_with_context(
                                    &nested,
                                    k,
                                    source_url,
                                    courses,
                                    display_name,
                                    school_name,
                                );
                            }
                        }
                    }
                    _ => {}
                }
            }
        }
        _ => {}
    }
}

#[cfg(test)]
fn extract_courses_and_profile_from_json(
    val: &Value,
    courses: &mut Vec<CourseItem>,
    display_name: &mut String,
    school_name: &mut String,
) {
    extract_courses_and_profile_from_json_with_context(
        val,
        "",
        "",
        courses,
        display_name,
        school_name,
    );
}

fn dedupe_discovered_courses(raw: Vec<CourseItem>) -> Vec<CourseItem> {
    let mut by_url: HashMap<String, CourseItem> = HashMap::new();
    let mut order: Vec<String> = Vec::new();
    for mut item in raw {
        let Some(norm_url) = normalize_zhs_url(&item.url) else {
            continue;
        };
        item.url = norm_url.clone();
        item.title = item.title.trim().to_string();
        item.teacher = item.teacher.trim().to_string();
        item.school = item.school.trim().to_string();
        item.progress = item.progress.trim().to_string();
        if item.course_type.trim().is_empty() {
            item.course_type = infer_course_type_label(&norm_url).to_string();
        }
        if item.title.is_empty() {
            item.title = item.course_type.clone();
        }
        if let Some(existing) = by_url.get_mut(&norm_url) {
            if is_generic_course_title(&existing.title) && !is_generic_course_title(&item.title) {
                existing.title = item.title;
            }
            if existing.teacher.is_empty() && !item.teacher.is_empty() {
                existing.teacher = item.teacher;
            }
            if existing.school.is_empty() && !item.school.is_empty() {
                existing.school = item.school;
            }
            if existing.progress.is_empty() && !item.progress.is_empty() {
                existing.progress = item.progress;
            }
            if existing.course_type == "智慧树课程" && item.course_type != "智慧树课程" {
                existing.course_type = item.course_type;
            }
        } else {
            order.push(norm_url.clone());
            by_url.insert(norm_url, item);
        }
    }
    order
        .into_iter()
        .filter_map(|k| by_url.remove(&k))
        .collect()
}

fn run_login_autofill(cdp: &mut CdpClient, req: &LoginRequest) -> Result<Value, String> {
    let payload = json!({
        "mode": req.mode,
        "username": req.username.trim(),
        "schoolId": req.school_id.trim(),
        "schoolName": req.school_name.trim(),
        "studentId": req.student_id.trim(),
        "password": req.password,
    });
    let js = format!(
        r#"(async () => {{
            const cfg = {payload};
            const sleep = (ms) => new Promise(r => setTimeout(r, ms));
            const isVisible = (el) => {{
                if (!el) return false;
                const s = getComputedStyle(el);
                return s.display !== 'none' && s.visibility !== 'hidden' && el.offsetWidth > 0 && el.offsetHeight > 0;
            }};
            const waitFor = async (fn, timeoutMs = 5000, intervalMs = 150) => {{
                const deadline = Date.now() + timeoutMs;
                while (Date.now() < deadline) {{
                    const res = fn();
                    if (res) return res;
                    await sleep(intervalMs);
                }}
                return fn();
            }};
            const setInputEl = (el, val) => {{
                if (!el) return false;
                el.focus();
                const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                if (desc && typeof desc.set === 'function') {{
                    desc.set.call(el, val);
                }} else {{
                    el.value = val;
                }}
                el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                return true;
            }};
            const setInput = (sel, val) => setInputEl(document.querySelector(sel), val);
            const checkTerms = () => {{
                const cbs = Array.from(document.querySelectorAll('input.el-checkbox__original'));
                for (const cb of cbs) {{
                    if (!cb.checked) cb.click();
                }}
            }};
            const findNewTab = (id, keywords) => {{
                const byId = id ? document.getElementById(id) : null;
                if (byId && isVisible(byId)) return byId;
                return Array.from(document.querySelectorAll('.login-tabs .el-tabs__item, .el-tabs__item'))
                    .find(el => isVisible(el) && keywords.some(k => (el.innerText || '').trim().includes(k))) || null;
            }};
            const clickNewSubmit = async () => {{
                const btn = await waitFor(() => {{
                    const el = document.querySelector('.btn-block__grandient_login');
                    if (!el || !isVisible(el)) return null;
                    if (el.classList.contains('primary') || !el.classList.contains('info')) return el;
                    return null;
                }}, 2500, 120) || document.querySelector('.btn-block__grandient_login');
                if (btn && isVisible(btn)) {{
                    btn.click();
                    return true;
                }}
                return false;
            }};

            const isNewLogin = location.hostname.includes('login.zhihuishu.com')
                || Boolean(document.querySelector('.login-tabs, .btn-block__grandient_login, input[name="mobile"], input[name="unid"]'));

            if (isNewLogin) {{
                if (cfg.mode === 'qrcode') {{
                    const tab = findNewTab('tab-3', ['扫码']);
                    if (tab) tab.click();
                    return {{ ok: Boolean(tab), page: 'new', mode: 'qrcode' }};
                }}

                if (cfg.mode === 'student') {{
                    const tab = findNewTab('tab-2', ['学号']);
                    if (tab) tab.click();
                    const unidEl = await waitFor(() => {{
                        const el = document.querySelector('input[name="unid"]');
                        return isVisible(el) ? el : null;
                    }}, 4000, 120);

                    let schoolPicked = false;
                    if (cfg.schoolName) {{
                        const wrapper = document.querySelector('.el-select__wrapper, .el-select');
                        if (wrapper) wrapper.click();
                        await sleep(200);
                        const schInput = await waitFor(() => {{
                            const el = document.querySelector('input.el-select__input');
                            return isVisible(el) ? el : null;
                        }}, 3000, 120);
                        if (schInput) {{
                            setInputEl(schInput, cfg.schoolName);
                            const opt = await waitFor(() => {{
                                const items = Array.from(document.querySelectorAll('.el-select-dropdown__item')).filter(isVisible);
                                if (!items.length) return null;
                                return items.find(i => (i.innerText || '').trim() === cfg.schoolName)
                                    || items.find(i => (i.innerText || '').trim().includes(cfg.schoolName))
                                    || items[0];
                            }}, 5000, 180);
                            if (opt) {{
                                opt.click();
                                schoolPicked = true;
                                await sleep(250);
                            }}
                        }}
                    }}

                    const filledId = setInputEl(unidEl || document.querySelector('input[name="unid"]'), cfg.studentId);
                    const pwdEl = Array.from(document.querySelectorAll('input[type="password"]')).find(isVisible)
                        || document.querySelector('input[type="password"]');
                    const filledPwd = setInputEl(pwdEl, cfg.password);
                    checkTerms();
                    await sleep(300);
                    let submitted = false;
                    if (schoolPicked && filledId && filledPwd && cfg.studentId && cfg.password) {{
                        submitted = await clickNewSubmit();
                    }}
                    return {{ ok: filledId && filledPwd, page: 'new', mode: 'student', schoolPicked, submitted }};
                }}

                // phone / account mode on new login page
                const tab = findNewTab('tab-1.1', ['账号登录', '手机号']);
                if (tab && !tab.classList.contains('is-active')) {{
                    tab.click();
                    await sleep(250);
                }}
                const mobileEl = await waitFor(() => {{
                    const el = document.querySelector('input[name="mobile"]');
                    return isVisible(el) ? el : null;
                }}, 4000, 120);
                const filledUser = setInputEl(mobileEl || document.querySelector('input[name="mobile"]'), cfg.username);
                const pwdEl = Array.from(document.querySelectorAll('input[type="password"]')).find(isVisible)
                    || document.querySelector('input[type="password"]');
                const filledPwd = setInputEl(pwdEl, cfg.password);
                checkTerms();
                await sleep(300);
                let submitted = false;
                if (filledUser && filledPwd && cfg.username && cfg.password) {{
                    submitted = await clickNewSubmit();
                }}
                return {{ ok: filledUser && filledPwd, page: 'new', mode: 'phone', submitted }};
            }}

            // Legacy passport.zhihuishu.com fallback
            if (cfg.mode === 'qrcode') {{
                const qrWall = document.querySelector('.qrcode-wall-warp');
                if (qrWall && isVisible(qrWall)) {{
                    return {{ ok: true, page: 'legacy', mode: 'qrcode' }};
                }}
                const tab = document.querySelector('#qrCodeLoginNew');
                if (tab && isVisible(tab)) tab.click();
                return {{ ok: true, page: 'legacy', mode: 'qrcode' }};
            }}

            if (cfg.mode === 'student') {{
                const tab = document.querySelector('#qStudentID');
                if (tab) tab.click();
                await sleep(250);
                if (cfg.schoolId) {{
                    const hidden = document.querySelector('#clSchoolId');
                    if (hidden) hidden.value = cfg.schoolId;
                }}
                if (cfg.schoolName) {{
                    setInput('#quickSearch', cfg.schoolName);
                    if (!cfg.schoolId) {{
                        try {{
                            if (window.userindex && typeof window.userindex.selectSchoolByName === 'function') {{
                                window.userindex.selectSchoolByName();
                            }}
                        }} catch (e) {{}}
                        await sleep(500);
                        const firstItem = document.querySelector('#schoolListCode li');
                        if (firstItem) firstItem.click();
                    }}
                }}
                setInput('#clCode', cfg.studentId);
                setInput('#clPassword', cfg.password);
                checkTerms();
                await sleep(300);
                const btn = document.querySelector('.wall-sub-btn');
                if (btn && cfg.studentId && cfg.password) btn.click();
                return {{ ok: true, page: 'legacy', mode: 'student' }};
            }}

            const tab = document.querySelector('#qSignin');
            if (tab) tab.click();
            await sleep(200);
            setInput('#lUsername', cfg.username);
            setInput('#lPassword', cfg.password);
            checkTerms();
            await sleep(300);
            const btn = document.querySelector('.wall-sub-btn');
            if (btn && cfg.username && cfg.password) btn.click();
            return {{ ok: true, page: 'legacy', mode: 'phone' }};
        }})()"#
    );
    cdp.eval_js(&js)
}

fn collect_captured_responses(
    cdp: &mut CdpClient,
    include_unfinished: bool,
    discovered: &mut Vec<CourseItem>,
    display_name: &mut String,
    school_name: &mut String,
) {
    let pending: Vec<(String, String)> = cdp
        .responses
        .iter()
        .filter(|(_, meta)| !meta.parsed && (include_unfinished || meta.finished))
        .map(|(id, meta)| (id.clone(), meta.url.clone()))
        .collect();

    for (req_id, source_url) in pending {
        if let Some(entry) = cdp.responses.get_mut(&req_id) {
            entry.parsed = true;
        }
        if let Some(body_str) = cdp.get_response_body(&req_id) {
            let trimmed = body_str.trim();
            if !(trimmed.starts_with('{') || trimmed.starts_with('[')) {
                continue;
            }
            if let Ok(json_val) = serde_json::from_str::<Value>(trimmed) {
                extract_courses_and_profile_from_json_with_context(
                    &json_val,
                    "",
                    &source_url,
                    discovered,
                    display_name,
                    school_name,
                );
            }
        }
    }
}

fn discover_courses_in_current_session(
    cdp: &mut CdpClient,
    app: &AppHandle,
    manager: &ZhsSessionManager,
) -> Result<(Vec<CourseItem>, String, String), String> {
    emit_status(
        app,
        "fetching_courses",
        "登录成功，正在自动识别智慧树课程队列...",
        false,
    );

    let mut discovered: Vec<CourseItem> = Vec::new();
    let mut display_name = String::new();
    let mut school_name = String::new();

    for attempt in 0..2 {
        if attempt == 0 {
            let current_url = cdp
                .eval_js("window.location.href")
                .ok()
                .and_then(|v| v.as_str().map(ToString::to_string))
                .unwrap_or_default();
            if !current_url.contains("onlineweb.zhihuishu.com/onlinestuh5") {
                let _ = cdp.send("Page.navigate", json!({ "url": STUDENT_HOME_URL }));
            }
        } else {
            // Retry once with a clean reload when the initial SPA boot raced with CAS cookie settlement
            let _ = cdp.send("Page.navigate", json!({ "url": STUDENT_HOME_URL }));
        }

        // Wait for student home page & initial XHR course queries to settle
        let wait_deadline = Instant::now() + Duration::from_secs(6);
        let mut ready_ticks = 0u32;
        while Instant::now() < wait_deadline {
            if manager.is_cancelled() {
                return Err("已取消操作".to_string());
            }
            cdp.drain_events(Duration::from_millis(400));
            collect_captured_responses(
                cdp,
                false,
                &mut discovered,
                &mut display_name,
                &mut school_name,
            );
            let page_ready = cdp
                .eval_js(
                    r#"(() => ({
                        url: location.href || '',
                        ready: document.readyState || '',
                        hasBody: Boolean(document.body && (document.body.innerText || '').trim().length > 20)
                    }))()"#,
                )
                .ok()
                .map(|v| {
                    let url = v.get("url").and_then(|x| x.as_str()).unwrap_or("");
                    let ready = v.get("ready").and_then(|x| x.as_str()).unwrap_or("");
                    let has_body = v.get("hasBody").and_then(|x| x.as_bool()).unwrap_or(false);
                    url.contains("onlineweb.zhihuishu.com") && ready != "loading" && has_body
                })
                .unwrap_or(false);
            if page_ready {
                ready_ticks += 1;
                if ready_ticks >= 3 && (!discovered.is_empty() || ready_ticks >= 5) {
                    break;
                }
            }
        }

        // Try switching across course category tabs (共享课 / 翻转课 / 校内课 / 融合课 / AI课)
        // and advancing pagination so Zhihuishu's SPA fires the API for every enrolled category.
        let _ = cdp.eval_js(ZHS_TAB_SWITCH_JS);
        cdp.drain_events(Duration::from_millis(1200));

        // Layer 1: Parse all captured Zhihuishu JSON API responses (including any newly triggered by tab clicks)
        collect_captured_responses(
            cdp,
            true,
            &mut discovered,
            &mut display_name,
            &mut school_name,
        );

        // Layer 2: Inspect DOM links, Vue 2 & Vue 3 reactive state, and user header
        let dom_val = cdp
            .eval_js(ZHS_DOM_EXTRACT_JS)
            .unwrap_or(Value::Null);

        if let Some(arr) = dom_val.get("courses").and_then(|v| v.as_array()) {
            for item in arr {
                if let Some(map) = item.as_object() {
                    let raw_url = get_string_field(map, &["url"]);
                    let Some(norm_url) = normalize_zhs_url(&raw_url) else {
                        continue;
                    };
                    let course_type = infer_course_type_label(&norm_url).to_string();
                    let raw_title = get_string_field(map, &["title"]);
                    let title = if is_generic_course_title(&raw_title) {
                        course_type.clone()
                    } else {
                        raw_title
                    };
                    let progress = format_progress_value(map);
                    discovered.push(CourseItem {
                        id: format!("auto-{}", short_hash(&norm_url)),
                        title,
                        url: norm_url,
                        enabled: true,
                        teacher: get_string_field(map, &["teacher"]),
                        school: get_string_field(map, &["school"]),
                        progress,
                        course_type,
                        auto_detected: true,
                    });
                }
            }
        }

        if display_name.is_empty() {
            if let Some(name) = dom_val.get("userName").and_then(|v| v.as_str()) {
                let trimmed = name.trim();
                if !trimmed.is_empty() {
                    display_name = trimmed.to_string();
                }
            }
        }

        if !discovered.is_empty() {
            break;
        }
    }

    Ok((dedupe_discovered_courses(discovered), display_name, school_name))
}

fn mask_account(raw: &str) -> String {
    let t = raw.trim();
    let chars: Vec<char> = t.chars().collect();
    if chars.len() >= 11 && chars.iter().all(|c| c.is_ascii_digit()) {
        let head: String = chars[..3].iter().collect();
        let tail: String = chars[chars.len() - 4..].iter().collect();
        format!("{head}****{tail}")
    } else {
        t.to_string()
    }
}

pub fn execute_zhs_login(
    app: &AppHandle,
    manager: &ZhsSessionManager,
    backend: &BackendManager,
    base_dir: &Path,
    req: LoginRequest,
) -> Result<SessionResult, String> {
    let (driver, browser_exe, browser_name) = resolve_session_browser(base_dir, &req.browser_exe)?;
    let is_cookie_mode = req.mode == "cookie";
    let auto_captcha = load_config_from_dir(base_dir)
        .map(|cfg| cfg.enable_auto_captcha)
        .unwrap_or(true);

    let saved_cookies = if is_cookie_mode {
        Some(load_saved_cookies(base_dir)?)
    } else {
        None
    };

    let _profile_guard = TempProfileGuard::create()?;
    let port = find_free_port()?;

    let launch_msg = if is_cookie_mode {
        format!("正在使用本机 {browser_name} 校验已保存的登录状态...")
    } else {
        format!("正在启动本机 {browser_name} 进行登录...")
    };
    emit_status(app, "launching", &launch_msg, false);

    let effective_proxy = if !req.proxy.trim().is_empty() {
        req.proxy.trim().to_string()
    } else {
        load_config_from_dir(base_dir)
            .map(|c| c.proxy)
            .unwrap_or_default()
    };

    let initial_url = if is_cookie_mode {
        "about:blank"
    } else {
        NEW_LOGIN_ENTRY_URL
    };
    let mut child = launch_cdp_browser(
        &browser_exe,
        port,
        &_profile_guard.dir,
        is_cookie_mode,
        initial_url,
        &effective_proxy,
        manager,
    )?;

    let result = (|| -> Result<SessionResult, String> {
        let ws_url = wait_for_cdp_ws_url(port, &mut child, manager)?;
        let mut cdp = CdpClient::connect(&ws_url)?;
        let _ = cdp.send("Page.enable", json!({}))?;
        let _ = cdp.send("Network.enable", json!({}))?;
        let _ = cdp.send("Runtime.enable", json!({}))?;

        if let Some(ref existing_cookies) = saved_cookies {
            cdp.set_cookies(existing_cookies)?;
            // Navigate via CAS entry so a valid CASTGC automatically refreshes the onlinestuh5 ST ticket.
            let _ = cdp.send("Page.navigate", json!({ "url": LOGIN_ENTRY_URL }))?;
            let check_deadline = Instant::now() + Duration::from_secs(15);
            let mut landed = false;
            let mut landed_at: Option<Instant> = None;
            while Instant::now() < check_deadline {
                if manager.is_cancelled() {
                    return Err("已取消操作".to_string());
                }
                if let Ok(Some(_)) = child.try_wait() {
                    return Err("校验浏览器意外关闭".to_string());
                }
                cdp.drain_events(Duration::from_millis(350));
                let state = cdp
                    .eval_js(
                        r#"(() => ({
                            url: window.location.href || '',
                            ready: document.readyState || '',
                            hasNewLoginForm: Boolean(document.querySelector('.login-tabs, input[name="mobile"], input[name="unid"]'))
                        }))()"#,
                    )
                    .unwrap_or(Value::Null);
                let cur = state.get("url").and_then(|v| v.as_str()).unwrap_or("");
                let has_new_form = state
                    .get("hasNewLoginForm")
                    .and_then(|v| v.as_bool())
                    .unwrap_or(false);

                // Only fail fast when CAS explicitly fell through to the rendered login.zhihuishu.com form.
                if cur.to_ascii_lowercase().contains("login.zhihuishu.com") && has_new_form {
                    return Err("已保存的登录凭证已失效，请重新登录".to_string());
                }
                if cur.contains("onlineweb.zhihuishu.com/onlinestuh5") && !cur.contains("ticket=") {
                    match landed_at {
                        None => landed_at = Some(Instant::now()),
                        Some(since) if since.elapsed() >= Duration::from_millis(900) => {
                            landed = true;
                            break;
                        }
                        _ => {}
                    }
                } else {
                    landed_at = None;
                }
            }
            if !landed {
                let cur = cdp
                    .eval_js("window.location.href")
                    .ok()
                    .and_then(|v| v.as_str().map(ToString::to_string))
                    .unwrap_or_default();
                if is_login_url(&cur) || cur.is_empty() || cur == "about:blank" {
                    return Err("已保存的登录凭证已失效，请重新登录".to_string());
                }
            }
        } else {
            // Wait for login page (Vue SPA on login.zhihuishu.com or legacy fallback) to mount controls.
            let ready_deadline = Instant::now() + Duration::from_secs(25);
            let mut legacy_seen_since: Option<Instant> = None;
            while Instant::now() < ready_deadline {
                if manager.is_cancelled() {
                    return Err("已取消登录".to_string());
                }
                if let Ok(Some(_)) = child.try_wait() {
                    return Err("登录浏览器已关闭".to_string());
                }
                let state = cdp
                    .eval_js(
                        r#"(() => ({
                            url: location.href || '',
                            host: location.hostname || '',
                            ready: document.readyState || '',
                            hasNewControls: Boolean(document.querySelector('.login-tabs') && (document.querySelector('input[name="mobile"]') || document.querySelector('input[name="unid"]') || document.getElementById('tab-3'))),
                            hasLegacyControls: Boolean(document.querySelector('#qSignin'))
                        }))()"#,
                    )
                    .unwrap_or(Value::Null);
                let url = state.get("url").and_then(|v| v.as_str()).unwrap_or("");
                let host = state
                    .get("host")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_ascii_lowercase();
                let ready_state = state.get("ready").and_then(|v| v.as_str()).unwrap_or("");
                let has_new = state
                    .get("hasNewControls")
                    .and_then(|v| v.as_bool())
                    .unwrap_or(false);
                let has_legacy = state
                    .get("hasLegacyControls")
                    .and_then(|v| v.as_bool())
                    .unwrap_or(false);

                if !url.is_empty() && url != "about:blank" && !is_login_url(url) {
                    break;
                }
                if host.contains("login.zhihuishu.com") && has_new {
                    break;
                }
                // Legacy passport page redirects to login.zhihuishu.com via JS; only accept it if it stays put.
                if host.contains("passport.zhihuishu.com")
                    && ready_state == "complete"
                    && has_legacy
                {
                    match legacy_seen_since {
                        None => legacy_seen_since = Some(Instant::now()),
                        Some(since) if since.elapsed() >= Duration::from_secs(2) => break,
                        _ => {}
                    }
                } else {
                    legacy_seen_since = None;
                }
                thread::sleep(Duration::from_millis(250));
            }

            let cur_url = cdp
                .eval_js("window.location.href")
                .ok()
                .and_then(|v| v.as_str().map(ToString::to_string))
                .unwrap_or_default();
            if is_login_url(&cur_url) {
                let autofill_res = run_login_autofill(&mut cdp, &req);
                let school_missing = matches!(
                    &autofill_res,
                    Ok(val) if req.mode == "student" && val.get("schoolPicked").and_then(|v| v.as_bool()) == Some(false)
                );
                let autofill_failed = autofill_res.is_err()
                    || matches!(
                        &autofill_res,
                        Ok(val) if val.get("ok").and_then(|v| v.as_bool()) == Some(false)
                    );
                let hint = if autofill_failed {
                    "登录页控件加载异常，请在弹出的浏览器窗口中手动完成登录"
                } else if school_missing {
                    "已自动填写学号与密码，请在浏览器中确认学校后点击登录并完成滑块验证"
                } else if auto_captcha {
                    match req.mode.as_str() {
                        "student" => {
                            "已自动填写学校与学号并触发登录，正在尝试自动完成滑块验证（失败请在弹出的浏览器窗口中手动完成）"
                        }
                        "qrcode" => {
                            "已切换到扫码登录，请在弹出的浏览器窗口中使用微信或知到 App 扫码"
                        }
                        _ => {
                            "已自动填写手机号与密码并触发登录，正在尝试自动完成滑块验证（失败请在弹出的浏览器窗口中手动完成）"
                        }
                    }
                } else {
                    match req.mode.as_str() {
                        "student" => {
                            "已自动填写学校与学号并触发登录，请在弹出的浏览器窗口中完成滑块验证"
                        }
                        "qrcode" => {
                            "已切换到扫码登录，请在弹出的浏览器窗口中使用微信或知到 App 扫码"
                        }
                        _ => {
                            "已自动填写手机号与密码并触发登录，请在弹出的浏览器窗口中完成滑块验证"
                        }
                    }
                };
                emit_status(app, "waiting_user", hint, true);

                let login_deadline = Instant::now() + Duration::from_secs(300);
                let mut last_err_text = String::new();
                let mut captcha_attempts: u32 = 0;
                let mut last_captcha_attempt: Option<Instant> = None;
                let mut captcha_hint_shown = false;
                while Instant::now() < login_deadline {
                    if manager.is_cancelled() {
                        return Err("已取消登录".to_string());
                    }
                    if let Ok(Some(_)) = child.try_wait() {
                        return Err("登录窗口已关闭，未完成登录".to_string());
                    }
                    cdp.drain_events(Duration::from_millis(350));

                    let state = cdp
                        .eval_js(
                            r#"(() => {
                                const url = window.location.href || '';
                                const errs = [];
                                document.querySelectorAll('.el-message__content, .el-form-item__error, .el-notification__content, .form-ipt-error.is-visible, .validate-error-tip-text, .layui-layer-content, .error-tips-pop').forEach(el => {
                                    const s = getComputedStyle(el);
                                    if (s.display !== 'none' && s.visibility !== 'hidden' && el.offsetWidth > 0) {
                                        const t = (el.innerText || '').trim();
                                        if (t) errs.push(t);
                                    }
                                });
                                const captchaSelectors = '.yidun_bg-img, .yidun_bgimg, .yidun_popup, .yidun_modal, #tcaptcha_iframe_dy, iframe[id^="tcaptcha"]';
                                const captcha = Array.from(document.querySelectorAll(captchaSelectors)).some(el => {
                                    const s = getComputedStyle(el);
                                    const r = el.getBoundingClientRect();
                                    return s.display !== 'none' && s.visibility !== 'hidden'
                                        && Number.parseFloat(s.opacity || '1') > 0.05
                                        && r.width > 20 && r.height > 20;
                                });
                                return { url, err: errs[0] || '', captcha };
                            })()"#,
                        )
                        .unwrap_or(Value::Null);

                    let url_now = state
                        .get("url")
                        .and_then(|v| v.as_str())
                        .unwrap_or("");
                    if !url_now.is_empty() && !is_login_url(url_now) && url_now != "about:blank" {
                        break;
                    }
                    let err_now = state
                        .get("err")
                        .and_then(|v| v.as_str())
                        .unwrap_or("")
                        .trim();
                    if !err_now.is_empty() && err_now != last_err_text {
                        last_err_text = err_now.to_string();
                        emit_status(
                            app,
                            "waiting_user",
                            &format!("智慧树提示：{err_now}（可在浏览器中修正后继续登录）"),
                            true,
                        );
                    }

                    let captcha_now = state
                        .get("captcha")
                        .and_then(|v| v.as_bool())
                        .unwrap_or(false);
                    if auto_captcha && captcha_now && !url_now.is_empty() && is_login_url(url_now) {
                        let should_try = last_captcha_attempt
                            .map(|seen| seen.elapsed() >= Duration::from_secs(10))
                            .unwrap_or(true);
                        if should_try && captcha_attempts < 3 {
                            captcha_attempts += 1;
                            last_captcha_attempt = Some(Instant::now());
                            emit_status(
                                app,
                                "verifying",
                                "检测到安全验证，正在自动完成滑块验证...",
                                true,
                            );
                            let payload = json!({
                                "cdpUrl": format!("http://127.0.0.1:{port}"),
                                "pageUrl": url_now,
                                "appearMs": 8000,
                                "maxAttempts": 2,
                            });
                            match backend.post_json(
                                Some(app),
                                "/api/captcha/solve",
                                &payload,
                                75,
                            ) {
                                Ok(res)
                                    if res.get("solved").and_then(|v| v.as_bool())
                                        == Some(true) =>
                                {
                                    emit_status(
                                        app,
                                        "verifying",
                                        "自动验证已通过，正在等待登录结果...",
                                        true,
                                    );
                                    captcha_hint_shown = false;
                                }
                                Ok(res) => {
                                    let reason = res
                                        .get("reason")
                                        .and_then(|v| v.as_str())
                                        .unwrap_or("自动验证未通过");
                                    if !captcha_hint_shown {
                                        emit_status(
                                            app,
                                            "waiting_user",
                                            &format!("{reason}，请在浏览器窗口中手动完成验证"),
                                            true,
                                        );
                                        captcha_hint_shown = true;
                                    }
                                }
                                Err(err) => {
                                    if !captcha_hint_shown {
                                        emit_status(
                                            app,
                                            "waiting_user",
                                            &format!(
                                                "自动验证不可用（{err}），请在浏览器窗口中手动完成验证"
                                            ),
                                            true,
                                        );
                                        captcha_hint_shown = true;
                                    }
                                }
                            }
                        }
                    } else if !captcha_now {
                        captcha_attempts = 0;
                        last_captcha_attempt = None;
                        captcha_hint_shown = false;
                    }
                }

                let final_url = cdp
                    .eval_js("window.location.href")
                    .ok()
                    .and_then(|v| v.as_str().map(ToString::to_string))
                    .unwrap_or_default();
                if final_url.is_empty() || is_login_url(&final_url) {
                    return Err("等待登录超时，请重试".to_string());
                }
            }
        }

        // Logged in! Collect cookies & discover courses
        let (discovered_courses, detected_name, detected_school) =
            discover_courses_in_current_session(&mut cdp, app, manager)?;

        let cookies = cdp.get_zhihuishu_cookies()?;
        if cookies.is_empty() {
            return Err("登录成功但未获取到智慧树 Cookie，请重试".to_string());
        }
        save_cookies_atomic(base_dir, &cookies)?;

        // Merge with existing courses & save config.ini + gui_courses.json
        let mut cfg = load_config_from_dir(base_dir).unwrap_or_default();
        cfg.driver = driver;
        cfg.exe_path = browser_exe;
        if req.mode == "phone" && req.remember_credentials {
            cfg.username = req.username.trim().to_string();
            cfg.password = req.password.clone();
        }
        if !discovered_courses.is_empty() {
            cfg.courses = merge_discovered_courses(&cfg.courses, discovered_courses);
        }
        let saved_cfg = save_config_to_dir(base_dir, &cfg)?;

        // Save session profile metadata (never storing password)
        let mut prefs = load_prefs();
        let prev_session = prefs.session.clone();
        let account = if req.mode == "student" && !req.student_id.trim().is_empty() {
            req.student_id.trim().to_string()
        } else if req.mode == "phone" && !req.username.trim().is_empty() {
            mask_account(&req.username)
        } else if is_cookie_mode {
            prev_session
                .as_ref()
                .map(|s| s.account.clone())
                .unwrap_or_default()
        } else {
            String::new()
        };

        let school_name = if !req.school_name.trim().is_empty() {
            req.school_name.trim().to_string()
        } else if !detected_school.is_empty() {
            detected_school
        } else {
            prev_session
                .as_ref()
                .map(|s| s.school_name.clone())
                .unwrap_or_default()
        };

        let school_id = if !req.school_id.trim().is_empty() {
            req.school_id.trim().to_string()
        } else {
            prev_session
                .as_ref()
                .map(|s| s.school_id.clone())
                .unwrap_or_default()
        };

        let login_mode = if is_cookie_mode {
            prev_session
                .as_ref()
                .map(|s| s.login_mode.clone())
                .filter(|m| !m.is_empty())
                .unwrap_or_else(|| "cookie".to_string())
        } else {
            req.mode.clone()
        };

        let display_name = if !detected_name.is_empty() {
            detected_name
        } else if !account.is_empty() {
            account.clone()
        } else {
            prev_session
                .as_ref()
                .map(|s| s.display_name.clone())
                .filter(|d| !d.is_empty())
                .unwrap_or_else(|| "智慧树学员".to_string())
        };

        let profile = SessionProfile {
            logged_in: true,
            display_name,
            account,
            login_mode,
            school_name,
            school_id,
            last_synced_at: Some(now_epoch_secs()),
        };

        prefs.session = Some(profile.clone());
        let _ = save_prefs(&prefs);

        emit_status(
            app,
            "done",
            &format!("已同步 {} 门课程", saved_cfg.courses.len()),
            false,
        );

        Ok(SessionResult {
            profile,
            courses: saved_cfg.courses,
            cookie_count: cookies.len(),
        })
    })();

    manager.finish();
    let _ = child.kill();
    let _ = child.wait();
    result
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn extracts_shared_and_fusion_courses_from_zhihuishu_json() {
        let payload = json!({
            "code": 200,
            "result": {
                "userInfo": {
                    "realName": "张三",
                    "schoolName": "示范大学"
                },
                "courseOpenDtos": [
                    {
                        "courseName": "高等数学（上）",
                        "teacherName": "李教授",
                        "schoolName": "示范大学",
                        "progress": "62%",
                        "secret": "abcdef1234567890"
                    },
                    {
                        "courseName": "人工智能通识",
                        "teacherName": "王老师",
                        "fusionCourseFlag": 1,
                        "studyProcess": 35,
                        "secret": "fusion9876543210"
                    },
                    {
                        "courseName": "AI 创新实践",
                        "studyUrl": "https://wisdom-mooc.zhihuishu.com/study/999",
                        "progress": "100%"
                    }
                ]
            }
        });

        let mut courses = Vec::new();
        let mut name = String::new();
        let mut school = String::new();
        extract_courses_and_profile_from_json(&payload, &mut courses, &mut name, &mut school);

        assert_eq!(name, "张三");
        assert_eq!(school, "示范大学");
        assert_eq!(courses.len(), 3);
        assert_eq!(
            courses[0].url,
            "https://studyvideoh5.zhihuishu.com/stuStudy?recruitAndCourseId=abcdef1234567890"
        );
        assert_eq!(courses[0].course_type, "智慧共享课");
        assert_eq!(courses[0].progress, "62%");

        assert_eq!(
            courses[1].url,
            "https://fusioncourseh5.zhihuishu.com/stuStudy?recruitAndCourseId=fusion9876543210"
        );
        assert_eq!(courses[1].course_type, "融合共享课");
        assert_eq!(courses[1].progress, "35%");

        assert_eq!(courses[2].url, "https://wisdom-mooc.zhihuishu.com/study/999");
        assert_eq!(courses[2].course_type, "AI 慕课");
    }

    #[test]
    fn normalizes_cdp_cookies_filtering_domains_and_expired() {
        let future = (now_epoch_secs() + 86400) as f64;
        let past = (now_epoch_secs().saturating_sub(3600)) as f64;
        let raw = vec![
            json!({
                "name": "CASTGC",
                "value": "tgt-123",
                "domain": ".zhihuishu.com",
                "path": "/",
                "expires": future,
                "httpOnly": true,
                "secure": true,
                "sameSite": "Lax"
            }),
            json!({
                "name": "expired_cookie",
                "value": "old",
                "domain": ".zhihuishu.com",
                "expires": past
            }),
            json!({
                "name": "other_site",
                "value": "x",
                "domain": ".example.com",
                "expires": future
            }),
        ];

        let cookies = normalize_cdp_cookies(&raw);
        assert_eq!(cookies.len(), 1);
        assert_eq!(cookies[0].name, "CASTGC");
        assert!(cookies[0].http_only);
        assert_eq!(cookies[0].same_site.as_deref(), Some("Lax"));
    }

    #[test]
    fn extracts_hike_ai_smart_non_coursename_and_skips_recommend_or_ended() {
        let long_cn_title = "习近平新时代中国特色社会主义思想概论与当代青年历史使命深度专题研学实践课程与跨学科综合素养提升训练营（2026秋季强化班）";
        assert!(long_cn_title.len() > 140);
        assert!(long_cn_title.chars().count() < 120);

        let payload = json!({
            "code": 200,
            "result": {
                "startInngcourseList": [
                    {
                        "courseId": 2048991,
                        "courseName": long_cn_title,
                        "teacherName": "陈教授",
                        "schoolName": "示范大学",
                        "studyRate": "48%"
                    }
                ],
                "aiCourseList": [
                    {
                        "courseId": "9001",
                        "classId": "8002",
                        "courseTitle": "AI 智慧微积分",
                        "teacher": "周老师",
                        "progress": 0.75
                    }
                ],
                "otherList": [
                    {
                        "title": "协议相对地址课程",
                        "studyUrl": "//wisdom-mooc.zhihuishu.com/play/555#/"
                    }
                ],
                "endCourseList": [
                    {
                        "courseId": 999999,
                        "courseName": "已完结旧课"
                    }
                ],
                "recommendCourseList": [
                    {
                        "courseName": "推荐广告课程",
                        "secret": "rec12345678"
                    }
                ]
            }
        });

        let mut courses = Vec::new();
        let mut name = String::new();
        let mut school = String::new();
        extract_courses_and_profile_from_json_with_context(
            &payload,
            "",
            "https://hikeservice.zhihuishu.com/student/course/aided/getMyCourseList",
            &mut courses,
            &mut name,
            &mut school,
        );

        assert_eq!(courses.len(), 3);
        let hike = courses
            .iter()
            .find(|c| {
                c.url == "https://hike.zhihuishu.com/aidedteaching/sourceLearning/sourceLearning?courseId=2048991"
            })
            .expect("hike course should be extracted");
        assert_eq!(hike.title, long_cn_title);
        assert_eq!(hike.progress, "48%");

        let ai = courses
            .iter()
            .find(|c| {
                c.url == "https://ai-smart-course-student-pro.zhihuishu.com/learnPage?courseId=9001&classId=8002"
            })
            .expect("ai smart course should be extracted");
        assert_eq!(ai.title, "AI 智慧微积分");
        assert_eq!(ai.progress, "75%");

        let mooc = courses
            .iter()
            .find(|c| c.url == "https://wisdom-mooc.zhihuishu.com/play/555")
            .expect("protocol-relative course should be extracted");
        assert_eq!(mooc.title, "协议相对地址课程");
    }

    #[test]
    fn dedupe_prefers_real_title_over_generic_and_normalizes_hash_suffix() {
        let raw = vec![
            CourseItem {
                id: "1".into(),
                title: "进入学习".into(),
                url: "https://studyvideoh5.zhihuishu.com/stuStudy?recruitAndCourseId=abc12345#/".into(),
                enabled: true,
                teacher: "".into(),
                school: "".into(),
                progress: "".into(),
                course_type: "智慧共享课".into(),
                auto_detected: true,
            },
            CourseItem {
                id: "2".into(),
                title: "数据结构与算法".into(),
                url: "https://studyvideoh5.zhihuishu.com/stuStudy?recruitAndCourseId=abc12345".into(),
                enabled: true,
                teacher: "刘教授".into(),
                school: "示范大学".into(),
                progress: "88%".into(),
                course_type: "智慧共享课".into(),
                auto_detected: true,
            },
        ];
        let deduped = dedupe_discovered_courses(raw);
        assert_eq!(deduped.len(), 1);
        assert_eq!(deduped[0].title, "数据结构与算法");
        assert_eq!(deduped[0].teacher, "刘教授");
        assert_eq!(deduped[0].progress, "88%");
        assert_eq!(
            deduped[0].url,
            "https://studyvideoh5.zhihuishu.com/stuStudy?recruitAndCourseId=abc12345"
        );
    }

    #[test]
    fn decodes_base64_utf8_response_body() {
        let encoded = "eyJjb3Vyc2VOYW1lIjoi6auY562J5pWw5a2mIn0=";
        let decoded = decode_base64_utf8(encoded).expect("valid utf8 base64");
        assert_eq!(decoded, r#"{"courseName":"高等数学"}"#);
    }
}
