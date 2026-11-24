use crate::backend::BackendStatusSnapshot;
use crate::browser_detect::{
    detect_installed_browsers, refresh_installed_browsers, DetectedBrowser,
};
use crate::config_ini::{save_config_to_dir, AutovisorConfig};
use crate::window_close::CloseAction;
use serde::{Deserialize, Serialize};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock};
use std::time::{SystemTime, UNIX_EPOCH};

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
#[serde(rename_all = "camelCase")]
pub struct SessionProfile {
    pub logged_in: bool,
    pub display_name: String,
    pub account: String,
    pub login_mode: String, // "phone" | "student" | "qrcode" | "cookie"
    pub school_name: String,
    pub school_id: String,
    pub last_synced_at: Option<u64>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
#[serde(rename_all = "camelCase")]
pub struct GuiPrefs {
    pub close_behavior: Option<String>,
    #[serde(default)]
    pub session: Option<SessionProfile>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct RuntimeInfo {
    pub backend_phase: String, // starting | ready | stopping | stopped | failed
    pub backend_error: String,
    pub platform: String,      // windows | macos | linux
    pub base_dir: String,
    pub config_path: String,
    pub logs_dir: String,
    pub cookie_exists: bool,
    pub cookie_count: usize,
    pub cookie_modified_at: Option<u64>,
    pub version: String,
    pub backend_version: String,
    pub close_behavior: String,
    pub detected_browsers: Vec<DetectedBrowser>,
    pub session: Option<SessionProfile>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct LogFileItem {
    pub name: String,
    pub path: String,
    pub size_bytes: u64,
    pub size_label: String,
    pub modified_at: String,
    pub modified_epoch: u64,
}

#[derive(Debug, Clone)]
pub enum BackendLaunchSpec {
    FrozenExe(PathBuf),
    PythonScript { python: PathBuf, script: PathBuf },
}

#[derive(Debug, Clone)]
pub struct AppPaths {
    pub data_dir: PathBuf,
    pub resources_dir: PathBuf,
    pub backend_launch: Option<BackendLaunchSpec>,
    pub is_portable: bool,
}

pub fn current_platform() -> &'static str {
    #[cfg(windows)]
    {
        "windows"
    }
    #[cfg(target_os = "macos")]
    {
        "macos"
    }
    #[cfg(all(unix, not(target_os = "macos")))]
    {
        "linux"
    }
    #[cfg(not(any(windows, unix)))]
    {
        "unknown"
    }
}

fn backend_binary_name() -> &'static str {
    #[cfg(windows)]
    {
        "autovisor-backend.exe"
    }
    #[cfg(not(windows))]
    {
        "autovisor-backend"
    }
}

pub fn appdata_dir() -> PathBuf {
    #[cfg(windows)]
    {
        if let Ok(appdata) = std::env::var("APPDATA") {
            if !appdata.trim().is_empty() {
                let dir = PathBuf::from(appdata).join("com.autovisor.gui");
                let _ = fs::create_dir_all(&dir);
                return dir;
            }
        }
    }
    #[cfg(target_os = "macos")]
    {
        if let Ok(home) = std::env::var("HOME") {
            if !home.trim().is_empty() {
                let dir = PathBuf::from(home)
                    .join("Library")
                    .join("Application Support")
                    .join("com.autovisor.gui");
                let _ = fs::create_dir_all(&dir);
                return dir;
            }
        }
    }
    #[cfg(all(unix, not(target_os = "macos")))]
    {
        if let Ok(xdg) = std::env::var("XDG_CONFIG_HOME") {
            if !xdg.trim().is_empty() {
                let dir = PathBuf::from(xdg).join("com.autovisor.gui");
                let _ = fs::create_dir_all(&dir);
                return dir;
            }
        }
        if let Ok(home) = std::env::var("HOME") {
            if !home.trim().is_empty() {
                let dir = PathBuf::from(home)
                    .join(".config")
                    .join("com.autovisor.gui");
                let _ = fs::create_dir_all(&dir);
                return dir;
            }
        }
    }
    std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(Path::to_path_buf))
        .unwrap_or_else(|| PathBuf::from("."))
}

