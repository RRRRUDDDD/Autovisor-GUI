use crate::backend::lock_unpoisoned;
use crate::error::AppError;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::sync::{Mutex, OnceLock};
use std::time::Duration;

const SCHOOL_LIST_URL: &str = "https://passport.zhihuishu.com/user/getAllSchool";

static SCHOOL_CACHE: OnceLock<Mutex<Option<Vec<SchoolItem>>>> = OnceLock::new();

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SchoolItem {
    pub school_id: String,
    pub name: String,
    pub letter: String,
}

fn parse_school_list(body: &Value) -> Vec<SchoolItem> {
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
    parsed
}

fn filter_schools(schools: Vec<SchoolItem>, query: &str) -> Vec<SchoolItem> {
    let q = query.trim().to_lowercase();
    if q.is_empty() {
        return schools.into_iter().take(25).collect();
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
    out
}

/// Fetches (and caches) Zhihuishu's school list asynchronously for Student ID login autocomplete.
pub async fn search_schools_async(query: &str, proxy: &str) -> Result<Vec<SchoolItem>, AppError> {
    let cache_mutex = SCHOOL_CACHE.get_or_init(|| Mutex::new(None));
    let cached = {
        let guard = lock_unpoisoned(cache_mutex);
        guard.clone()
    };

    let schools = match cached {
        Some(list) => list,
        None => {
            let mut builder = reqwest::Client::builder()
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
                .map_err(|e| AppError::backend(format!("初始化 HTTP 客户端失败: {e}")))?;
            let resp = client
                .post(SCHOOL_LIST_URL)
                .header("Referer", "https://passport.zhihuishu.com/login")
                .send()
                .await
                .map_err(|e| AppError::backend(format!("获取智慧树学校列表失败: {e}")))?;
            let body: Value = resp
                .json()
                .await
                .map_err(|e| AppError::backend(format!("解析智慧树学校列表失败: {e}")))?;
            let parsed = parse_school_list(&body);
            if parsed.is_empty() {
                return Err(AppError::backend("智慧树学校列表为空，请稍后重试"));
            }
            let mut guard = lock_unpoisoned(cache_mutex);
            *guard = Some(parsed.clone());
            parsed
        }
    };

    Ok(filter_schools(schools, query))
}
