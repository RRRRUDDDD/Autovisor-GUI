use serde::{Deserialize, Serialize};
use std::collections::HashSet;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock};
#[cfg(windows)]
use winreg::enums::{HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE};
#[cfg(windows)]
use winreg::RegKey;

static BROWSER_CACHE: OnceLock<Mutex<Option<Vec<DetectedBrowser>>>> = OnceLock::new();

pub fn detect_installed_browsers() -> Vec<DetectedBrowser> {
    let cache = BROWSER_CACHE.get_or_init(|| Mutex::new(None));
    if let Ok(guard) = cache.lock() {
        if let Some(ref list) = *guard {
            return list.clone();
        }
    }
    refresh_installed_browsers()
}

pub fn refresh_installed_browsers() -> Vec<DetectedBrowser> {
    let scanned = scan_installed_browsers();
    let cache = BROWSER_CACHE.get_or_init(|| Mutex::new(None));
    if let Ok(mut guard) = cache.lock() {
        *guard = Some(scanned.clone());
    }
    scanned
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct DetectedBrowser {
    pub id: String,
    pub name: String,
    pub driver: String, // "Edge" | "Chrome" | "Chromium"
    pub exe_path: String,
    pub source: String, // "registry" | "standard" | "playwright"
    pub recommended: bool,
}

#[cfg(windows)]
fn read_app_path(root: winreg::HKEY, subkey: &str) -> Option<PathBuf> {
    let hk = RegKey::predef(root);
    let key = hk.open_subkey(subkey).ok()?;
    let raw: String = key.get_value("").ok()?;
    let trimmed = raw.trim().trim_matches('"').trim();
    if trimmed.is_empty() {
        return None;
    }
    let path = PathBuf::from(trimmed);
    if path.is_file() {
        Some(path)
    } else {
        None
    }
}

#[cfg(windows)]
fn find_via_registry(exe_name: &str) -> Option<PathBuf> {
    let paths = [
        (
            HKEY_LOCAL_MACHINE,
            format!(r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}"),
        ),
        (
            HKEY_LOCAL_MACHINE,
            format!(r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}"),
        ),
        (
            HKEY_CURRENT_USER,
            format!(r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}"),
        ),
    ];
    for (root, subkey) in paths {
        if let Some(p) = read_app_path(root, &subkey) {
            return Some(p);
        }
    }
    None
}

fn push_if_valid(
    out: &mut Vec<DetectedBrowser>,
    seen: &mut HashSet<String>,
    id: &str,
    name: &str,
    driver: &str,
    path: PathBuf,
    source: &str,
) {
    if !path.is_file() {
        return;
    }
    let canonical = fs::canonicalize(&path).unwrap_or(path);
    let mut display = canonical.to_string_lossy().to_string();
    if let Some(stripped) = display.strip_prefix(r"\\?\") {
        display = stripped.to_string();
    }
    let key = display.to_ascii_lowercase();
    if seen.insert(key) {
        out.push(DetectedBrowser {
            id: id.to_string(),
            name: name.to_string(),
            driver: driver.to_string(),
            exe_path: display,
            source: source.to_string(),
            recommended: false,
        });
    }
}

#[cfg(windows)]
fn env_paths(var: &str, rels: &[&str]) -> Vec<PathBuf> {
    let Ok(base) = std::env::var(var) else {
        return Vec::new();
    };
    if base.trim().is_empty() {
        return Vec::new();
    }
    let root = PathBuf::from(base);
    rels.iter().map(|rel| root.join(rel)).collect()
}

#[cfg(windows)]
fn discover_playwright_chromium() -> Vec<PathBuf> {
    let mut found = Vec::new();
    let Ok(local) = std::env::var("LOCALAPPDATA") else {
        return found;
    };
    let pw_dir = PathBuf::from(local).join("ms-playwright");
    let Ok(entries) = fs::read_dir(pw_dir) else {
        return found;
    };
    let mut dirs: Vec<PathBuf> = entries
        .flatten()
        .map(|e| e.path())
        .filter(|p| {
            p.is_dir()
                && p.file_name()
                    .and_then(|s| s.to_str())
                    .map(|n| n.starts_with("chromium-"))
                    .unwrap_or(false)
        })
        .collect();
    dirs.sort();
    dirs.reverse();
    for dir in dirs {
        for sub in [
            r"chrome-win\chrome.exe",
            r"chrome-win64\chrome.exe",
        ] {
            let candidate = dir.join(sub);
            if candidate.is_file() {
                found.push(candidate);
            }
        }
    }
    found
}

/// Automatically scans the local device for Chromium-based browsers
/// compatible with Playwright and CDP, ordered by reliability.
#[cfg(windows)]
fn scan_installed_browsers() -> Vec<DetectedBrowser> {
    let mut out = Vec::new();
    let mut seen = HashSet::new();

    struct BrowserSpec<'a> {
        id: &'a str,
        name: &'a str,
        driver: &'a str,
        app_path_exe: Option<&'a str>,
        program_files_x86: &'a [&'a str],
        program_files: &'a [&'a str],
        local_appdata: &'a [&'a str],
    }

    let specs = [
        BrowserSpec {
            id: "edge",
            name: "Microsoft Edge",
            driver: "Edge",
            app_path_exe: Some("msedge.exe"),
            program_files_x86: &[r"Microsoft\Edge\Application\msedge.exe"],
            program_files: &[r"Microsoft\Edge\Application\msedge.exe"],
            local_appdata: &[r"Microsoft\Edge\Application\msedge.exe"],
        },
        BrowserSpec {
            id: "chrome",
            name: "Google Chrome",
            driver: "Chrome",
            app_path_exe: Some("chrome.exe"),
            program_files_x86: &[r"Google\Chrome\Application\chrome.exe"],
            program_files: &[r"Google\Chrome\Application\chrome.exe"],
            local_appdata: &[r"Google\Chrome\Application\chrome.exe"],
        },
        BrowserSpec {
            id: "edge-beta",
            name: "Microsoft Edge Beta",
            driver: "Edge",
            app_path_exe: None,
            program_files_x86: &[r"Microsoft\Edge Beta\Application\msedge.exe"],
            program_files: &[r"Microsoft\Edge Beta\Application\msedge.exe"],
            local_appdata: &[r"Microsoft\Edge Beta\Application\msedge.exe"],
        },
        BrowserSpec {
            id: "chrome-beta",
            name: "Google Chrome Beta",
            driver: "Chrome",
            app_path_exe: None,
            program_files_x86: &[r"Google\Chrome Beta\Application\chrome.exe"],
            program_files: &[r"Google\Chrome Beta\Application\chrome.exe"],
            local_appdata: &[r"Google\Chrome Beta\Application\chrome.exe"],
        },
        BrowserSpec {
            id: "brave",
            name: "Brave Browser",
            driver: "Chrome",
            app_path_exe: Some("brave.exe"),
            program_files_x86: &[r"BraveSoftware\Brave-Browser\Application\brave.exe"],
            program_files: &[r"BraveSoftware\Brave-Browser\Application\brave.exe"],
            local_appdata: &[r"BraveSoftware\Brave-Browser\Application\brave.exe"],
        },
        BrowserSpec {
            id: "vivaldi",
            name: "Vivaldi",
            driver: "Chrome",
            app_path_exe: Some("vivaldi.exe"),
            program_files_x86: &[r"Vivaldi\Application\vivaldi.exe"],
            program_files: &[r"Vivaldi\Application\vivaldi.exe"],
            local_appdata: &[r"Vivaldi\Application\vivaldi.exe"],
        },
        BrowserSpec {
            id: "360chrome",
            name: "360 极速浏览器",
            driver: "Chrome",
            app_path_exe: Some("360chromex.exe"),
            program_files_x86: &[r"360\360ChromeX\Chrome\Application\360ChromeX.exe"],
            program_files: &[r"360\360ChromeX\Chrome\Application\360ChromeX.exe"],
            local_appdata: &[
                r"360ChromeX\Chrome\Application\360ChromeX.exe",
                r"360Chrome\Chrome\Application\360chrome.exe",
            ],
        },
        BrowserSpec {
            id: "qqbrowser",
            name: "QQ 浏览器",
            driver: "Chrome",
            app_path_exe: Some("QQBrowser.exe"),
            program_files_x86: &[r"Tencent\QQBrowser\QQBrowser.exe"],
            program_files: &[r"Tencent\QQBrowser\QQBrowser.exe"],
            local_appdata: &[r"Tencent\QQBrowser\QQBrowser.exe"],
        },
        BrowserSpec {
            id: "chromium",
            name: "Chromium",
            driver: "Chrome",
            app_path_exe: None,
            program_files_x86: &[r"Chromium\Application\chrome.exe"],
            program_files: &[r"Chromium\Application\chrome.exe"],
            local_appdata: &[r"Chromium\Application\chrome.exe"],
        },
    ];

    for spec in specs {
        if let Some(exe_name) = spec.app_path_exe {
            if let Some(path) = find_via_registry(exe_name) {
                push_if_valid(
                    &mut out,
                    &mut seen,
                    spec.id,
                    spec.name,
                    spec.driver,
                    path,
                    "registry",
                );
            }
        }
        for path in env_paths("ProgramFiles(x86)", spec.program_files_x86)
            .into_iter()
            .chain(env_paths("ProgramFiles", spec.program_files))
            .chain(env_paths("LOCALAPPDATA", spec.local_appdata))
        {
            push_if_valid(
                &mut out,
                &mut seen,
                spec.id,
                spec.name,
                spec.driver,
                path,
                "standard",
            );
        }
    }

    for path in discover_playwright_chromium() {
        push_if_valid(
            &mut out,
            &mut seen,
            "playwright-chromium",
            "Playwright Chromium",
            "Chromium",
            path,
            "playwright",
        );
    }

    if let Some(first) = out.first_mut() {
        first.recommended = true;
    }
    out
}