fn prefs_lock() -> &'static Mutex<()> {
    static LOCK: OnceLock<Mutex<()>> = OnceLock::new();
    LOCK.get_or_init(|| Mutex::new(()))
}

pub fn prefs_path_in_data_dir(data_dir: &Path) -> PathBuf {
    data_dir.join("data").join("gui_prefs.json")
}

pub fn prefs_path() -> PathBuf {
    prefs_path_in_data_dir(&resolve_app_paths().data_dir)
}

fn legacy_prefs_path_for(paths: &AppPaths, appdata: &Path) -> Option<PathBuf> {
    if paths.is_portable || paths.data_dir != appdata.join("userdata") {
        return None;
    }
    Some(appdata.join("gui_prefs.json"))
}

pub fn load_prefs_from_path(path: &Path) -> GuiPrefs {
    fs::read_to_string(path)
        .ok()
        .and_then(|raw| serde_json::from_str::<GuiPrefs>(&raw).ok())
        .unwrap_or_default()
}

pub fn save_prefs_to_path(path: &Path, prefs: &GuiPrefs) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|e| format!("创建配置目录失败: {e}"))?;
    }
    let text = serde_json::to_string_pretty(prefs).map_err(|e| format!("序列化偏好失败: {e}"))?;
    let tmp_path = path.with_extension("json.tmp");
    fs::write(&tmp_path, &text).map_err(|e| format!("写入偏好失败: {e}"))?;
    if fs::rename(&tmp_path, path).is_err() {
        let _ = fs::remove_file(&tmp_path);
        fs::write(path, &text).map_err(|e| format!("写入偏好失败: {e}"))?;
    }
    Ok(())
}

pub fn load_prefs_with_legacy(path: &Path, legacy_path: Option<&Path>) -> GuiPrefs {
    if path.is_file() {
        return load_prefs_from_path(path);
    }
    if let Some(legacy) = legacy_path {
        if legacy != path && legacy.is_file() {
            let migrated = load_prefs_from_path(legacy);
            let _ = save_prefs_to_path(path, &migrated);
            return migrated;
        }
    }
    GuiPrefs::default()
}

pub fn load_prefs() -> GuiPrefs {
    let _guard = prefs_lock().lock().unwrap_or_else(|e| e.into_inner());
    let paths = resolve_app_paths();
    let path = prefs_path_in_data_dir(&paths.data_dir);
    let legacy = legacy_prefs_path_for(&paths, &appdata_dir());
    load_prefs_with_legacy(&path, legacy.as_deref())
}

pub fn save_prefs(prefs: &GuiPrefs) -> Result<(), String> {
    let _guard = prefs_lock().lock().unwrap_or_else(|e| e.into_inner());
    save_prefs_to_path(&prefs_path(), prefs)
}

pub fn resolve_close_action(prefs: &GuiPrefs) -> CloseAction {
    prefs
        .close_behavior
        .as_deref()
        .and_then(CloseAction::parse)
        .unwrap_or(CloseAction::Ask)
}

pub fn load_close_action() -> CloseAction {
    resolve_close_action(&load_prefs())
}

pub fn save_close_action(action: CloseAction) -> Result<(), String> {
    let _guard = prefs_lock().lock().unwrap_or_else(|e| e.into_inner());
    let paths = resolve_app_paths();
    let path = prefs_path_in_data_dir(&paths.data_dir);
    let legacy = legacy_prefs_path_for(&paths, &appdata_dir());
    let mut prefs = load_prefs_with_legacy(&path, legacy.as_deref());
    prefs.close_behavior = Some(action.as_str().to_string());
    save_prefs_to_path(&path, &prefs)
}

fn dir_is_writable(dir: &Path) -> bool {
    if fs::create_dir_all(dir).is_err() {
        return false;
    }
    let probe = dir.join(".autovisor_write_test");
    if fs::write(&probe, b"ok").is_ok() {
        let _ = fs::remove_file(&probe);
        true
    } else {
        false
    }
}

