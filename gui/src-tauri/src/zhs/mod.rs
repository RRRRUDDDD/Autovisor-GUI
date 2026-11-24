pub mod cdp;
pub mod cookies;
pub mod schools;

pub use schools::{search_schools_async, SchoolItem};

use crate::backend::{lock_unpoisoned, BackendManager};
use crate::browser_detect::{detect_installed_browsers, resolve_browser_choice};
use crate::config::{
    load_config_from_dir, merge_discovered_courses, save_config_to_dir, AutovisorConfig, CourseItem,
};
use crate::process_tree::Job;
use crate::runtime::{update_prefs, SavedLoginInfo, SessionProfile};
use cdp::{
    find_free_port, launch_cdp_browser, wait_for_cdp_ws_url, CdpClient, TempProfileGuard,
};
use cookies::{
    is_login_url, load_saved_cookies, merge_cookies, now_epoch_secs, save_cookies_atomic,
    PlaywrightCookie,
};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::path::Path;
use std::process::Child;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};
use tauri::{AppHandle, Emitter};

const LOGIN_ENTRY_URL: &str =
    "https://passport.zhihuishu.com/login?service=https%3A%2F%2Fonlineweb.zhihuishu.com%2Fonlinestuh5";
const NEW_LOGIN_ENTRY_URL: &str =
    "https://login.zhihuishu.com/?origin=zhs&service=https%3A%2F%2Fonlineweb.zhihuishu.com%2Fonlinestuh5";
const STUDENT_HOME_URL: &str = "https://onlineweb.zhihuishu.com/onlinestuh5";
const ZHS_TAB_SWITCH_JS: &str = include_str!("../../../../resources/scripts/zhs_tab_switch.js");
const ZHS_DOM_EXTRACT_JS: &str = include_str!("../../../../resources/scripts/zhs_dom_extract.js");
const ZHS_LOGIN_AUTOFILL_JS: &str =
    include_str!("../../../../resources/scripts/zhs_login_autofill.js");

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
    pub phase: String,
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

struct ActiveSession {
    id: u64,
    job: Option<Arc<Job>>,
    cancel_flag: Arc<AtomicBool>,
}

pub struct ZhsSessionManager {
    active_session: Mutex<Option<ActiveSession>>,
    next_session_id: AtomicU64,
}

impl Default for ZhsSessionManager {
    fn default() -> Self {
        Self::new()
    }
}

impl ZhsSessionManager {
    pub fn new() -> Self {
        Self {
            active_session: Mutex::new(None),
            next_session_id: AtomicU64::new(0),
        }
    }

    pub fn cancel(&self) {
        let prev = {
            let mut guard = lock_unpoisoned(&self.active_session);
            guard.take()
        };
        if let Some(sess) = prev {
            sess.cancel_flag.store(true, Ordering::SeqCst);
            if let Some(j) = sess.job {
                j.terminate();
            }
        }
    }

    fn start_attempt(&self) -> (u64, Arc<AtomicBool>) {
        let sid = self.next_session_id.fetch_add(1, Ordering::SeqCst) + 1;
        let cancel_flag = Arc::new(AtomicBool::new(false));
        let prev = {
            let mut guard = lock_unpoisoned(&self.active_session);
            guard.replace(ActiveSession {
                id: sid,
                job: None,
                cancel_flag: Arc::clone(&cancel_flag),
            })
        };
        if let Some(old) = prev {
            old.cancel_flag.store(true, Ordering::SeqCst);
            if let Some(j) = old.job {
                j.terminate();
            }
        }
        (sid, cancel_flag)
    }

    pub(crate) fn attach_job(&self, session_id: u64, job: Arc<Job>) -> bool {
        let mut guard = lock_unpoisoned(&self.active_session);
        if let Some(ref mut sess) = *guard {
            if sess.id == session_id && !sess.cancel_flag.load(Ordering::SeqCst) {
                let old_job = sess.job.replace(job);
                drop(guard);
                if let Some(old) = old_job {
                    old.terminate();
                }
                return true;
            }
        }
        drop(guard);
        job.terminate();
        false
    }

    #[cfg(test)]
    fn begin(&self, job: Arc<Job>) -> u64 {
        let (sid, _) = self.start_attempt();
        let _ = self.attach_job(sid, job);
        sid
    }

