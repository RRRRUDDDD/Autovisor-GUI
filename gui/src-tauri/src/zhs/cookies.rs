use crate::config::write_file_atomic;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::fs;
use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct PlaywrightCookie {
    pub name: String,
    pub value: String,
    pub domain: String,
    pub path: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub expires: Option<f64>,
    #[serde(default)]
    pub http_only: bool,
    #[serde(default)]
    pub secure: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub same_site: Option<String>,
}

pub(crate) fn now_epoch_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

pub(crate) fn is_zhihuishu_domain(domain: &str) -> bool {
    let norm = domain.trim().trim_start_matches('.').to_ascii_lowercase();
    norm == "zhihuishu.com" || norm.ends_with(".zhihuishu.com")
}

pub(crate) fn is_login_url(url: &str) -> bool {
    let lower = url.to_ascii_lowercase();
    lower.contains("passport.zhihuishu.com") || lower.contains("login.zhihuishu.com")
}

pub(crate) fn normalize_cdp_cookies(raw_cookies: &[Value]) -> Vec<PlaywrightCookie> {
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

pub(crate) fn save_cookies_atomic(
    base_dir: &Path,
    cookies: &[PlaywrightCookie],
) -> Result<(), String> {
    let data_dir = base_dir.join("data");
    let target = data_dir.join("cookies.json");
    let json_text =
        serde_json::to_string(cookies).map_err(|e| format!("序列化 Cookie 失败: {e}"))?;
    write_file_atomic(&target, json_text.as_bytes(), "Cookie 文件")
}

pub(crate) fn load_saved_cookies(base_dir: &Path) -> Result<Vec<PlaywrightCookie>, String> {
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

pub(crate) fn merge_cookies(
    base: &[PlaywrightCookie],
    updates: &[PlaywrightCookie],
) -> Vec<PlaywrightCookie> {
    if updates.is_empty() {
        return base.to_vec();
    }
    let mut out = base.to_vec();
    for upd in updates {
        let upd_domain = upd.domain.trim().to_ascii_lowercase();
        let upd_path = if upd.path.is_empty() {
            "/"
        } else {
            upd.path.as_str()
        };
        if let Some(slot) = out.iter_mut().find(|c| {
            c.name == upd.name
                && c.domain.trim().to_ascii_lowercase() == upd_domain
                && (if c.path.is_empty() {
                    "/"
                } else {
                    c.path.as_str()
                }) == upd_path
        }) {
            let upd_exp = upd.expires.filter(|&e| e > 0.0);
            let slot_exp = slot.expires.filter(|&e| e > 0.0);
            if upd.value == slot.value {
                if slot_exp.is_none() {
                    slot.expires = upd_exp;
                } else if let (Some(s), Some(u)) = (slot_exp, upd_exp) {
                    if u > s {
                        slot.expires = Some(u);
                    }
                }
                if slot.same_site.is_none() {
                    slot.same_site = upd.same_site.clone();
                }
                slot.secure = slot.secure || upd.secure;
                slot.http_only = slot.http_only || upd.http_only;
            } else {
                slot.value = upd.value.clone();
                if upd_exp.is_some() {
                    slot.expires = upd_exp;
                }
                if upd.same_site.is_some() {
                    slot.same_site = upd.same_site.clone();
                }
                slot.secure = slot.secure || upd.secure;
                slot.http_only = slot.http_only || upd.http_only;
            }
        } else {
            out.push(upd.clone());
        }
    }
    out
}