fn find_frozen_backend(exe_dir: &Path) -> Option<(PathBuf, PathBuf)> {
    let bin = backend_binary_name();
    let mut candidates = vec![
        exe_dir.join("runtime").join(bin),
        exe_dir.join("resources").join("runtime").join(bin),
        exe_dir.join("backend").join(bin),
        exe_dir.join(bin),
    ];
    // macOS .app bundle (Contents/MacOS/<exe> -> Contents/Resources/runtime/autovisor-backend)
    // and Linux .deb / system install (/usr/bin/<exe> -> /usr/lib/<app>/runtime/autovisor-backend)
    if let Some(parent) = exe_dir.parent() {
        candidates.push(parent.join("Resources").join("runtime").join(bin));
        candidates.push(parent.join("Resources").join("resources").join("runtime").join(bin));
        for lib_subdir in ["Autovisor GUI", "autovisor-gui"] {
            candidates.push(parent.join("lib").join(lib_subdir).join("runtime").join(bin));
            candidates.push(
                parent
                    .join("lib")
                    .join(lib_subdir)
                    .join("resources")
                    .join("runtime")
                    .join(bin),
            );
        }
    }
    for exe in candidates {
        if exe.is_file() {
            let parent = exe.parent().unwrap_or(exe_dir);
            let res = if parent.join("resources").is_dir() {
                parent.join("resources")
            } else {
                parent.to_path_buf()
            };
            return Some((exe, res));
        }
    }
    None
}

fn find_dev_backend(start: &Path) -> Option<(PathBuf, PathBuf, PathBuf)> {
    let mut cursor = Some(start);
    for _ in 0..8 {
        if let Some(dir) = cursor {
            let direct = dir.join("backend_server.py");
            let nested = dir.join("Autovisor").join("backend_server.py");
            let repo_root = if direct.is_file() {
                Some(dir.to_path_buf())
            } else if nested.is_file() {
                Some(dir.join("Autovisor"))
            } else {
                None
            };
            if let Some(root) = repo_root {
                #[cfg(windows)]
                let python = {
                    let venv_py = root.join(".venv").join("Scripts").join("python.exe");
                    if venv_py.is_file() {
                        venv_py
                    } else {
                        PathBuf::from("python")
                    }
                };
                #[cfg(not(windows))]
                let python = {
                    let venv_py = root.join(".venv").join("bin").join("python");
                    let venv_py3 = root.join(".venv").join("bin").join("python3");
                    if venv_py.is_file() {
                        venv_py
                    } else if venv_py3.is_file() {
                        venv_py3
                    } else {
                        PathBuf::from("python3")
                    }
                };
                let res = root.join("resources");
                return Some((root, res, python));
            }
            cursor = dir.parent();
        }
    }
    None
}

const DEFAULT_MIRRORS_JSON: &str = r#"{
  "Huawei": "https://mirrors.huaweicloud.com/repository/pypi",
  "Aliyun": "https://mirrors.aliyun.com/pypi",
  "Tsinghua": "https://pypi.tuna.tsinghua.edu.cn",
  "Official": "https://pypi.org"
}
"#;

fn ensure_data_dir_initialized(data_dir: &Path, resources_dir: Option<&Path>) {
    let data_subdir = data_dir.join("data");
    let _ = fs::create_dir_all(&data_subdir);
    let _ = fs::create_dir_all(data_dir.join("logs"));
    let mirrors_path = data_subdir.join("mirrors.json");
    if !mirrors_path.is_file() {
        let copied = resources_dir
            .map(|dir| dir.join("mirrors.json"))
            .filter(|src| src.is_file())
            .and_then(|src| fs::copy(src, &mirrors_path).ok())
            .is_some();
        if !copied {
            let _ = fs::write(&mirrors_path, DEFAULT_MIRRORS_JSON);
        }
    }
    let cfg_path = data_dir.join("config.ini");
    if !cfg_path.is_file() {
        let mut default_cfg = AutovisorConfig::default();
        if let Ok(raw) = fs::read_to_string(&mirrors_path) {
            if let Ok(parsed) = serde_json::from_str::<std::collections::BTreeMap<String, String>>(
                raw.trim_start_matches('\u{feff}'),
            ) {
                if !parsed.is_empty() {
                    default_cfg.mirrors = parsed;
                }
            }
        }
        let _ = save_config_to_dir(data_dir, &default_cfg);
    }
}