    fn finish_with_child(&self, session_id: u64, child: Option<&mut Child>) {
        let job = {
            let mut guard = lock_unpoisoned(&self.active_session);
            if guard.as_ref().map(|s| s.id) == Some(session_id) {
                guard.take().and_then(|s| s.job)
            } else {
                None
            }
        };
        if let Some(j) = job {
            j.terminate_with_child(child);
        } else if let Some(c) = child {
            let _ = c.try_wait();
        }
    }

    #[cfg(test)]
    fn finish(&self, session_id: u64) {
        self.finish_with_child(session_id, None);
    }
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

fn resolve_session_browser(
    base_dir: &Path,
    requested_exe: &str,
) -> Result<(String, String, String), String> {
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

fn short_hash(input: &str) -> String {
    let mut h: u64 = 0xcbf29ce484222325;
    for b in input.as_bytes() {
        h ^= u64::from(*b);
        h = h.wrapping_mul(0x100000001b3);
    }
    format!("{h:016x}")
}

fn merge_backend_courses(primary: Vec<CourseItem>, secondary: Vec<CourseItem>) -> Vec<CourseItem> {
    let mut by_url: HashMap<String, CourseItem> = HashMap::new();
    let mut order: Vec<String> = Vec::new();
    for item in primary.into_iter().chain(secondary) {
        let url = item.url.trim().to_string();
        if url.is_empty() {
            continue;
        }
        if let Some(existing) = by_url.get_mut(&url) {
            let existing_is_fallback =
                existing.title.is_empty() || existing.title == existing.course_type;
            let item_is_real = !item.title.is_empty() && item.title != item.course_type;
            if existing_is_fallback && item_is_real {
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
            order.push(url.clone());
            by_url.insert(url, item);
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
    let js = format!("({ZHS_LOGIN_AUTOFILL_JS})({payload})");
    cdp.eval_js(&js)
}

fn collect_captured_responses(
    cdp: &mut CdpClient,
    include_unfinished: bool,
    captured: &mut Vec<Value>,
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
            captured.push(json!({
                "url": source_url,
                "body": trimmed,
            }));
        }
    }
}

fn extract_courses_via_backend(
    app: &AppHandle,
    backend: &BackendManager,
    responses: &[Value],
    dom_courses: &[Value],
    dom_user_name: &str,
) -> Result<ApiCoursesResponse, String> {
    let payload = json!({
        "responses": responses,
        "domCourses": dom_courses,
        "domUserName": dom_user_name,
    });
    let resp = backend.post_json(Some(app), "/api/courses/extract", &payload, 30)?;
    Ok(parse_backend_courses_response(&resp))
}

fn discover_courses_in_current_session(
    cdp: &mut CdpClient,
    app: &AppHandle,
    backend: &BackendManager,
    cancel_flag: &AtomicBool,
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
        let mut captured_responses: Vec<Value> = Vec::new();
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
            let _ = cdp.send("Page.navigate", json!({ "url": STUDENT_HOME_URL }));
        }

        let wait_deadline = Instant::now() + Duration::from_secs(6);
        let mut ready_ticks = 0u32;
        while Instant::now() < wait_deadline {
            if cancel_flag.load(Ordering::SeqCst) {
                return Err("已取消操作".to_string());
            }
            cdp.drain_events(Duration::from_millis(400));
            collect_captured_responses(cdp, false, &mut captured_responses);
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
                let has_course_resp = captured_responses.iter().any(|entry| {
                    entry
                        .get("url")
                        .and_then(|v| v.as_str())
                        .map(|u| u.to_ascii_lowercase().contains("course"))
                        .unwrap_or(false)
                });
                if ready_ticks >= 3 && (has_course_resp || ready_ticks >= 5) {
                    break;
                }
            }
        }

        let _ = cdp.eval_js(ZHS_TAB_SWITCH_JS);
        cdp.drain_events(Duration::from_millis(1200));

        collect_captured_responses(cdp, true, &mut captured_responses);

        let dom_val = cdp.eval_js(ZHS_DOM_EXTRACT_JS).unwrap_or(Value::Null);
        let dom_courses: Vec<Value> = dom_val
            .get("courses")
            .and_then(|v| v.as_array())
            .cloned()
            .unwrap_or_default();
        let dom_user_name = dom_val
            .get("userName")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim();

        if let Ok(extracted) = extract_courses_via_backend(
            app,
            backend,
            &captured_responses,
            &dom_courses,
            dom_user_name,
        ) {
            discovered = merge_backend_courses(discovered, extracted.courses);
            if display_name.is_empty() && !extracted.display_name.is_empty() {
                display_name = extracted.display_name;
            }
            if school_name.is_empty() && !extracted.school_name.is_empty() {
                school_name = extracted.school_name;
            }
        } else if display_name.is_empty() && !dom_user_name.is_empty() {
            display_name = dom_user_name.to_string();
        }

        if !discovered.is_empty() {
            break;
        }
    }

