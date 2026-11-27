import { inferLlmProvider, normalizeLlmApiFormat } from './llmProviders';

const URL_PATTERN = /^https:\/\/[-A-Za-z0-9+&@#/%?=~_|!:,.;]+[-A-Za-z0-9+&@#/%=~_|]$/i;
const PROXY_SCHEMES = new Set(['http:', 'https:', 'socks5:', 'socks5h:']);

function parseStrictUrl(raw, allowedSchemes) {
  const trimmed = String(raw || '').trim();
  if (!trimmed) return { empty: true, ok: true };
  // eslint-disable-next-line no-control-regex
  if (/[\s\u0000-\u001f\u007f]/.test(trimmed)) return { empty: false, ok: false };
  try {
    const rawUrl = new URL(trimmed);
    const protocol = rawUrl.protocol.toLowerCase();
    if (!allowedSchemes.has(protocol)) return { empty: false, ok: false };
    // Normalize socks5/socks5h to http: so WHATWG URL parser strictly validates hostname & port range (1..65535)
    const normalizedForAuthority =
      protocol === 'socks5:' || protocol === 'socks5h:'
        ? new URL(trimmed.replace(/^socks5h?:/i, 'http:'))
        : rawUrl;
    if (!normalizedForAuthority.hostname) return { empty: false, ok: false };
    if (normalizedForAuthority.port) {
      const portNum = Number(normalizedForAuthority.port);
      if (!Number.isInteger(portNum) || portNum < 1 || portNum > 65535) {
        return { empty: false, ok: false };
      }
    }
    return { empty: false, ok: true };
  } catch {
    return { empty: false, ok: false };
  }
}

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

export const QUESTION_BANK_SCOPES = [
  { value: 'zhidao_homework', label: '知到作业' },
  { value: 'zhidao_exam', label: '知到考试' },
  { value: 'ai_homework', label: 'AI 作业' },
  { value: 'ai_exam', label: 'AI 考试' },
];

const VALID_QUESTION_BANK_SCOPES = QUESTION_BANK_SCOPES.map((item) => item.value);
export const DEFAULT_QUESTION_BANK_SCOPES = ['zhidao_exam', 'ai_exam'];

export function normalizeQuestionBankScopes(raw) {
  if (raw === undefined || raw === null) {
    return [...DEFAULT_QUESTION_BANK_SCOPES];
  }
  let items;
  if (typeof raw === 'string') {
    items = raw
      .split(/[\s,;]+/)
      .map((s) => s.trim().toLowerCase())
      .filter(Boolean);
  } else if (Array.isArray(raw)) {
    items = raw.map((s) => String(s || '').trim().toLowerCase());
  } else {
    return [...DEFAULT_QUESTION_BANK_SCOPES];
  }
  const allowed = new Set(items.filter((s) => VALID_QUESTION_BANK_SCOPES.includes(s)));
  return VALID_QUESTION_BANK_SCOPES.filter((s) => allowed.has(s));
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
    streamTimeout: 120,
    streamIdleTimeout: 30,
    questionBankToken: '',
    questionBankUrl: 'https://tk.enncy.cn/query',
    questionBankScopes: [...DEFAULT_QUESTION_BANK_SCOPES],
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
    skipAiFreeExam: true,
    masteryTarget: 100,
    enablePptReference: true,
    enablePopupIntercept: true,
    enableHomeworkApi: true,
    homeworkThreshold: 100,
    homeworkMaxRedo: 3,
    enableFormalExamApi: true,
    forceRedoAnswered: false,
    logMaxSizeMb: 10,
    logRetentionDays: 14,
    logMaxFiles: 200,
    logCompressAfterDays: 3,
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
  return parseStrictUrl(raw, PROXY_SCHEMES).ok;
}

export function isValidLlmApiBase(raw) {
  return parseStrictUrl(raw, new Set(['http:', 'https:'])).ok;
}

export function detectCourseType(rawUrl) {
  const url = String(rawUrl || '').toLowerCase();
  if (/\/dohomework\//.test(url)) {
    return { key: 'homework', label: '知到作业', cls: 'bg-primary-container text-on-primary-container' };
  }
  if (/\/doexam\//.test(url)) {
    return { key: 'exam', label: '知到考试', cls: 'bg-error-container text-on-error-container' };
  }
  if (/\/testdetail\//.test(url)) {
    return { key: 'test-detail', label: 'AI 测试详情', cls: 'bg-tertiary-container text-on-tertiary-container' };
  }
  if (url.includes('ai-smart-course-student-pro.zhihuishu.com')) {
    return { key: 'ai-smart', label: 'AI 智慧课', cls: 'bg-tertiary-container text-on-tertiary-container' };
  }
  if (url.includes('wisdom-mooc.zhihuishu.com')) {
    return { key: 'ai-mooc', label: 'AI 慕课', cls: 'bg-tertiary-container text-on-tertiary-container' };
  }
  if (url.includes('fusioncourseh5.zhihuishu.com')) {
    return { key: 'fusion', label: '融合共享课', cls: 'bg-primary-container text-on-primary-container' };
  }
  if (
    url.includes('studywisdomh5.zhihuishu.com')
    || url.includes('hike.zhihuishu.com')
    || url.includes('wenda.zhihuishu.com/stu/courseinfo/studyresource')
  ) {
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
  const numberOrNaN = (value) => {
  if (value === undefined || value === null || value === '') return NaN;
  return Number(value);
};
  const speed = Number(raw.limitSpeed);
  const limitMaxTime = Number(raw.limitMaxTime);
  const masteryTarget = Number(raw.masteryTarget);
  const homeworkThreshold = numberOrNaN(raw.homeworkThreshold);
  const homeworkMaxRedo = numberOrNaN(raw.homeworkMaxRedo);
  const logMaxSizeMb = numberOrNaN(raw.logMaxSizeMb);
  const logRetentionDays = numberOrNaN(raw.logRetentionDays);
  const logMaxFiles = numberOrNaN(raw.logMaxFiles);
  const logCompressAfterDays = numberOrNaN(raw.logCompressAfterDays);
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
  const llmStreamTimeout = Number(rawLlm.streamTimeout ?? rawLlm.stream_timeout);
  const llmStreamIdleTimeout = Number(
    rawLlm.streamIdleTimeout ?? rawLlm.stream_idle_timeout
  );
  const llmStreamTotal =
    Number.isFinite(llmStreamTimeout) && llmStreamTimeout > 0 ? llmStreamTimeout : 120;
  const llmStreamIdle =
    Number.isFinite(llmStreamIdleTimeout) && llmStreamIdleTimeout > 0
      ? llmStreamIdleTimeout
      : 30;
  const rawBankScopes =
    'questionBankScopes' in rawLlm
      ? rawLlm.questionBankScopes
      : 'question_bank_scopes' in rawLlm
        ? rawLlm.question_bank_scopes
        : undefined;
  const llm = {
    provider: llmProvider,
    apiFormat: llmApiFormat,
    apiKey: String(rawLlm.apiKey ?? rawLlm.api_key ?? '').trim(),
    apiBase: llmApiBase,
    model: llmModel,
    visionModel: String(rawLlm.visionModel ?? rawLlm.vision_model ?? '').trim(),
    apiVersion: String(rawLlm.apiVersion ?? rawLlm.api_version ?? '').trim(),
    autoSubmit: Boolean(rawLlm.autoSubmit ?? rawLlm.auto_submit),
    streamTimeout: llmStreamTotal,
    streamIdleTimeout: Math.min(llmStreamIdle, llmStreamTotal),
    questionBankToken: String(
      rawLlm.questionBankToken ?? rawLlm.question_bank_token ?? ''
    ).trim(),
    questionBankUrl:
      String(rawLlm.questionBankUrl ?? rawLlm.question_bank_url ?? '').trim() ||
      'https://tk.enncy.cn/query',
    questionBankScopes: normalizeQuestionBankScopes(rawBankScopes),
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
    skipAiFreeExam:
      raw.skipAiFreeExam !== undefined
        ? Boolean(raw.skipAiFreeExam)
        : raw.skip_ai_free_exam !== undefined
          ? Boolean(raw.skip_ai_free_exam)
          : true,
    masteryTarget: Number.isFinite(masteryTarget) && masteryTarget > 0
      ? Math.min(100, Math.max(1, Math.round(masteryTarget)))
      : 100,
    enablePptReference:
      raw.enablePptReference !== undefined ? Boolean(raw.enablePptReference) : true,
    enablePopupIntercept:
      raw.enablePopupIntercept !== undefined ? Boolean(raw.enablePopupIntercept) : true,
    enableHomeworkApi:
      raw.enableHomeworkApi !== undefined ? Boolean(raw.enableHomeworkApi) : true,
    homeworkThreshold:
      Number.isFinite(homeworkThreshold)
        ? Math.min(100, Math.max(0, Math.round(homeworkThreshold)))
        : 100,
    homeworkMaxRedo:
      Number.isFinite(homeworkMaxRedo) && homeworkMaxRedo >= 0
        ? Math.max(0, Math.round(homeworkMaxRedo))
        : 3,
    enableFormalExamApi:
      raw.enableFormalExamApi !== undefined ? Boolean(raw.enableFormalExamApi) : true,
    forceRedoAnswered: Boolean(raw.forceRedoAnswered),
    logMaxSizeMb:
      Number.isFinite(logMaxSizeMb) && logMaxSizeMb > 0 ? Math.round(logMaxSizeMb) : 10,
    logRetentionDays:
      Number.isFinite(logRetentionDays) && logRetentionDays >= 0
        ? Math.round(logRetentionDays)
        : 14,
    logMaxFiles:
      Number.isFinite(logMaxFiles) && logMaxFiles > 0 ? Math.round(logMaxFiles) : 200,
    logCompressAfterDays:
      Number.isFinite(logCompressAfterDays) && logCompressAfterDays >= 0
        ? Math.round(logCompressAfterDays)
        : 3,
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