fn resolve_frozen_data_dir(exe_dir: &Path) -> (PathBuf, bool) {
    let portable_marker = exe_dir.join(".portable");
    let portable_data = exe_dir.join("userdata");
    if portable_marker.is_file() && dir_is_writable(&portable_data) {
        (portable_data, true)
    } else {
        (appdata_dir().join("userdata"), false)
    }
}

static APP_PATHS_CACHE: OnceLock<AppPaths> = OnceLock::new();

pub fn resolve_app_paths() -> AppPaths {
    let paths = APP_PATHS_CACHE
        .get_or_init(resolve_app_paths_uncached)
        .clone();
    if !paths.data_dir.is_dir() {
        let _ = fs::create_dir_all(&paths.data_dir);
    }
    paths
}

fn resolve_app_paths_uncached() -> AppPaths {
    // 1. 生产/安装模式：优先查找内置后端；仅存在 .portable 标记文件且可写时使用便携目录
    if let Ok(exe) = std::env::current_exe() {
        if let Some(exe_dir) = exe.parent() {
            if let Some((backend_exe, resources_dir)) = find_frozen_backend(exe_dir) {
                let (data_dir, is_portable) = resolve_frozen_data_dir(exe_dir);
                ensure_data_dir_initialized(&data_dir, Some(&resources_dir));
                return AppPaths {
                    data_dir,
                    resources_dir,
                    backend_launch: Some(BackendLaunchSpec::FrozenExe(backend_exe)),
                    is_portable,
                };
            }

            // 2. 开发模式：从 exe 目录向上查找仓库 backend_server.py
            if let Some((repo_root, resources_dir, python)) = find_dev_backend(exe_dir) {
                let script = repo_root.join("backend_server.py");
                ensure_data_dir_initialized(&repo_root, Some(&resources_dir));
                return AppPaths {
                    data_dir: repo_root,
                    resources_dir,
                    backend_launch: Some(BackendLaunchSpec::PythonScript { python, script }),
                    is_portable: false,
                };
            }
        }
    }

    // 3. 开发模式兜底：从当前工作目录向上查找
    if let Ok(cwd) = std::env::current_dir() {
        if let Some((repo_root, resources_dir, python)) = find_dev_backend(&cwd) {
            let script = repo_root.join("backend_server.py");
            ensure_data_dir_initialized(&repo_root, Some(&resources_dir));
            return AppPaths {
                data_dir: repo_root,
                resources_dir,
                backend_launch: Some(BackendLaunchSpec::PythonScript { python, script }),
                is_portable: false,
            };
        }
    }

    // 4. 缺失内置后端组件时仍保证 data_dir 可用，避免配置/登录逻辑崩溃
    let fallback_data = appdata_dir().join("userdata");
    ensure_data_dir_initialized(&fallback_data, None);
    AppPaths {
        data_dir: fallback_data.clone(),
        resources_dir: fallback_data,
        backend_launch: None,
        is_portable: false,
    }
}

pub fn inspect_cookies(base_dir: &Path) -> (bool, usize, Option<u64>) {
    let cookie_file = base_dir.join("data").join("cookies.json");
    if !cookie_file.is_file() {
        return (false, 0, None);
    }
    let modified = fs::metadata(&cookie_file)
        .ok()
        .and_then(|m| m.modified().ok())
        .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
        .map(|d| d.as_secs());
    let count = fs::read_to_string(&cookie_file)
        .ok()
        .and_then(|raw| serde_json::from_str::<serde_json::Value>(&raw).ok())
        .and_then(|v| v.as_array().map(|arr| arr.len()))
        .unwrap_or(0);
    (count > 0, count, modified)
}

pub fn get_runtime_info(backend_status: &BackendStatusSnapshot) -> RuntimeInfo {
    get_runtime_info_with_refresh(backend_status, false)
}