#[cfg(target_os = "macos")]
fn discover_playwright_chromium_macos() -> Vec<PathBuf> {
    let mut found = Vec::new();
    let Ok(home) = std::env::var("HOME") else {
        return found;
    };
    let pw_dir = PathBuf::from(home)
        .join("Library")
        .join("Caches")
        .join("ms-playwright");
    let Ok(entries) = fs::read_dir(pw_dir) else {
        return found;
    };
    let mut dirs: Vec<PathBuf> = entries
        .flatten()
        .map(|e| e.path())
        .filter(|p| {
            p.is_dir()
                && p.file_name()
                    .and_then(|s| s.to_str())
                    .map(|n| n.starts_with("chromium-"))
                    .unwrap_or(false)
        })
        .collect();
    dirs.sort();
    dirs.reverse();
    for dir in dirs {
        for sub in [
            "chrome-mac-arm64/Chromium.app/Contents/MacOS/Chromium",
            "chrome-mac/Chromium.app/Contents/MacOS/Chromium",
            "chrome-mac-x64/Chromium.app/Contents/MacOS/Chromium",
        ] {
            let candidate = dir.join(sub);
            if candidate.is_file() {
                found.push(candidate);
            }
        }
    }
    found
}

#[cfg(target_os = "macos")]
fn scan_installed_browsers() -> Vec<DetectedBrowser> {
    let mut out = Vec::new();
    let mut seen = HashSet::new();

    let specs = [
        (
            "chrome",
            "Google Chrome",
            "Chrome",
            "Google Chrome.app/Contents/MacOS/Google Chrome",
        ),
        (
            "edge",
            "Microsoft Edge",
            "Edge",
            "Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ),
        (
            "brave",
            "Brave Browser",
            "Chrome",
            "Brave Browser.app/Contents/MacOS/Brave Browser",
        ),
        (
            "vivaldi",
            "Vivaldi",
            "Chrome",
            "Vivaldi.app/Contents/MacOS/Vivaldi",
        ),
    ];

    let mut roots = vec![PathBuf::from("/Applications")];
    if let Ok(home) = std::env::var("HOME") {
        roots.push(PathBuf::from(home).join("Applications"));
    }

    for (id, name, driver, rel) in specs {
        for root in &roots {
            push_if_valid(&mut out, &mut seen, id, name, driver, root.join(rel), "standard");
        }
    }

    for path in discover_playwright_chromium_macos() {
        push_if_valid(
            &mut out,
            &mut seen,
            "playwright-chromium",
            "Playwright Chromium",
            "Chromium",
            path,
            "playwright",
        );
    }

    if let Some(first) = out.first_mut() {
        first.recommended = true;
    }
    out
}

#[cfg(target_os = "linux")]
fn find_in_path(bin_name: &str) -> Option<PathBuf> {
    if let Some(path_var) = std::env::var_os("PATH") {
        for dir in std::env::split_paths(&path_var) {
            let candidate = dir.join(bin_name);
            if candidate.is_file() {
                return Some(candidate);
            }
        }
    }
    for dir in ["/usr/bin", "/usr/local/bin", "/snap/bin"] {
        let candidate = Path::new(dir).join(bin_name);
        if candidate.is_file() {
            return Some(candidate);
        }
    }
    None
}

#[cfg(target_os = "linux")]
fn discover_playwright_chromium_linux() -> Vec<PathBuf> {
    let mut found = Vec::new();
    let Ok(home) = std::env::var("HOME") else {
        return found;
    };
    let pw_dir = PathBuf::from(home).join(".cache").join("ms-playwright");
    let Ok(entries) = fs::read_dir(pw_dir) else {
        return found;
    };
    let mut dirs: Vec<PathBuf> = entries
        .flatten()
        .map(|e| e.path())
        .filter(|p| {
            p.is_dir()
                && p.file_name()
                    .and_then(|s| s.to_str())
                    .map(|n| n.starts_with("chromium-"))
                    .unwrap_or(false)
        })
        .collect();
    dirs.sort();
    dirs.reverse();
    for dir in dirs {
        for sub in ["chrome-linux/chrome", "chrome-linux64/chrome"] {
            let candidate = dir.join(sub);
            if candidate.is_file() {
                found.push(candidate);
            }
        }
    }
    found
}

#[cfg(target_os = "linux")]
fn scan_installed_browsers() -> Vec<DetectedBrowser> {
    let mut out = Vec::new();
    let mut seen = HashSet::new();

    let specs: &[(&str, &str, &str, &[&str])] = &[
        (
            "chrome",
            "Google Chrome",
            "Chrome",
            &["google-chrome", "google-chrome-stable"],
        ),
        (
            "edge",
            "Microsoft Edge",
            "Edge",
            &["microsoft-edge", "microsoft-edge-stable"],
        ),
        (
            "chromium",
            "Chromium",
            "Chrome",
            &["chromium", "chromium-browser"],
        ),
        ("brave", "Brave Browser", "Chrome", &["brave-browser"]),
    ];

    for &(id, name, driver, bins) in specs {
        for bin in bins {
            if let Some(path) = find_in_path(bin) {
                push_if_valid(&mut out, &mut seen, id, name, driver, path, "standard");
            }
        }
    }

    for path in discover_playwright_chromium_linux() {
        push_if_valid(
            &mut out,
            &mut seen,
            "playwright-chromium",
            "Playwright Chromium",
            "Chromium",
            path,
            "playwright",
        );
    }

    if let Some(first) = out.first_mut() {
        first.recommended = true;
    }
    out
}

#[cfg(not(any(windows, target_os = "macos", target_os = "linux")))]
fn scan_installed_browsers() -> Vec<DetectedBrowser> {
    Vec::new()
}

/// Given a configured driver and exe_path, returns the best valid (driver, exe_path)
/// using installed browsers on this device when the configured path is missing or invalid.
pub fn resolve_browser_choice(
    configured_driver: &str,
    configured_exe: &str,
    detected: &[DetectedBrowser],
) -> Option<(String, String)> {
    let trimmed_driver = configured_driver.trim();
    let trimmed_exe = configured_exe.trim();
    let matched_detected = if !trimmed_exe.is_empty() {
        detected
            .iter()
            .find(|b| b.exe_path.eq_ignore_ascii_case(trimmed_exe))
    } else {
        None
    };
    let exe_exists = !trimmed_exe.is_empty()
        && (matched_detected.is_some() || Path::new(trimmed_exe).is_file());

    if trimmed_driver.eq_ignore_ascii_case("chromium") {
        if exe_exists {
            let is_non_chromium_browser = if let Some(found) = matched_detected {
                !found.driver.eq_ignore_ascii_case("chromium")
            } else {
                let lower = trimmed_exe.to_ascii_lowercase();
                lower.contains("msedge")
                    || lower.contains("microsoft edge")
                    || lower.contains("microsoft-edge")
                    || lower.contains("google\\chrome")
                    || lower.contains("google/chrome")
            };
            if !is_non_chromium_browser {
                let out_path = matched_detected
                    .map(|b| b.exe_path.clone())
                    .unwrap_or_else(|| trimmed_exe.to_string());
                return Some(("Chromium".to_string(), out_path));
            }
        }
        if let Some(matched) = detected
            .iter()
            .find(|b| b.driver.eq_ignore_ascii_case("chromium"))
        {
            return Some(("Chromium".to_string(), matched.exe_path.clone()));
        }
        return Some(("Chromium".to_string(), String::new()));
    }

    if exe_exists {
        let inferred_driver = if let Some(found) = matched_detected {
            found.driver.clone()
        } else {
            let lower = trimmed_exe.to_ascii_lowercase();
            if lower.contains("msedge")
                || lower.contains("microsoft edge")
                || lower.contains("microsoft-edge")
            {
                "Edge".to_string()
            } else {
                "Chrome".to_string()
            }
        };
        let resolved_exe = matched_detected
            .map(|b| b.exe_path.clone())
            .unwrap_or_else(|| trimmed_exe.to_string());

        if trimmed_driver.is_empty() || inferred_driver.eq_ignore_ascii_case(trimmed_driver) {
            return Some((inferred_driver, resolved_exe));
        }

        if let Some(matched) = detected
            .iter()
            .find(|b| b.driver.eq_ignore_ascii_case(trimmed_driver))
        {
            return Some((matched.driver.clone(), matched.exe_path.clone()));
        }
        return Some((inferred_driver, resolved_exe));
    }

    // Try to match configured driver among detected browsers
    if let Some(matched) = detected
        .iter()
        .find(|b| b.driver.eq_ignore_ascii_case(trimmed_driver))
    {
        return Some((matched.driver.clone(), matched.exe_path.clone()));
    }

    // Fall back to the top recommended detected browser on this device
    detected
        .first()
        .map(|b| (b.driver.clone(), b.exe_path.clone()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn resolves_fallback_browser_when_configured_driver_missing() {
        let detected = vec![DetectedBrowser {
            id: "chrome".into(),
            name: "Google Chrome".into(),
            driver: "Chrome".into(),
            exe_path: r"C:\Program Files\Google\Chrome\Application\chrome.exe".into(),
            source: "registry".into(),
            recommended: true,
        }];
        let resolved = resolve_browser_choice("Edge", "", &detected).unwrap();
        assert_eq!(resolved.0, "Chrome");
        assert_eq!(
            resolved.1,
            r"C:\Program Files\Google\Chrome\Application\chrome.exe"
        );
    }

    #[test]
    fn matches_configured_driver_when_available() {
        let detected = vec![
            DetectedBrowser {
                id: "edge".into(),
                name: "Microsoft Edge".into(),
                driver: "Edge".into(),
                exe_path: r"C:\Edge\msedge.exe".into(),
                source: "standard".into(),
                recommended: true,
            },
            DetectedBrowser {
                id: "chrome".into(),
                name: "Google Chrome".into(),
                driver: "Chrome".into(),
                exe_path: r"C:\Chrome\chrome.exe".into(),
                source: "standard".into(),
                recommended: false,
            },
        ];
        let resolved = resolve_browser_choice("Chrome", "", &detected).unwrap();
        assert_eq!(resolved.0, "Chrome");
        assert_eq!(resolved.1, r"C:\Chrome\chrome.exe");
    }

    #[test]
    fn preserves_chromium_driver_even_with_stale_edge_exe_path() {
        let detected_no_pw = vec![DetectedBrowser {
            id: "edge".into(),
            name: "Microsoft Edge".into(),
            driver: "Edge".into(),
            exe_path: r"C:\Edge\msedge.exe".into(),
            source: "standard".into(),
            recommended: true,
        }];
        let resolved =
            resolve_browser_choice("Chromium", r"C:\Edge\msedge.exe", &detected_no_pw).unwrap();
        assert_eq!(resolved.0, "Chromium");
        assert_eq!(resolved.1, "");

        let detected_with_pw = vec![
            DetectedBrowser {
                id: "edge".into(),
                name: "Microsoft Edge".into(),
                driver: "Edge".into(),
                exe_path: r"C:\Edge\msedge.exe".into(),
                source: "standard".into(),
                recommended: true,
            },
            DetectedBrowser {
                id: "playwright-chromium".into(),
                name: "Playwright Chromium".into(),
                driver: "Chromium".into(),
                exe_path: r"C:\ms-playwright\chromium-1100\chrome-win\chrome.exe".into(),
                source: "playwright".into(),
                recommended: false,
            },
        ];
        let resolved_pw =
            resolve_browser_choice("Chromium", r"C:\Edge\msedge.exe", &detected_with_pw).unwrap();
        assert_eq!(resolved_pw.0, "Chromium");
        assert_eq!(
            resolved_pw.1,
            r"C:\ms-playwright\chromium-1100\chrome-win\chrome.exe"
        );
    }

    #[test]
    fn switches_exe_path_when_driver_changed_from_edge_to_chrome() {
        let detected = vec![
            DetectedBrowser {
                id: "edge".into(),
                name: "Microsoft Edge".into(),
                driver: "Edge".into(),
                exe_path: r"C:\Edge\msedge.exe".into(),
                source: "standard".into(),
                recommended: true,
            },
            DetectedBrowser {
                id: "chrome".into(),
                name: "Google Chrome".into(),
                driver: "Chrome".into(),
                exe_path: r"C:\Chrome\chrome.exe".into(),
                source: "standard".into(),
                recommended: false,
            },
        ];
        let resolved =
            resolve_browser_choice("Chrome", r"C:\Edge\msedge.exe", &detected).unwrap();
        assert_eq!(resolved.0, "Chrome");
        assert_eq!(resolved.1, r"C:\Chrome\chrome.exe");
    }
}
