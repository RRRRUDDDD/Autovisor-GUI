use super::cookies::{normalize_cdp_cookies, PlaywrightCookie};
use super::ZhsSessionManager;
use crate::process_tree::{configure_hidden_command, prepare_process_tree_command, Job};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::fs;
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tungstenite::stream::MaybeTlsStream;
use tungstenite::{Message, WebSocket};

pub(crate) struct TempProfileGuard {
    pub dir: PathBuf,
}

impl TempProfileGuard {
    pub fn create() -> Result<Self, String> {
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

pub(crate) fn find_free_port() -> Result<u16, String> {
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
pub(crate) struct CapturedResponse {
    pub url: String,
    pub finished: bool,
    pub parsed: bool,
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

pub(crate) fn decode_base64_utf8(input: &str) -> Option<String> {
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

pub(crate) struct CdpClient {
    ws: WebSocket<MaybeTlsStream<TcpStream>>,
    next_id: u64,
    pub responses: HashMap<String, CapturedResponse>,
}

impl CdpClient {
    pub fn connect(ws_url: &str) -> Result<Self, String> {
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
                    && (200..300).contains(&status)
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

    pub fn send(&mut self, method: &str, params: Value) -> Result<Value, String> {
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

    pub fn drain_events(&mut self, duration: Duration) {
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

    pub fn eval_js(&mut self, expression: &str) -> Result<Value, String> {
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

    pub fn get_zhihuishu_cookies(&mut self) -> Result<Vec<PlaywrightCookie>, String> {
        let res = self.send("Network.getAllCookies", json!({}))?;
        let raw_cookies = res
            .get("cookies")
            .and_then(|v| v.as_array())
            .cloned()
            .unwrap_or_default();
        Ok(normalize_cdp_cookies(&raw_cookies))
    }

    pub fn set_cookies(&mut self, cookies: &[PlaywrightCookie]) -> Result<(), String> {
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

    pub fn get_response_body(&mut self, request_id: &str) -> Option<String> {
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

pub(crate) fn normalize_chromium_proxy(raw: &str) -> Option<String> {
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

#[allow(clippy::too_many_arguments)]
pub(crate) fn launch_cdp_browser(
    exe_path: &str,
    port: u16,
    profile_dir: &Path,
    headless: bool,
    initial_url: &str,
    proxy: &str,
    manager: &ZhsSessionManager,
    session_id: u64,
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

    let mut child = cmd
        .spawn()
        .map_err(|e| format!("启动浏览器失败 ({exe_path}): {e}"))?;
    if let Err(e) = job.assign(child.id()) {
        let _ = child.kill();
        let _ = child.wait();
        job.terminate_with_child(Some(&mut child));
        return Err(format!("托管浏览器进程树失败: {e}"));
    }
    if !manager.attach_job(session_id, job) {
        let _ = child.kill();
        let _ = child.wait();
        return Err("已取消登录".to_string());
    }
    Ok(child)
}

pub(crate) fn wait_for_cdp_ws_url(
    port: u16,
    child: &mut Child,
    cancel_flag: &AtomicBool,
) -> Result<String, String> {
    let client = reqwest::blocking::Client::builder()
        .no_proxy()
        .timeout(Duration::from_secs(2))
        .build()
        .map_err(|e| format!("创建本地调试客户端失败: {e}"))?;

    let list_url = format!("http://127.0.0.1:{port}/json/list");
    let deadline = Instant::now() + Duration::from_secs(20);
    while Instant::now() < deadline {
        if cancel_flag.load(Ordering::SeqCst) {
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