pub fn get_runtime_info_with_refresh(
    backend_status: &BackendStatusSnapshot,
    refresh_browsers: bool,
) -> RuntimeInfo {
    let prefs = load_prefs();
    let detected_browsers = if refresh_browsers {
        refresh_installed_browsers()
    } else {
        detect_installed_browsers()
    };
    let close_behavior = resolve_close_action(&prefs).as_str().to_string();

    let paths = resolve_app_paths();
    let base_dir = paths.data_dir;
    let config_path = base_dir.join("config.ini");
    let logs_dir = base_dir.join("logs");
    let (cookie_exists, cookie_count, cookie_modified_at) = inspect_cookies(&base_dir);
    let session = prefs.session.clone().map(|mut s| {
        if !cookie_exists {
            s.logged_in = false;
        }
        s
    });

    RuntimeInfo {
        backend_phase: backend_status.phase.clone(),
        backend_error: backend_status.error.clone(),
        platform: current_platform().to_string(),
        base_dir: base_dir.to_string_lossy().to_string(),
        config_path: config_path.to_string_lossy().to_string(),
        logs_dir: logs_dir.to_string_lossy().to_string(),
        cookie_exists,
        cookie_count,
        cookie_modified_at,
        version: backend_status.version.clone(),
        backend_version: backend_status.backend_version.clone(),
        close_behavior,
        detected_browsers,
        session,
    }
}

fn format_size(bytes: u64) -> String {
    if bytes < 1024 {
        format!("{bytes} B")
    } else if bytes < 1024 * 1024 {
        format!("{:.1} KB", bytes as f64 / 1024.0)
    } else {
        format!("{:.1} MB", bytes as f64 / (1024.0 * 1024.0))
    }
}

fn format_system_time(st: SystemTime) -> (u64, String) {
    let secs = st
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let local_secs = secs + 8 * 3600;
    let days = local_secs / 86400;
    let rem = local_secs % 86400;
    let h = rem / 3600;
    let m = (rem % 3600) / 60;
    let (year, month, day) = civil_from_days(days as i64);
    (secs, format!("{year:04}-{month:02}-{day:02} {h:02}:{m:02}"))
}

fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719468;
    let era = if z >= 0 { z } else { z - 146096 } / 146097;
    let doe = (z - era * 146097) as u64;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = if m <= 2 { y + 1 } else { y };
    (y, m as u32, d as u32)
}

pub fn list_log_files(base_dir: &Path) -> Vec<LogFileItem> {
    let logs_dir = base_dir.join("logs");
    let Ok(entries) = fs::read_dir(logs_dir) else {
        return Vec::new();
    };
    let mut items: Vec<LogFileItem> = entries
        .flatten()
        .filter_map(|entry| {
            let path = entry.path();
            if !path.is_file() {
                return None;
            }
            let name = entry.file_name().to_string_lossy().to_string();
            let meta = entry.metadata().ok()?;
            let size_bytes = meta.len();
            let (modified_epoch, modified_at) = meta
                .modified()
                .map(format_system_time)
                .unwrap_or((0, "--".to_string()));
            Some(LogFileItem {
                name,
                path: path.to_string_lossy().to_string(),
                size_bytes,
                size_label: format_size(size_bytes),
                modified_at,
                modified_epoch,
            })
        })
        .collect();
    items.sort_by(|a, b| b.modified_epoch.cmp(&a.modified_epoch).then_with(|| b.name.cmp(&a.name)));
    items
}

pub fn read_log_tail(path: &Path, max_bytes: u64) -> Result<String, String> {
    use std::io::{Read, Seek, SeekFrom};
    let mut file = fs::File::open(path).map_err(|e| format!("无法打开日志文件: {e}"))?;
    let len = file
        .metadata()
        .map_err(|e| format!("读取文件信息失败: {e}"))?
        .len();
    let mut prefix = String::new();
    if len > max_bytes {
        file.seek(SeekFrom::Start(len - max_bytes))
            .map_err(|e| format!("定位日志失败: {e}"))?;
        prefix = format!(
            "...（文件共 {}，仅显示末尾 {}）...\n\n",
            format_size(len),
            format_size(max_bytes)
        );
    }
    let mut buf = Vec::new();
    file.read_to_end(&mut buf)
        .map_err(|e| format!("读取日志失败: {e}"))?;
    let content = String::from_utf8_lossy(&buf);
    Ok(format!("{prefix}{content}"))
}

