//! 记住登录凭据的跨平台安全存储：
//! - Windows: 使用 Win32 DPAPI (`CryptProtectData` / `CryptUnprotectData`) 将密码加密为当前用户绑定的密文后落盘到 `data/credentials.json`；
//! - macOS / Linux: 优先使用系统密钥链 (`keyring`: macOS Keychain / Linux Secret Service)，
//!   在无桌面密钥链服务（如 headless Linux / CI）环境下自动回退到权限严格为 `0o600` 的 `data/credentials.json`。

use crate::config::write_file_atomic;
use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock};

pub const PHONE_PASSWORD_KEY: &str = "phone_password";
pub const STUDENT_PASSWORD_KEY: &str = "student_password";

#[cfg(windows)]
const DPAPI_PREFIX: &str = "dpapi:";
#[cfg(unix)]
const KEYRING_SERVICE: &str = "com.autovisor.gui";
#[cfg(unix)]
const KEYRING_SENTINEL: &str = "keyring:";
#[cfg(unix)]
const FILE_PREFIX: &str = "b64:";

fn store_lock() -> &'static Mutex<()> {
    static LOCK: OnceLock<Mutex<()>> = OnceLock::new();
    LOCK.get_or_init(|| Mutex::new(()))
}

fn credentials_path(base_dir: &Path) -> PathBuf {
    base_dir.join("data").join("credentials.json")
}

fn read_store_map(base_dir: &Path) -> BTreeMap<String, String> {
    let path = credentials_path(base_dir);
    let Ok(raw) = fs::read_to_string(&path) else {
        return BTreeMap::new();
    };
    serde_json::from_str::<BTreeMap<String, String>>(raw.trim_start_matches('\u{feff}'))
        .unwrap_or_default()
}

fn write_store_map(base_dir: &Path, map: &BTreeMap<String, String>) -> Result<(), String> {
    let path = credentials_path(base_dir);
    if map.is_empty() {
        if path.is_file() {
            let _ = fs::remove_file(&path);
        }
        return Ok(());
    }
    let body = serde_json::to_string_pretty(map)
        .map_err(|e| format!("序列化凭据存储失败: {e}"))?;
    write_file_atomic(&path, body.as_bytes(), "data/credentials.json")
}

fn b64_encode(bytes: &[u8]) -> String {
    const TABLE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for chunk in bytes.chunks(3) {
        let b0 = chunk[0] as u32;
        let b1 = if chunk.len() > 1 { chunk[1] as u32 } else { 0 };
        let b2 = if chunk.len() > 2 { chunk[2] as u32 } else { 0 };
        let n = (b0 << 16) | (b1 << 8) | b2;
        out.push(TABLE[((n >> 18) & 63) as usize] as char);
        out.push(TABLE[((n >> 12) & 63) as usize] as char);
        if chunk.len() > 1 {
            out.push(TABLE[((n >> 6) & 63) as usize] as char);
        } else {
            out.push('=');
        }
        if chunk.len() > 2 {
            out.push(TABLE[(n & 63) as usize] as char);
        } else {
            out.push('=');
        }
    }
    out
}

fn b64_decode(input: &str) -> Result<Vec<u8>, String> {
    let mut out = Vec::with_capacity(input.len() * 3 / 4);
    let mut buf: u32 = 0;
    let mut bits: u8 = 0;
    for b in input.bytes() {
        let val = match b {
            b'A'..=b'Z' => b - b'A',
            b'a'..=b'z' => b - b'a' + 26,
            b'0'..=b'9' => b - b'0' + 52,
            b'+' => 62,
            b'/' => 63,
            b'=' | b'\n' | b'\r' | b' ' | b'\t' => continue,
            _ => return Err("无效的 Base64 密文".to_string()),
        };
        buf = (buf << 6) | (val as u32);
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push(((buf >> bits) & 0xFF) as u8);
        }
    }
    Ok(out)
}