    Ok((discovered, display_name, school_name))
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

struct ApiCoursesResponse {
    session_valid: bool,
    courses: Vec<CourseItem>,
    display_name: String,
    school_name: String,
    cookies: Vec<PlaywrightCookie>,
}

fn parse_backend_courses_response(resp: &Value) -> ApiCoursesResponse {
    let session_valid = resp
        .get("sessionValid")
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    let display_name = resp
        .get("displayName")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .trim()
        .to_string();
    let school_name = resp
        .get("schoolName")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .trim()
        .to_string();
    let mut courses = Vec::new();
    if let Some(arr) = resp.get("courses").and_then(|v| v.as_array()) {
        for item in arr {
            let Some(map) = item.as_object() else {
                continue;
            };
            let url = get_string_field(map, &["url"]);
            if url.is_empty() {
                continue;
            }
            let course_type = {
                let raw_ct = get_string_field(map, &["courseType", "course_type"]);
                if raw_ct.is_empty() {
                    "智慧树课程".to_string()
                } else {
                    raw_ct
                }
            };
            let raw_title = get_string_field(map, &["title"]);
            let title = if raw_title.is_empty() {
                course_type.clone()
            } else {
                raw_title
            };
            courses.push(CourseItem {
                id: format!("auto-{}", short_hash(&url)),
                title,
                url,
                enabled: true,
                teacher: get_string_field(map, &["teacher"]),
                school: get_string_field(map, &["school"]),
                progress: get_string_field(map, &["progress"]),
                course_type,
                auto_detected: true,
            });
        }
    }
    let refreshed_cookies: Vec<PlaywrightCookie> = resp
        .get("cookies")
        .and_then(|v| serde_json::from_value(v.clone()).ok())
        .unwrap_or_default();

    ApiCoursesResponse {
        session_valid,
        courses: merge_backend_courses(courses, Vec::new()),
        display_name,
        school_name,
        cookies: refreshed_cookies,
    }
}

fn fetch_courses_via_backend_api(
    app: &AppHandle,
    backend: &BackendManager,
    cookies: &[PlaywrightCookie],
    proxy: &str,
) -> Result<ApiCoursesResponse, String> {
    let payload = json!({
        "cookies": cookies,
        "proxy": proxy,
    });
    let resp = backend.post_json(Some(app), "/api/courses/fetch", &payload, 30)?;
    Ok(parse_backend_courses_response(&resp))
}

fn merge_and_save_session_config(
    base_dir: &Path,
    req: &LoginRequest,
    browser_override: Option<(String, String)>,
    discovered_courses: Vec<CourseItem>,
) -> Result<AutovisorConfig, String> {
    let mut cfg = load_config_from_dir(base_dir)?;
    if let Some((driver, browser_exe)) = browser_override {
        cfg.driver = driver;
        cfg.exe_path = browser_exe;
    }
    if req.mode == "phone" && req.remember_credentials {
        cfg.username = req.username.trim().to_string();
        cfg.password = req.password.clone();
    }
    if !discovered_courses.is_empty() {
        cfg.courses = merge_discovered_courses(&cfg.courses, discovered_courses);
    }
    save_config_to_dir(base_dir, &cfg)
}

#[allow(clippy::too_many_arguments)]
fn finalize_session_result(
    app: &AppHandle,
    base_dir: &Path,
    req: &LoginRequest,
    is_cookie_mode: bool,
    browser_override: Option<(String, String)>,
    cookies: &[PlaywrightCookie],
    discovered_courses: Vec<CourseItem>,
    detected_name: String,
    detected_school: String,
) -> Result<SessionResult, String> {
    save_cookies_atomic(base_dir, cookies)?;

    let saved_cfg =
        merge_and_save_session_config(base_dir, req, browser_override, discovered_courses)?;

    let mut profile_out: Option<SessionProfile> = None;
    let _ = update_prefs(|prefs| {
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
            detected_school.clone()
        } else if is_cookie_mode {
            prev_session
                .as_ref()
                .map(|s| s.school_name.clone())
                .unwrap_or_default()
        } else {
            String::new()
        };

        let school_id = if !req.school_id.trim().is_empty() {
            req.school_id.trim().to_string()
        } else if is_cookie_mode {
            prev_session
                .as_ref()
                .map(|s| s.school_id.clone())
                .unwrap_or_default()
        } else {
            String::new()
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
            detected_name.clone()
        } else if !account.is_empty() {
            account.clone()
        } else if is_cookie_mode {
            prev_session
                .as_ref()
                .map(|s| s.display_name.clone())
                .filter(|d| !d.is_empty())
                .unwrap_or_else(|| "智慧树学员".to_string())
        } else {
            "智慧树学员".to_string()
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

        if !is_cookie_mode {
            let mut saved: SavedLoginInfo = prefs.saved_login.clone().unwrap_or_default();
            if matches!(req.mode.as_str(), "phone" | "student" | "qrcode") {
                saved.login_mode = req.mode.clone();
            }
            if req.mode == "phone" {
                if req.remember_credentials {
                    saved.remember_phone = true;
                    saved.phone_username = req.username.trim().to_string();
                    saved.phone_password = req.password.clone();
                } else {
                    saved.remember_phone = false;
                    saved.phone_username.clear();
                    saved.phone_password.clear();
                }
            } else if req.mode == "student" {
                if req.remember_credentials {
                    saved.remember_student = true;
                    saved.school_id = req.school_id.trim().to_string();
                    saved.school_name = req.school_name.trim().to_string();
                    saved.student_id = req.student_id.trim().to_string();
                    saved.student_password = req.password.clone();
                } else {
                    saved.remember_student = false;
                    saved.school_id.clear();
                    saved.school_name.clear();
                    saved.student_id.clear();
                    saved.student_password.clear();
                }
            }
            prefs.saved_login = Some(saved);
        }

        prefs.session = Some(profile.clone());
        profile_out = Some(profile);
    });

    let profile = profile_out.unwrap_or_else(|| SessionProfile {
        logged_in: true,
        display_name: if !detected_name.is_empty() {
            detected_name
        } else {
            "智慧树学员".to_string()
        },
        account: String::new(),
        login_mode: req.mode.clone(),
        school_name: detected_school,
        school_id: req.school_id.trim().to_string(),
        last_synced_at: Some(now_epoch_secs()),
    });

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
}

pub fn execute_zhs_login(
    app: &AppHandle,
    manager: &ZhsSessionManager,
    backend: &BackendManager,
    base_dir: &Path,
    req: LoginRequest,
) -> Result<SessionResult, String> {
    let (session_id, cancel_flag) = manager.start_attempt();
    let is_cookie_mode = req.mode == "cookie";
    let auto_captcha = load_config_from_dir(base_dir)
        .map(|cfg| cfg.enable_auto_captcha)
        .unwrap_or(true);

    let effective_proxy = if !req.proxy.trim().is_empty() {
        req.proxy.trim().to_string()
    } else {
        load_config_from_dir(base_dir)
            .map(|c| c.proxy)
            .unwrap_or_default()
    };

    let saved_cookies = if is_cookie_mode {
        Some(load_saved_cookies(base_dir)?)
    } else {
        None
    };

    let mut valid_api_fallback: Option<(ApiCoursesResponse, Vec<PlaywrightCookie>)> = None;
    if let Some(ref existing_cookies) = saved_cookies {
        emit_status(
            app,
            "fetching_courses",
            "正在校验登录状态并同步智慧树课程队列...",
            false,
        );
        let api_result =
            fetch_courses_via_backend_api(app, backend, existing_cookies, &effective_proxy);
        if cancel_flag.load(Ordering::SeqCst) {
            return Err("已取消操作".to_string());
        }
        if let Ok(api_res) = api_result {
            if api_res.session_valid {
                let merged_cookies = merge_cookies(existing_cookies, &api_res.cookies);
                if !api_res.courses.is_empty() {
                    let browser_override = resolve_session_browser(base_dir, &req.browser_exe)
                        .ok()
                        .map(|(d, exe, _)| (d, exe));
                    return finalize_session_result(
                        app,
                        base_dir,
                        &req,
                        true,
                        browser_override,
                        &merged_cookies,
                        api_res.courses,
                        api_res.display_name,
                        api_res.school_name,
                    );
                }
                valid_api_fallback = Some((api_res, merged_cookies));
            }
        }
    }

    let (driver, browser_exe, browser_name) = match resolve_session_browser(base_dir, &req.browser_exe)
    {
        Ok(resolved) => resolved,
        Err(err) => {
            if let Some((api_res, merged_cookies)) = valid_api_fallback {
                return finalize_session_result(
                    app,
                    base_dir,
                    &req,
                    true,
                    None,
                    &merged_cookies,
                    api_res.courses,
                    api_res.display_name,
                    api_res.school_name,
                );
            }
            return Err(err);
        }
    };

    let _profile_guard = TempProfileGuard::create()?;
    let port = find_free_port()?;

    let launch_msg = if is_cookie_mode {
        format!("正在使用本机 {browser_name} 校验已保存的登录状态...")
    } else {
        format!("正在启动本机 {browser_name} 进行登录...")
    };
    emit_status(app, "launching", &launch_msg, false);

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
        session_id,
    )?;

