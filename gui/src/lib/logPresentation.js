export const LOG_LIMIT = 800;

export function logLevel(raw) {
  const value = String(raw || 'info').toLowerCase();
  if (value === 'error' || value === 'err') return 'error';
  if (value === 'warning' || value === 'warn') return 'warning';
  if (value === 'success' || value === 'ok') return 'success';
  if (value === 'debug' || value === 'dbg') return 'debug';
  return 'info';
}

export function logTime(timestamp) {
  if (!timestamp) return '--:--:--';
  if (typeof timestamp === 'string' && /^\d{2}:\d{2}:\d{2}/.test(timestamp)) {
    return timestamp.slice(0, 8);
  }
  const date = new Date(typeof timestamp === 'number' && timestamp < 1e12 ? timestamp * 1000 : timestamp);
  if (Number.isNaN(date.getTime())) return '--:--:--';
  return date.toTimeString().slice(0, 8);
}

export function filterLogs(logs, levelFilter = 'all', query = '') {
  const q = String(query || '').trim().toLowerCase();
  return (logs || []).filter((entry) => {
    const lv = logLevel(entry.level);
    if (levelFilter === 'info' && lv === 'debug') return false;
    if (levelFilter === 'errors' && lv !== 'error') return false;
    if (levelFilter === 'warnings' && lv !== 'error' && lv !== 'warning') return false;
    if (q) {
      const msg = String(entry.message || '').toLowerCase();
      if (!msg.includes(q)) return false;
    }
    return true;
  });
}

export function redactSensitive(text) {
  return String(text || '')
    .replace(/(sk-[A-Za-z0-9_-]{8,})/g, 'sk-***REDACTED***')
    .replace(/(AIza[0-9A-Za-z_-]{16,})/g, 'AIza***REDACTED***')
    .replace(/((?:xai|gsk)_[A-Za-z0-9_-]{12,}|xai-[A-Za-z0-9_-]{12,})/g, '***REDACTED***')
    .replace(/((?:https?|socks5h?):\/\/)([^:\s/@]+):([^@\s/]+)@/gi, '$1***:***@')
    .replace(/(https?:\/\/api\.day\.app\/)([^\s/?#]+)/gi, '$1***REDACTED***')
    .replace(/(https?:\/\/sctapi\.ftqq\.com\/)([^\s/?#]+)/gi, '$1***REDACTED***')
    .replace(/(https?:\/\/qmsg\.zendee\.cn\/send\/)([^\s/?#]+)/gi, '$1***REDACTED***')
    .replace(/(https?:\/\/api\.telegram\.org\/bot)([^\s/?#]+)/gi, '$1***REDACTED***')
    .replace(
      /((?:notification[_-]?url|tg[_-]?chat[_-]?id|chat[_-]?id|bark[_-]?url|pushplus[_-]?token|password|passwd|pwd|api[_-]?key|x-api-key|x-goog-api-key|token|cookie)\s*[:=]\s*)([^\s,;]+)/gi,
      '$1***REDACTED***'
    )
    .replace(/\b(1[3-9]\d)\d{4}(\d{4})\b/g, '$1****$2');
}

export function formatLogsForCopy(logs) {
  return (logs || [])
    .map((entry) => `[${logTime(entry.timestamp)}] [${logLevel(entry.level).toUpperCase()}] ${redactSensitive(entry.message)}`)
    .join('\n');
}