#[cfg(windows)]
fn dpapi_protect(plaintext: &[u8]) -> Result<Vec<u8>, String> {
    use windows::Win32::Foundation::{LocalFree, HLOCAL};
    use windows::Win32::Security::Cryptography::{
        CryptProtectData, CRYPTPROTECT_UI_FORBIDDEN, CRYPT_INTEGER_BLOB,
    };

    const ENTROPY: &[u8] = b"Autovisor-GUI-SavedLogin-v1";
    let mut in_buf = plaintext.to_vec();
    let mut entropy_buf = ENTROPY.to_vec();
    let in_blob = CRYPT_INTEGER_BLOB {
        cbData: u32::try_from(in_buf.len()).map_err(|_| "密码内容过长".to_string())?,
        pbData: in_buf.as_mut_ptr(),
    };
    let entropy_blob = CRYPT_INTEGER_BLOB {
        cbData: entropy_buf.len() as u32,
        pbData: entropy_buf.as_mut_ptr(),
    };
    let mut out_blob = CRYPT_INTEGER_BLOB::default();

    unsafe {
        CryptProtectData(
            &in_blob,
            windows::core::PCWSTR::null(),
            Some(&entropy_blob),
            None,
            None,
            CRYPTPROTECT_UI_FORBIDDEN,
            &mut out_blob,
        )
        .map_err(|e| format!("DPAPI 加密凭据失败: {e}"))?;

        let slice = std::slice::from_raw_parts(out_blob.pbData, out_blob.cbData as usize);
        let encrypted = slice.to_vec();
        let _ = LocalFree(Some(HLOCAL(out_blob.pbData.cast())));
        Ok(encrypted)
    }
}

#[cfg(windows)]
fn dpapi_unprotect(ciphertext: &[u8]) -> Result<Vec<u8>, String> {
    use windows::Win32::Foundation::{LocalFree, HLOCAL};
    use windows::Win32::Security::Cryptography::{
        CryptUnprotectData, CRYPTPROTECT_UI_FORBIDDEN, CRYPT_INTEGER_BLOB,
    };

    const ENTROPY: &[u8] = b"Autovisor-GUI-SavedLogin-v1";
    let mut in_buf = ciphertext.to_vec();
    let mut entropy_buf = ENTROPY.to_vec();
    let in_blob = CRYPT_INTEGER_BLOB {
        cbData: u32::try_from(in_buf.len()).map_err(|_| "密文长度异常".to_string())?,
        pbData: in_buf.as_mut_ptr(),
    };
    let entropy_blob = CRYPT_INTEGER_BLOB {
        cbData: entropy_buf.len() as u32,
        pbData: entropy_buf.as_mut_ptr(),
    };
    let mut out_blob = CRYPT_INTEGER_BLOB::default();

    unsafe {
        CryptUnprotectData(
            &in_blob,
            None,
            Some(&entropy_blob),
            None,
            None,
            CRYPTPROTECT_UI_FORBIDDEN,
            &mut out_blob,
        )
        .map_err(|e| format!("DPAPI 解密凭据失败: {e}"))?;

        let slice = std::slice::from_raw_parts(out_blob.pbData, out_blob.cbData as usize);
        let decrypted = slice.to_vec();
        let _ = LocalFree(Some(HLOCAL(out_blob.pbData.cast())));
        Ok(decrypted)
    }
}

#[cfg(unix)]
fn keyring_account(base_dir: &Path, key: &str) -> String {
    use std::collections::hash_map::DefaultHasher;
    use std::hash::{Hash, Hasher};
    let mut hasher = DefaultHasher::new();
    base_dir.to_string_lossy().hash(&mut hasher);
    format!("{key}:{:016x}", hasher.finish())
}

/// 加密保存密码凭据（若 secret 为空则等同于删除）。
pub fn store_secret(base_dir: &Path, key: &str, secret: &str) -> Result<(), String> {
    if secret.is_empty() {
        return delete_secret(base_dir, key);
    }
    let _guard = store_lock().lock().unwrap_or_else(|e| e.into_inner());
    let mut map = read_store_map(base_dir);

    #[cfg(windows)]
    {
        let encrypted = dpapi_protect(secret.as_bytes())?;
        map.insert(key.to_string(), format!("{DPAPI_PREFIX}{}", b64_encode(&encrypted)));
        write_store_map(base_dir, &map)
    }

    #[cfg(unix)]
    {
        let account = keyring_account(base_dir, key);
        let stored_in_keyring = keyring::Entry::new(KEYRING_SERVICE, &account)
            .and_then(|entry| entry.set_password(secret))
            .is_ok();
        if stored_in_keyring {
            map.insert(key.to_string(), format!("{KEYRING_SENTINEL}{account}"));
        } else {
            // 无系统密钥链守护进程（如 headless Linux / CI）时，降级到 0o600 权限文件
            map.insert(key.to_string(), format!("{FILE_PREFIX}{}", b64_encode(secret.as_bytes())));
        }
        write_store_map(base_dir, &map)
    }
}