pub fn read_log_tail_scoped(
    base_dir: &Path,
    requested_path: &Path,
    max_bytes: u64,
) -> Result<String, String> {
    let logs_dir = base_dir.join("logs");
    let canonical_logs_dir =
        fs::canonicalize(&logs_dir).map_err(|_| "仅允许读取日志目录下的文件".to_string())?;
    let canonical_file =
        fs::canonicalize(requested_path).map_err(|_| "仅允许读取日志目录下的文件".to_string())?;
    if !canonical_file.starts_with(&canonical_logs_dir) || !canonical_file.is_file() {
        return Err("仅允许读取日志目录下的文件".to_string());
    }
    read_log_tail(&canonical_file, max_bytes)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_temp_dir(tag: &str) -> PathBuf {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        let dir = std::env::temp_dir().join(format!(
            "autovisor-runtime-test-{}-{}-{nanos}",
            tag,
            std::process::id()
        ));
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn finds_frozen_backend_in_runtime_and_resources_runtime() {
        let root = make_temp_dir("find");
        let bin = backend_binary_name();
        let res_runtime = root.join("resources").join("runtime");
        fs::create_dir_all(res_runtime.join("resources")).unwrap();
        let backend_exe = res_runtime.join(bin);
        fs::write(&backend_exe, b"stub").unwrap();

        let (found_exe, found_res) = find_frozen_backend(&root).expect("should find resources/runtime");
        assert_eq!(found_exe, backend_exe);
        assert_eq!(found_res, res_runtime.join("resources"));

        let direct_runtime = root.join("runtime");
        fs::create_dir_all(&direct_runtime).unwrap();
        let direct_exe = direct_runtime.join(bin);
        fs::write(&direct_exe, b"stub").unwrap();
        let (found_direct, _) = find_frozen_backend(&root).expect("should prefer runtime/");
        assert_eq!(found_direct, direct_exe);

        // Linux .deb layout: /usr/bin -> /usr/lib/Autovisor GUI/runtime/<bin>
        let usr_bin = root.join("usr").join("bin");
        let usr_lib_rt = root.join("usr").join("lib").join("Autovisor GUI").join("runtime");
        fs::create_dir_all(&usr_bin).unwrap();
        fs::create_dir_all(usr_lib_rt.join("resources")).unwrap();
        let deb_exe = usr_lib_rt.join(bin);
        fs::write(&deb_exe, b"stub").unwrap();
        let (found_deb, found_deb_res) =
            find_frozen_backend(&usr_bin).expect("should find Linux .deb runtime");
        assert_eq!(found_deb, deb_exe);
        assert_eq!(found_deb_res, usr_lib_rt.join("resources"));

        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn uses_portable_userdata_only_when_marker_exists() {
        let root = make_temp_dir("portable");
        let (default_dir, default_portable) = resolve_frozen_data_dir(&root);
        assert_eq!(default_dir, appdata_dir().join("userdata"));
        assert!(!default_portable);

        fs::write(root.join(".portable"), b"").unwrap();
        let (portable_dir, is_portable) = resolve_frozen_data_dir(&root);
        assert_eq!(portable_dir, root.join("userdata"));
        assert!(is_portable);

        // Even if .portable is removed mid-run, cached AppPaths keeps portable isolation.
        let cached_paths = AppPaths {
            data_dir: portable_dir.clone(),
            resources_dir: root.clone(),
            backend_launch: None,
            is_portable,
        };
        let _ = fs::remove_file(root.join(".portable"));
        assert_eq!(legacy_prefs_path_for(&cached_paths, &appdata_dir()), None);

        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn legacy_prefs_path_only_applies_to_installed_appdata_userdata() {
        let root = make_temp_dir("legacy_scope");
        let appdata = root.join("appdata");

        let installed = AppPaths {
            data_dir: appdata.join("userdata"),
            resources_dir: root.join("res"),
            backend_launch: None,
            is_portable: false,
        };
        assert_eq!(
            legacy_prefs_path_for(&installed, &appdata),
            Some(appdata.join("gui_prefs.json"))
        );

        let portable = AppPaths {
            data_dir: root.join("portable_app").join("userdata"),
            resources_dir: root.join("portable_app"),
            backend_launch: None,
            is_portable: true,
        };
        assert_eq!(legacy_prefs_path_for(&portable, &appdata), None);

        // Dev mode (repo_root != appdata/userdata) must not pull installed AppData prefs either.
        let dev_repo = AppPaths {
            data_dir: root.join("repo"),
            resources_dir: root.join("repo").join("resources"),
            backend_launch: None,
            is_portable: false,
        };
        assert_eq!(legacy_prefs_path_for(&dev_repo, &appdata), None);

        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn initializes_mirrors_json_from_resources_or_default() {
        let root = make_temp_dir("mirrors");
        let res = root.join("res");
        let data1 = root.join("data1");
        fs::create_dir_all(&res).unwrap();
        fs::write(res.join("mirrors.json"), r#"{"Custom": "https://example.com"}"#).unwrap();

        ensure_data_dir_initialized(&data1, Some(&res));
        let copied = fs::read_to_string(data1.join("data").join("mirrors.json")).unwrap();
        assert!(copied.contains("https://example.com"));

        let data2 = root.join("data2");
        ensure_data_dir_initialized(&data2, None);
        let fallback = fs::read_to_string(data2.join("data").join("mirrors.json")).unwrap();
        assert!(fallback.contains("Huawei"));

        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn close_behavior_prefs_default_to_ask_and_round_trip() {
        let root = make_temp_dir("prefs");
        let path = prefs_path_in_data_dir(&root);

        assert_eq!(resolve_close_action(&load_prefs_from_path(&path)), CloseAction::Ask);

        for action in [
            CloseAction::Minimize,
            CloseAction::Tray,
            CloseAction::Exit,
            CloseAction::Ask,
        ] {
            let prefs = GuiPrefs {
                close_behavior: Some(action.as_str().to_string()),
                session: None,
            };
            save_prefs_to_path(&path, &prefs).unwrap();
            let loaded = load_prefs_from_path(&path);
            assert_eq!(resolve_close_action(&loaded), action);
        }

        fs::write(&path, r#"{"closeBehavior":"invalid-mode"}"#).unwrap();
        assert_eq!(resolve_close_action(&load_prefs_from_path(&path)), CloseAction::Ask);

        // Legacy migration test: migrates from legacy appdata path when new path doesn't exist
        let _ = fs::remove_file(&path);
        let legacy_path = root.join("legacy_gui_prefs.json");
        let legacy_prefs = GuiPrefs {
            close_behavior: Some(CloseAction::Tray.as_str().to_string()),
            session: None,
        };
        save_prefs_to_path(&legacy_path, &legacy_prefs).unwrap();

        // Portable mode (legacy = None) does NOT migrate from legacy path
        let portable_loaded = load_prefs_with_legacy(&path, None);
        assert_eq!(resolve_close_action(&portable_loaded), CloseAction::Ask);
        assert!(!path.is_file());

        // Non-portable mode (legacy = Some) migrates and writes new path
        let migrated = load_prefs_with_legacy(&path, Some(&legacy_path));
        assert_eq!(resolve_close_action(&migrated), CloseAction::Tray);
        assert!(path.is_file());

        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn read_log_tail_scoped_rejects_paths_outside_logs_dir() {
        let root = make_temp_dir("scoped_log");
        let logs_dir = root.join("logs");
        fs::create_dir_all(&logs_dir).unwrap();
        let valid_log = logs_dir.join("Log1.txt");
        fs::write(&valid_log, "hello log line").unwrap();
        let outside_file = root.join("config.ini");
        fs::write(&outside_file, "[user-account]\npassword = secret\n").unwrap();

        let content = read_log_tail_scoped(&root, &valid_log, 1024).unwrap();
        assert_eq!(content, "hello log line");

        let traversal = logs_dir.join("..").join("config.ini");
        assert!(read_log_tail_scoped(&root, &traversal, 1024).is_err());
        assert!(read_log_tail_scoped(&root, &outside_file, 1024).is_err());
        assert!(read_log_tail_scoped(&root, &logs_dir, 1024).is_err());

        let _ = fs::remove_dir_all(&root);
    }
}
