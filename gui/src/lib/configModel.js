import { inferLlmProvider, normalizeLlmApiFormat } from './llmProviders';

const URL_PATTERN = /^https:\/\/[-A-Za-z0-9+&@#/%?=~_|!:,.;]+[-A-Za-z0-9+&@#/%=~_|]$/i;
const HTTP_URL_PATTERN = /^https?:\/\/[^\s/]+/i;
const PROXY_PATTERN = /^(https?|socks5h?):\/\/[^\s/]+/i;

export const NOTIFICATION_PROVIDERS = [
  { value: '', label: '不使用通知', hint: '' },
  { value: 'Windows', label: 'Windows 系统通知', hint: '' },
  { value: 'ServerChan', label: 'Server酱', hint: 'https://sctapi.ftqq.com/YOUR_KEY.send' },
  { value: 'Qmsg', label: 'Qmsg酱', hint: 'https://qmsg.zendee.cn/send/YOUR_KEY' },
  { value: 'Bark', label: 'Bark', hint: 'https://api.day.app/YOUR_KEY/' },
  {
    value: 'Telegram',
    label: 'Telegram',
    hint: 'https://api.telegram.org/botYOUR_TOKEN/sendMessage',
  },
];

const VALID_NOTIFICATION_PROVIDERS = new Set(NOTIFICATION_PROVIDERS.map((p) => p.value));

export function normalizeNotificationProvider(raw) {
  const trimmed = String(raw || '').trim();
  if (!trimmed) return '';
  if (VALID_NOTIFICATION_PROVIDERS.has(trimmed)) return trimmed;
  const lowered = trimmed.toLowerCase();
  const matched = NOTIFICATION_PROVIDERS.find((p) => p.value.toLowerCase() === lowered);
  return matched ? matched.value : '';
}

export function notificationProviderLabel(provider) {
  const normalized = normalizeNotificationProvider(provider);
  const found = NOTIFICATION_PROVIDERS.find((p) => p.value === normalized);
  return found && found.value ? found.label : '';
}

export function isNotificationConfigured(cfg) {
  const provider = normalizeNotificationProvider(cfg?.notificationProvider);
  if (!provider) return false;
  if (provider === 'Windows') return true;
  const url = String(cfg?.notificationUrl || '').trim();
  if (!url) return false;
  if (provider === 'Telegram') {
    return Boolean(String(cfg?.notificationTgChatId || '').trim());
  }
  return true;
}

export function defaultLlmConfig() {
  return {
    provider: 'deepseek',
    apiFormat: 'openai',
    apiKey: '',
    apiBase: 'https://api.deepseek.com',
    model: 'deepseek-chat',
    visionModel: '',
    apiVersion: '',
    autoSubmit: false,
  };
}

export function defaultConfig() {
  return {
    username: '',
    password: '',
    qrLogin: false,
    qrTerminal: false,
    qrUnicode: false,
    qrSaveDir: '',
    driver: 'Edge',
    exePath: '',
    proxy: '',
    attachExistingChrome: false,
    cdpUrl: 'http://127.0.0.1:9222',
    enableAutoCaptcha: true,
    enableHideWindow: true,
    shutdownOnComplete: false,
    enableDebugLog: false,
    notificationProvider: 'Windows',
    notificationUrl: '',
    notificationTgChatId: '',
    notifyOnVerify: true,
    notifyOnFinish: true,
    notifyOnError: true,
    limitMaxTime: 0,
    limitSpeed: 1.0,
    soundOff: true,
    reviewMode: false,
    enableExamApi: true,
    enableReviewExamApi: false,
    enableZhidaoQuiz: true,
    enableMastery: true,
    masteryTarget: 100,
    llm: defaultLlmConfig(),
    courses: [],
    mirrors: {
      Huawei: 'https://mirrors.huaweicloud.com/repository/pypi',
      Aliyun: 'https://mirrors.aliyun.com/pypi',
      Tsinghua: 'https://pypi.tuna.tsinghua.edu.cn',
      Official: 'https://pypi.org',
    },
  };
}

export function isValidCourseUrl(raw) {
  return URL_PATTERN.test(String(raw || '').trim());
}

export function isValidProxyUrl(raw) {
  const trimmed = String(raw || '').trim();
  if (!trimmed) return true;
  return PROXY_PATTERN.test(trimmed);
}

export function isValidLlmApiBase(raw) {
  const trimmed = String(raw || '').trim();
  if (!trimmed) return true;
  return HTTP_URL_PATTERN.test(trimmed);
}

export function detectCourseType(rawUrl) {
  const url = String(rawUrl || '').toLowerCase();
  if (url.includes('ai-smart-course-student-pro.zhihuishu.com')) {
    return { key: 'ai-smart', label: 'AI 智慧课', cls: 'bg-tertiary-container text-on-tertiary-container' };
  }
  if (url.includes('wisdom-mooc.zhihuishu.com')) {
    return { key: 'ai-mooc', label: 'AI 慕课', cls: 'bg-tertiary-container text-on-tertiary-container' };
  }
  if (url.includes('fusioncourseh5.zhihuishu.com')) {
    return { key: 'fusion', label: '融合共享课', cls: 'bg-primary-container text-on-primary-container' };
  }
  if (url.includes('studywisdomh5.zhihuishu.com') || url.includes('hike.zhihuishu.com')) {
    return { key: 'hike', label: '翻转 / 智慧课', cls: 'bg-success-container text-on-success-container' };
  }
  if (url.includes('studyvideoh5.zhihuishu.com')) {
    return { key: 'shared', label: '智慧共享课', cls: 'bg-secondary-container text-on-secondary-container' };
  }
  if (url.includes('zhihuishu.com')) {
    return { key: 'zhs', label: '智慧树课程', cls: 'bg-surface-container-high text-on-surface-variant' };
  }
  return { key: 'custom', label: '自定义链接', cls: 'bg-warning-container text-on-warning-container' };
}

export function inferCourseTitle(rawUrl, index = 1) {
  const type = detectCourseType(rawUrl);
  try {
    const parsed = new URL(rawUrl);
    const idParam = parsed.searchParams.get('recruitAndCourseId')
      || parsed.searchParams.get('courseId')
      || parsed.searchParams.get('secretStr');
    if (idParam) {
      return `${type.label} #${idParam.slice(0, 8)}`;
    }
    const tail = parsed.pathname.split('/').filter(Boolean).pop();
    if (tail && tail.length <= 24) {
      return `${type.label} (${tail})`;
    }
  } catch {
    // ignore malformed URL
  }
  return `${type.label} ${index}`;
}

export function extractCourseUrlsFromText(text) {
  const lines = String(text || '')
    .split(/[\r\n]+/)
    .map((line) => line.trim())
    .filter(Boolean);
  const valid = [];
  const invalid = [];
  const seen = new Set();

  for (const line of lines) {
    // Allow pasting lines like "URL1 = https://..." from config.ini
    const cleaned = line.replace(/^[A-Za-z0-9_]+\s*=\s*/, '').trim();
    if (!cleaned || cleaned.startsWith(';') || cleaned.startsWith('#')) continue;
    if (isValidCourseUrl(cleaned)) {
      if (!seen.has(cleaned)) {
        seen.add(cleaned);
        valid.push(cleaned);
      }
    } else {
      invalid.push(cleaned);
    }
  }
  return { valid, invalid };
}

export function normalizeConfig(raw) {
  const base = defaultConfig();
  if (!raw || typeof raw !== 'object') return base;
  const speed = Number(raw.limitSpeed);
  const limitMaxTime = Number(raw.limitMaxTime);
  const masteryTarget = Number(raw.masteryTarget);
  const courses = Array.isArray(raw.courses)
    ? raw.courses
        .filter((item) => item && typeof item.url === 'string' && item.url.trim())
        .map((item, idx) => ({
          id: item.id || `course-${idx + 1}`,
          title: (item.title || '').trim() || inferCourseTitle(item.url.trim(), idx + 1),
          url: item.url.trim(),
          enabled: item.enabled !== false,
          teacher: String(item.teacher ?? ''),
          school: String(item.school ?? ''),
          progress: String(item.progress ?? ''),
          courseType: String(item.courseType ?? ''),
          autoDetected: Boolean(item.autoDetected),
        }))
    : [];

  const rawLlm = raw.llm && typeof raw.llm === 'object' ? raw.llm : {};
  const llmApiBase = String(rawLlm.apiBase ?? rawLlm.api_base ?? '').trim() || 'https://api.deepseek.com';
  const llmModel = String(rawLlm.model ?? '').trim() || 'deepseek-chat';
  const rawProvider = String(rawLlm.provider ?? '').trim().toLowerCase();
  const llmApiFormat = normalizeLlmApiFormat(
    rawLlm.apiFormat ?? rawLlm.api_format ?? '',
    llmApiBase,
    rawProvider
  );
  const llmProvider = rawProvider || inferLlmProvider(llmApiBase, llmApiFormat);
  const llm = {
    provider: llmProvider,
    apiFormat: llmApiFormat,
    apiKey: String(rawLlm.apiKey ?? rawLlm.api_key ?? '').trim(),
    apiBase: llmApiBase,
    model: llmModel,
    visionModel: String(rawLlm.visionModel ?? rawLlm.vision_model ?? '').trim(),
    apiVersion: String(rawLlm.apiVersion ?? rawLlm.api_version ?? '').trim(),
    autoSubmit: Boolean(rawLlm.autoSubmit ?? rawLlm.auto_submit),
  };

  return {
    username: String(raw.username ?? ''),
    password: String(raw.password ?? ''),
    qrLogin: Boolean(raw.qrLogin),
    qrTerminal: Boolean(raw.qrTerminal),
    qrUnicode: Boolean(raw.qrUnicode),
    qrSaveDir: String(raw.qrSaveDir ?? '').trim(),
    driver: ['Edge', 'Chrome', 'Chromium'].includes(raw.driver) ? raw.driver : 'Edge',
    exePath: String(raw.exePath ?? ''),
    proxy: String(raw.proxy ?? '').trim(),
    attachExistingChrome: Boolean(raw.attachExistingChrome),
    cdpUrl: String(raw.cdpUrl ?? 'http://127.0.0.1:9222') || 'http://127.0.0.1:9222',
    enableAutoCaptcha: raw.enableAutoCaptcha !== undefined ? Boolean(raw.enableAutoCaptcha) : true,
    enableHideWindow: raw.enableHideWindow !== undefined ? Boolean(raw.enableHideWindow) : true,
    shutdownOnComplete: Boolean(raw.shutdownOnComplete),
    enableDebugLog: Boolean(raw.enableDebugLog),
    notificationProvider:
      raw.notificationProvider !== undefined
        ? normalizeNotificationProvider(raw.notificationProvider)
        : 'Windows',
    notificationUrl: String(raw.notificationUrl ?? '').trim(),
    notificationTgChatId: String(raw.notificationTgChatId ?? '').trim(),
    notifyOnVerify: raw.notifyOnVerify !== undefined ? Boolean(raw.notifyOnVerify) : true,
    notifyOnFinish: raw.notifyOnFinish !== undefined ? Boolean(raw.notifyOnFinish) : true,
    notifyOnError: raw.notifyOnError !== undefined ? Boolean(raw.notifyOnError) : true,
    limitMaxTime: Number.isFinite(limitMaxTime) && limitMaxTime >= 0 ? limitMaxTime : 0,
    limitSpeed: Number.isFinite(speed) ? Math.min(1.8, Math.max(0.5, Number(speed.toFixed(1)))) : 1.0,
    soundOff: raw.soundOff !== undefined ? Boolean(raw.soundOff) : true,
    reviewMode: Boolean(raw.reviewMode),
    enableExamApi: raw.enableExamApi !== undefined ? Boolean(raw.enableExamApi) : true,
    enableReviewExamApi: Boolean(raw.enableReviewExamApi),
    enableZhidaoQuiz: raw.enableZhidaoQuiz !== undefined ? Boolean(raw.enableZhidaoQuiz) : true,
    enableMastery: raw.enableMastery !== undefined ? Boolean(raw.enableMastery) : true,
    masteryTarget: Number.isFinite(masteryTarget) && masteryTarget > 0
      ? Math.min(100, Math.max(1, Math.round(masteryTarget)))
      : 100,
    llm,
    courses,
    mirrors: raw.mirrors && typeof raw.mirrors === 'object' && Object.keys(raw.mirrors).length > 0
      ? raw.mirrors
      : base.mirrors,
  };
}

export function configSnapshot(cfg) {
  return JSON.stringify(normalizeConfig(cfg));
}