/// 读取并解密已保存的密码凭据。
pub fn load_secret(base_dir: &Path, key: &str) -> Result<Option<String>, String> {
    let _guard = store_lock().lock().unwrap_or_else(|e| e.into_inner());
    let map = read_store_map(base_dir);
    let Some(raw) = map.get(key) else {
        return Ok(None);
    };

    #[cfg(windows)]
    {
        if let Some(encoded) = raw.strip_prefix(DPAPI_PREFIX) {
            let cipher = b64_decode(encoded)?;
            let plain = dpapi_unprotect(&cipher)?;
            let text = String::from_utf8(plain)
                .map_err(|e| format!("解密后的凭据编码异常: {e}"))?;
            return Ok(if text.is_empty() { None } else { Some(text) });
        }
        Ok(None)
    }

    #[cfg(unix)]
    {
        if let Some(account) = raw.strip_prefix(KEYRING_SENTINEL) {
            match keyring::Entry::new(KEYRING_SERVICE, account).and_then(|e| e.get_password()) {
                Ok(secret) if !secret.is_empty() => return Ok(Some(secret)),
                Ok(_) => return Ok(None),
                Err(_) => return Ok(None),
            }
        }
        if let Some(encoded) = raw.strip_prefix(FILE_PREFIX) {
            let bytes = b64_decode(encoded)?;
            let text = String::from_utf8(bytes)
                .map_err(|e| format!("读取本地凭据编码异常: {e}"))?;
            return Ok(if text.is_empty() { None } else { Some(text) });
        }
        Ok(None)
    }
}

/// 检查某类凭据是否已加密保存（不返回明文）。
pub fn has_secret(base_dir: &Path, key: &str) -> bool {
    let _guard = store_lock().lock().unwrap_or_else(|e| e.into_inner());
    let map = read_store_map(base_dir);
    let Some(raw) = map.get(key) else {
        return false;
    };
    #[cfg(windows)]
    {
        raw.starts_with(DPAPI_PREFIX) && raw.len() > DPAPI_PREFIX.len()
    }
    #[cfg(unix)]
    {
        if let Some(account) = raw.strip_prefix(KEYRING_SENTINEL) {
            return keyring::Entry::new(KEYRING_SERVICE, account)
                .and_then(|e| e.get_password())
                .map(|s| !s.is_empty())
                .unwrap_or(false);
        }
        raw.starts_with(FILE_PREFIX) && raw.len() > FILE_PREFIX.len()
    }
}

/// 删除已保存的加密凭据。
pub fn delete_secret(base_dir: &Path, key: &str) -> Result<(), String> {
    let _guard = store_lock().lock().unwrap_or_else(|e| e.into_inner());
    let mut map = read_store_map(base_dir);
    #[cfg(unix)]
    {
        if let Some(account) = map.get(key).and_then(|v| v.strip_prefix(KEYRING_SENTINEL)) {
            if let Ok(entry) = keyring::Entry::new(KEYRING_SERVICE, account) {
                let _ = entry.delete_credential();
            }
        }
    }
    if map.remove(key).is_some() {
        write_store_map(base_dir, &map)?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn temp_dir(name: &str) -> PathBuf {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let dir = std::env::temp_dir().join(format!("autovisor_creds_{name}_{nanos}"));
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn roundtrip_store_load_delete_encrypted_secret() {
        let dir = temp_dir("roundtrip");
        assert!(!has_secret(&dir, PHONE_PASSWORD_KEY));
        assert_eq!(load_secret(&dir, PHONE_PASSWORD_KEY).unwrap(), None);

        let secret = "  P@ssw0rd!中文测试  ";
        store_secret(&dir, PHONE_PASSWORD_KEY, secret).unwrap();
        assert!(has_secret(&dir, PHONE_PASSWORD_KEY));

        // 确认磁盘文件中不包含明文密码
        let raw_file = fs::read_to_string(credentials_path(&dir)).unwrap();
        assert!(!raw_file.contains(secret));
        #[cfg(windows)]
        assert!(raw_file.contains(DPAPI_PREFIX));

        let loaded = load_secret(&dir, PHONE_PASSWORD_KEY).unwrap();
        assert_eq!(loaded.as_deref(), Some(secret));

        delete_secret(&dir, PHONE_PASSWORD_KEY).unwrap();
        assert!(!has_secret(&dir, PHONE_PASSWORD_KEY));
        assert_eq!(load_secret(&dir, PHONE_PASSWORD_KEY).unwrap(), None);

        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn b64_roundtrip_handles_arbitrary_lengths() {
        for sample in [b"".as_slice(), b"a", b"ab", b"abc", b"abcd", b"\x00\xff\x7f\x80\x01"] {
            let encoded = b64_encode(sample);
            let decoded = b64_decode(&encoded).unwrap();
            assert_eq!(decoded, sample);
        }
    }
}
