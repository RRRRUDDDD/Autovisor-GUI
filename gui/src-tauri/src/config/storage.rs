use super::ini::{parse_ini_str, render_ini_str};
use super::llm::LlmConfig;
use super::model::{infer_title, is_https_url, AutovisorConfig, CourseItem};
use crate::browser_detect::{detect_installed_browsers, resolve_browser_choice};
use std::collections::{BTreeMap, HashSet};
use std::fs;
use std::io::Write;
use std::path::Path;
use std::sync::{Mutex, OnceLock};

fn atomic_io_lock() -> &'static Mutex<()> {
    static LOCK: OnceLock<Mutex<()>> = OnceLock::new();
    LOCK.get_or_init(|| Mutex::new(()))
}

pub fn write_file_atomic(target: &Path, content: &[u8], label: &str) -> Result<(), String> {
    let _guard = atomic_io_lock().lock().unwrap_or_else(|e| e.into_inner());
    let parent = target
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."));
    fs::create_dir_all(parent).map_err(|e| format!("创建 {label} 目录失败: {e}"))?;

    let mut tmp = tempfile::Builder::new()
        .prefix(".tmp-autovisor-")
        .tempfile_in(parent)
        .map_err(|e| format!("创建 {label} 临时文件失败: {e}"))?;

    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = tmp
            .as_file()
            .set_permissions(fs::Permissions::from_mode(0o600));
    }

    tmp.write_all(content)
        .and_then(|_| tmp.as_file().sync_all())
        .map_err(|e| format!("写入 {label} 失败: {e}"))?;

    tmp.persist(target)
        .map_err(|e| format!("保存 {label} 失败: {}", e.error))?;
    Ok(())
}

pub fn load_config_from_dir(base_dir: &Path) -> Result<AutovisorConfig, String> {
    let config_path = base_dir.join("config.ini");
    let (mut cfg, ini_urls) = match fs::read(&config_path) {
        Ok(bytes) => {
            if bytes.contains(&0) {
                return Err("config.ini 文件已损坏（包含非法空字节）".to_string());
            }
            let text = String::from_utf8_lossy(&bytes).to_string();
            parse_ini_str(&text)
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            (AutovisorConfig::default(), Vec::new())
        }
        Err(e) => return Err(format!("读取 config.ini 失败: {e}")),
    };

    // Merge companion metadata (gui_courses.json) to preserve custom titles and unchecked courses
    let meta_path = base_dir.join("data").join("gui_courses.json");
    let saved_courses: Vec<CourseItem> = match fs::read_to_string(&meta_path) {
        Ok(raw) => serde_json::from_str::<Vec<CourseItem>>(raw.trim_start_matches('\u{feff}'))
            .map_err(|e| format!("解析 gui_courses.json 失败: {e}"))?,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Vec::new(),
        Err(e) => return Err(format!("读取 gui_courses.json 失败: {e}")),
    };

    let ini_set: HashSet<String> = ini_urls.iter().cloned().collect();
    let mut merged: Vec<CourseItem> = Vec::new();
    let mut seen: HashSet<String> = HashSet::new();

    for mut item in saved_courses {
        let trimmed = item.url.trim().to_string();
        if !is_https_url(&trimmed) || seen.contains(&trimmed) {
            continue;
        }
        seen.insert(trimmed.clone());
        item.url = trimmed.clone();
        if !ini_urls.is_empty() {
            item.enabled = ini_set.contains(&trimmed);
        }
        merged.push(item);
    }

    for url in ini_urls {
        if seen.insert(url.clone()) {
            let idx = merged.len() + 1;
            merged.push(CourseItem {
                id: format!("course-{idx}"),
                title: infer_title(&url, idx),
                url,
                enabled: true,
                teacher: String::new(),
                school: String::new(),
                progress: String::new(),
                course_type: String::new(),
                auto_detected: false,
            });
        }
    }
    cfg.courses = merged;

    // Auto-select an installed browser on this device when exe_path is empty or invalid
    let detected = detect_installed_browsers();
    if let Some((driver, exe_path)) =
        resolve_browser_choice(&cfg.driver, &cfg.exe_path, &detected)
    {
        cfg.driver = driver;
        cfg.exe_path = exe_path;
    }

    // Load mirrors.json
    let mirrors_path = base_dir.join("data").join("mirrors.json");
    match fs::read_to_string(&mirrors_path) {
        Ok(raw) => {
            let parsed = serde_json::from_str::<BTreeMap<String, String>>(
                raw.trim_start_matches('\u{feff}'),
            )
            .map_err(|e| format!("解析 mirrors.json 失败: {e}"))?;
            if !parsed.is_empty() {
                cfg.mirrors = parsed;
            }
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
        Err(e) => return Err(format!("读取 mirrors.json 失败: {e}")),
    }

    // Load data/llm.json (universal LLM quiz solver)
    let llm_path = base_dir.join("data").join("llm.json");
    match fs::read_to_string(&llm_path) {
        Ok(raw) => {
            let parsed =
                serde_json::from_str::<LlmConfig>(raw.trim_start_matches('\u{feff}'))
                    .map_err(|e| format!("解析 llm.json 失败: {e}"))?;
            cfg.llm = parsed.normalized();
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
        Err(e) => return Err(format!("读取 llm.json 失败: {e}")),
    }

    Ok(cfg)
}

pub fn save_config_to_dir(base_dir: &Path, cfg: &AutovisorConfig) -> Result<AutovisorConfig, String> {
    let config_path = base_dir.join("config.ini");
    let ini_text = render_ini_str(cfg);
    write_file_atomic(&config_path, ini_text.as_bytes(), "config.ini")?;

    let data_dir = base_dir.join("data");
    let _ = fs::create_dir_all(&data_dir);

    let meta_path = data_dir.join("gui_courses.json");
    let meta_json = serde_json::to_string_pretty(&cfg.courses)
        .map_err(|e| format!("序列化课程列表失败: {e}"))?;
    write_file_atomic(&meta_path, meta_json.as_bytes(), "gui_courses.json")?;

    if !cfg.mirrors.is_empty() {
        let mirrors_path = data_dir.join("mirrors.json");
        let mirrors_json = serde_json::to_string_pretty(&cfg.mirrors)
            .map_err(|e| format!("序列化镜像源失败: {e}"))?;
        write_file_atomic(&mirrors_path, mirrors_json.as_bytes(), "mirrors.json")?;
    }

    let llm_path = data_dir.join("llm.json");
    let llm_norm = cfg.llm.normalized();
    let llm_disk = llm_norm.to_disk_value();
    let llm_json = serde_json::to_string_pretty(&llm_disk)
        .map_err(|e| format!("序列化大模型配置失败: {e}"))?;
    let llm_text = format!("{llm_json}\n");
    write_file_atomic(&llm_path, llm_text.as_bytes(), "llm.json")?;

    // 在内存中构造规范化副本，避免每次保存都冗余重读磁盘和扫描本机浏览器
    let (mut normalized, _) = parse_ini_str(&ini_text);
    let mut cleaned_courses = Vec::with_capacity(cfg.courses.len());
    let mut seen_urls = HashSet::new();
    for item in &cfg.courses {
        let trimmed = item.url.trim().to_string();
        if !is_https_url(&trimmed) || !seen_urls.insert(trimmed.clone()) {
            continue;
        }
        let mut copy = item.clone();
        copy.url = trimmed;
        cleaned_courses.push(copy);
    }
    normalized.courses = cleaned_courses;
    if !cfg.mirrors.is_empty() {
        normalized.mirrors = cfg.mirrors.clone();
    }
    normalized.llm = llm_norm;
    Ok(normalized)
}