    let result = (|| -> Result<SessionResult, String> {
        let ws_url = wait_for_cdp_ws_url(port, &mut child, &cancel_flag)?;
        let mut cdp = CdpClient::connect(&ws_url)?;
        let _ = cdp.send("Page.enable", json!({}))?;
        let _ = cdp.send("Network.enable", json!({}))?;
        let _ = cdp.send("Runtime.enable", json!({}))?;

        if let Some(ref existing_cookies) = saved_cookies {
            cdp.set_cookies(existing_cookies)?;
            let _ = cdp.send("Page.navigate", json!({ "url": LOGIN_ENTRY_URL }))?;
            let check_deadline = Instant::now() + Duration::from_secs(15);
            let mut landed = false;
            let mut landed_at: Option<Instant> = None;
            while Instant::now() < check_deadline {
                if cancel_flag.load(Ordering::SeqCst) {
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
            let ready_deadline = Instant::now() + Duration::from_secs(25);
            let mut legacy_seen_since: Option<Instant> = None;
            while Instant::now() < ready_deadline {
                if cancel_flag.load(Ordering::SeqCst) {
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
                    if cancel_flag.load(Ordering::SeqCst) {
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

                    let url_now = state.get("url").and_then(|v| v.as_str()).unwrap_or("");
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

        let (browser_courses, mut detected_name, mut detected_school) =
            discover_courses_in_current_session(&mut cdp, app, backend, &cancel_flag)?;

        let mut cookies = cdp.get_zhihuishu_cookies()?;
        if cookies.is_empty() {
            return Err("登录成功但未获取到智慧树 Cookie，请重试".to_string());
        }

        let mut api_courses = Vec::new();
        let api_result =
            fetch_courses_via_backend_api(app, backend, &cookies, &effective_proxy);
        if cancel_flag.load(Ordering::SeqCst) {
            return Err("已取消操作".to_string());
        }
        if let Ok(api_res) = api_result {
            cookies = merge_cookies(&cookies, &api_res.cookies);
            api_courses = api_res.courses;
            if !api_res.display_name.is_empty() {
                detected_name = api_res.display_name;
            }
            if !api_res.school_name.is_empty() {
                detected_school = api_res.school_name;
            }
        }
        let discovered_courses = merge_backend_courses(api_courses, browser_courses);

        finalize_session_result(
            app,
            base_dir,
            &req,
            is_cookie_mode,
            Some((driver.clone(), browser_exe.clone())),
            &cookies,
            discovered_courses,
            detected_name,
            detected_school,
        )
    })();

    manager.finish_with_child(session_id, Some(&mut child));
    if child.try_wait().ok().flatten().is_none() {
        let _ = child.kill();
        let _ = child.wait();
    }
    if result.is_err() {
        if let Some((api_res, merged_cookies)) = valid_api_fallback {
            return finalize_session_result(
                app,
                base_dir,
                &req,
                true,
                Some((driver, browser_exe)),
                &merged_cookies,
                api_res.courses,
                api_res.display_name,
                api_res.school_name,
            );
        }
    }
    result
}

#[cfg(test)]
mod tests {
    use super::cdp::decode_base64_utf8;
    use super::cookies::normalize_cdp_cookies;
    use super::*;
    use std::fs;
    use std::time::{SystemTime, UNIX_EPOCH};

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
    fn merge_backend_courses_prefers_real_title_and_richer_metadata() {
        let primary = vec![CourseItem {
            id: "1".into(),
            title: "智慧共享课".into(),
            url: "https://studyvideoh5.zhihuishu.com/stuStudy?recruitAndCourseId=abc12345".into(),
            enabled: true,
            teacher: "".into(),
            school: "".into(),
            progress: "".into(),
            course_type: "智慧共享课".into(),
            auto_detected: true,
        }];
        let secondary = vec![CourseItem {
            id: "2".into(),
            title: "数据结构与算法".into(),
            url: "https://studyvideoh5.zhihuishu.com/stuStudy?recruitAndCourseId=abc12345".into(),
            enabled: true,
            teacher: "刘教授".into(),
            school: "示范大学".into(),
            progress: "88%".into(),
            course_type: "智慧共享课".into(),
            auto_detected: true,
        }];
        let merged = merge_backend_courses(primary, secondary);
        assert_eq!(merged.len(), 1);
        assert_eq!(merged[0].title, "数据结构与算法");
        assert_eq!(merged[0].teacher, "刘教授");
        assert_eq!(merged[0].school, "示范大学");
        assert_eq!(merged[0].progress, "88%");
    }

    #[test]
    fn decodes_base64_utf8_response_body() {
        let encoded = "eyJjb3Vyc2VOYW1lIjoi6auY562J5pWw5a2mIn0=";
        let decoded = decode_base64_utf8(encoded).expect("valid utf8 base64");
        assert_eq!(decoded, r#"{"courseName":"高等数学"}"#);
    }

    #[test]
    fn parses_backend_courses_response_and_merges_cookies() {
        let resp = json!({
            "ok": true,
            "sessionValid": true,
            "displayName": "王小明",
            "schoolName": "哈尔滨工程大学",
            "courses": [
                {
                    "title": "大学物理思维导引（下）",
                    "url": "https://studyvideoh5.zhihuishu.com/stuStudy?recruitAndCourseId=abc123",
                    "courseType": "智慧共享课",
                    "teacher": "姜海丽",
                    "school": "哈尔滨工程大学",
                    "progress": "95.5%"
                },
                {
                    "title": "劳动教育",
                    "url": "https://hike.zhihuishu.com/aidedteaching/sourceLearning/sourceLearning?courseId=11497949",
                    "courseType": "翻转 / 智慧课",
                    "teacher": "曾富珍",
                    "progress": "0%"
                }
            ],
            "cookies": [
                {
                    "name": "SESSION",
                    "value": "new-online-sess",
                    "domain": "onlineservice-api.zhihuishu.com",
                    "path": "/"
                }
            ]
        });
        let parsed = parse_backend_courses_response(&resp);
        assert!(parsed.session_valid);
        assert_eq!(parsed.display_name, "王小明");
        assert_eq!(parsed.school_name, "哈尔滨工程大学");
        assert_eq!(parsed.courses.len(), 2);
        assert_eq!(parsed.courses[0].course_type, "智慧共享课");
        assert_eq!(parsed.courses[1].course_type, "翻转 / 智慧课");

        let base = vec![
            PlaywrightCookie {
                name: "SESSION".into(),
                value: "old-online-sess".into(),
                domain: "onlineservice-api.zhihuishu.com".into(),
                path: "/".into(),
                expires: None,
                http_only: true,
                secure: true,
                same_site: None,
            },
            PlaywrightCookie {
                name: "SESSION".into(),
                value: "hike-sess".into(),
                domain: "hikeservice.zhihuishu.com".into(),
                path: "/".into(),
                expires: None,
                http_only: true,
                secure: true,
                same_site: None,
            },
        ];
        let merged = merge_cookies(&base, &parsed.cookies);
        assert_eq!(merged.len(), 2);
        assert_eq!(merged[0].value, "new-online-sess");
        assert!(merged[0].http_only);
        assert!(merged[0].secure);
        assert_eq!(merged[1].value, "hike-sess");
    }

    #[test]
    fn merge_cookies_preserves_metadata_without_downgrade() {
        let base = vec![
            PlaywrightCookie {
                name: "CASTGC".into(),
                value: "tgt-1".into(),
                domain: ".zhihuishu.com".into(),
                path: "/".into(),
                expires: Some(1893456000.0),
                http_only: true,
                secure: true,
                same_site: Some("Lax".into()),
            },
            PlaywrightCookie {
                name: "SESSION".into(),
                value: "old-sess".into(),
                domain: "onlineservice-api.zhihuishu.com".into(),
                path: "/".into(),
                expires: Some(1893456500.0),
                http_only: true,
                secure: true,
                same_site: Some("None".into()),
            },
        ];
        let updates = vec![
            PlaywrightCookie {
                name: "CASTGC".into(),
                value: "tgt-1".into(),
                domain: ".zhihuishu.com".into(),
                path: "/".into(),
                expires: None,
                http_only: false,
                secure: false,
                same_site: None,
            },
            PlaywrightCookie {
                name: "SESSION".into(),
                value: "new-sess".into(),
                domain: "onlineservice-api.zhihuishu.com".into(),
                path: "/".into(),
                expires: None,
                http_only: false,
                secure: false,
                same_site: None,
            },
        ];
        let merged = merge_cookies(&base, &updates);
        assert_eq!(merged.len(), 2);
        assert_eq!(merged[0].value, "tgt-1");
        assert_eq!(merged[0].expires, Some(1893456000.0));
        assert!(merged[0].http_only);
        assert!(merged[0].secure);
        assert_eq!(merged[0].same_site.as_deref(), Some("Lax"));

        assert_eq!(merged[1].value, "new-sess");
        assert_eq!(merged[1].expires, Some(1893456500.0));
        assert!(merged[1].http_only);
        assert!(merged[1].secure);
        assert_eq!(merged[1].same_site.as_deref(), Some("None"));
    }

    #[test]
    fn session_manager_isolates_cancellation_across_overlapping_attempts() {
        let mgr = ZhsSessionManager::new();
        let (_sid1, cancel1) = mgr.start_attempt();
        assert!(!cancel1.load(Ordering::SeqCst));

        let (sid2, cancel2) = mgr.start_attempt();
        assert!(cancel1.load(Ordering::SeqCst));
        assert!(!cancel2.load(Ordering::SeqCst));

        mgr.cancel();
        assert!(cancel2.load(Ordering::SeqCst));

        let (sid3, cancel3) = mgr.start_attempt();
        assert_ne!(sid2, sid3);
        assert!(cancel2.load(Ordering::SeqCst));
        assert!(!cancel3.load(Ordering::SeqCst));
    }

    #[test]
    fn session_manager_finish_ignores_stale_session_id() {
        let mgr = ZhsSessionManager::new();
        let job1 = Arc::new(Job::create().unwrap());
        let job2 = Arc::new(Job::create().unwrap());
        let sid1 = mgr.begin(job1);
        let sid2 = mgr.begin(job2);
        assert_ne!(sid1, sid2);

        mgr.finish(sid1);
        {
            let guard = lock_unpoisoned(&mgr.active_session);
            assert_eq!(guard.as_ref().map(|s| s.id), Some(sid2));
        }

        mgr.finish(sid2);
        {
            let guard = lock_unpoisoned(&mgr.active_session);
            assert!(guard.is_none());
        }
    }

    #[test]
    fn merge_and_save_session_config_errors_and_preserves_corrupt_config() {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let base_dir = std::env::temp_dir().join(format!("autovisor_t14_zhs_{nanos}"));
        fs::create_dir_all(&base_dir).unwrap();
        let config_path = base_dir.join("config.ini");
        let corrupt_bytes = b"[user-account]\nusername = keepme\n\0corrupt";
        fs::write(&config_path, corrupt_bytes).unwrap();

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
        let res = merge_and_save_session_config(&base_dir, &req, None, Vec::new());
        assert!(res.is_err());
        assert_eq!(fs::read(&config_path).unwrap(), corrupt_bytes);

        let _ = fs::remove_dir_all(&base_dir);
    }
}
