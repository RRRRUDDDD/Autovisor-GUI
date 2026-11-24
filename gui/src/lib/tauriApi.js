import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import { defaultConfig } from './configModel';
import { getLlmProvider, isLocalLlmEndpoint } from './llmProviders';

export function isTauriEnv() {
  return typeof window !== 'undefined' && Boolean(window.__TAURI_INTERNALS__);
}

const MOCK_SCHOOLS = [
  { schoolId: '1001', name: '清华大学', letter: 'q' },
  { schoolId: '1002', name: '北京大学', letter: 'b' },
  { schoolId: '1003', name: '浙江大学', letter: 'z' },
  { schoolId: '1004', name: '复旦大学', letter: 'f' },
  { schoolId: '1005', name: '上海交通大学', letter: 's' },
  { schoolId: '1006', name: '电子科技大学', letter: 'd' },
];

const MOCK_DISCOVERED_COURSES = [
  {
    id: 'auto-demo-1',
    title: '高等数学（上）',
    url: 'https://studyvideoh5.zhihuishu.com/stuStudy?recruitAndCourseId=demo0001',
    enabled: true,
    teacher: '李教授',
    school: '示范大学',
    progress: '42%',
    courseType: '智慧共享课',
    autoDetected: true,
  },
  {
    id: 'auto-demo-2',
    title: '人工智能导论',
    url: 'https://fusioncourseh5.zhihuishu.com/stuStudy?recruitAndCourseId=demo0002',
    enabled: true,
    teacher: '王老师',
    school: '示范大学',
    progress: '15%',
    courseType: '融合共享课',
    autoDetected: true,
  },
];

// Browser-only mock state so `npm run dev` / Vitest works cleanly outside Tauri.
const mockState = {
  runtime: {
    backendPhase: 'ready',
    backendError: '',
    platform: 'windows',
    baseDir: 'C:\\Autovisor\\userdata',
    configPath: 'C:\\Autovisor\\userdata\\config.ini',
    logsDir: 'C:\\Autovisor\\userdata\\logs',
    cookieExists: false,
    cookieCount: 0,
    cookieModifiedAt: null,
    version: '1.1.0',
    backendVersion: '3.20.0(Autovisor GUI Edition)',
    closeBehavior: 'ask',
    detectedBrowsers: [
      {
        id: 'chrome',
        name: 'Google Chrome',
        driver: 'Chrome',
        exePath: 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
        source: 'registry',
        recommended: true,
      },
    ],
    session: null,
    savedLogin: null,
  },
  config: {
    ...defaultConfig(),
    driver: 'Chrome',
    exePath: 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  },
  runState: {
    status: 'idle',
    mode: 'study',
    reviewMode: false,
    shutdownOnComplete: false,
    shutdown: null,
    startedAt: null,
    finishedAt: null,
    exitCode: null,
    stage: '就绪',
    courseIndex: 0,
    courseTotal: 0,
    currentCourse: '',
    catalogType: '',
    currentLesson: '',
    lessonProgress: 0,
    progressDesc: '',
    remainingMinutes: null,
    courseElapsedMin: 0,
    completedLessons: [],
    needsAttention: '',
    errorMessage: '',
    logFilePath: '',
    logs: [],
    logsTruncated: false,
  },
  logFiles: [],
};

async function call(cmd, args = {}) {
  if (isTauriEnv()) {
    return invoke(cmd, args);
  }
  switch (cmd) {
    case 'get_runtime_info':
      return structuredClone(mockState.runtime);
    case 'restart_backend':
      mockState.runtime.backendPhase = 'ready';
      mockState.runtime.backendError = '';
      return structuredClone(mockState.runtime);
    case 'search_schools': {
      const q = String(args.query || '').trim().toLowerCase();
      if (!q) return structuredClone(MOCK_SCHOOLS);
      return structuredClone(
        MOCK_SCHOOLS.filter((s) => s.name.toLowerCase().includes(q) || s.letter === q)
      );
    }
    case 'zhs_login': {
      const req = args.request || {};
      if (req.mode === 'cookie' && !mockState.runtime.cookieExists) {
        throw new Error('本地暂无保存的登录凭证');
      }
      const displayName =
        req.mode === 'student'
          ? req.studentId || '演示学号'
          : req.mode === 'phone'
            ? req.username || '138****0000'
            : '演示学员';
      const profile = {
        loggedIn: true,
        displayName,
        account: req.mode === 'student' ? req.studentId || '' : req.username || '',
        loginMode: req.mode || 'phone',
        schoolName: req.schoolName || '示范大学',
        schoolId: req.schoolId || '1001',
        lastSyncedAt: Math.floor(Date.now() / 1000),
      };
      mockState.runtime.cookieExists = true;
      mockState.runtime.cookieCount = 12;
      mockState.runtime.session = profile;
      if (req.mode && req.mode !== 'cookie') {
        const prevSaved = mockState.runtime.savedLogin || {
          loginMode: 'phone',
          rememberPhone: false,
          phoneUsername: '',
          phonePassword: '',
          rememberStudent: false,
          schoolId: '',
          schoolName: '',
          studentId: '',
          studentPassword: '',
        };
        const nextSaved = { ...prevSaved, loginMode: req.mode };
        if (req.mode === 'phone') {
          if (req.rememberCredentials) {
            nextSaved.rememberPhone = true;
            nextSaved.phoneUsername = String(req.username || '').trim();
            nextSaved.phonePassword = String(req.password || '');
          } else {
            nextSaved.rememberPhone = false;
            nextSaved.phoneUsername = '';
            nextSaved.phonePassword = '';
          }
        } else if (req.mode === 'student') {
          if (req.rememberCredentials) {
            nextSaved.rememberStudent = true;
            nextSaved.schoolId = String(req.schoolId || '').trim();
            nextSaved.schoolName = String(req.schoolName || '').trim();
            nextSaved.studentId = String(req.studentId || '').trim();
            nextSaved.studentPassword = String(req.password || '');
          } else {
            nextSaved.rememberStudent = false;
            nextSaved.schoolId = '';
            nextSaved.schoolName = '';
            nextSaved.studentId = '';
            nextSaved.studentPassword = '';
          }
        }
        mockState.runtime.savedLogin = nextSaved;
      }
      mockState.config.courses = structuredClone(MOCK_DISCOVERED_COURSES);
      return {
        profile: structuredClone(profile),
        courses: structuredClone(mockState.config.courses),
        cookieCount: 12,
      };
    }
    case 'cancel_zhs_login':
      return null;
    case 'refresh_courses': {
      mockState.config.courses = structuredClone(MOCK_DISCOVERED_COURSES);
      return {
        profile: structuredClone(
          mockState.runtime.session || {
            loggedIn: true,
            displayName: '演示学员',
            account: '',
            loginMode: 'cookie',
            schoolName: '示范大学',
            schoolId: '',
            lastSyncedAt: Math.floor(Date.now() / 1000),
          }
        ),
        courses: structuredClone(mockState.config.courses),
        cookieCount: mockState.runtime.cookieCount || 12,
      };
    }
    case 'logout_zhs':
      mockState.runtime.cookieExists = false;
      mockState.runtime.cookieCount = 0;
      mockState.runtime.session = null;
      return structuredClone(mockState.runtime);
    case 'clear_saved_login': {
      const m = String(args.mode || '').trim();
      if (m === 'phone' && mockState.runtime.savedLogin) {
        mockState.runtime.savedLogin = {
          ...mockState.runtime.savedLogin,
          rememberPhone: false,
          phoneUsername: '',
          phonePassword: '',
        };
      } else if (m === 'student' && mockState.runtime.savedLogin) {
        mockState.runtime.savedLogin = {
          ...mockState.runtime.savedLogin,
          rememberStudent: false,
          schoolId: '',
          schoolName: '',
          studentId: '',
          studentPassword: '',
        };
      } else {
        mockState.runtime.savedLogin = null;
      }
      return structuredClone(mockState.runtime);
    }
    case 'load_config':
      return structuredClone(mockState.config);
    case 'save_config':
      mockState.config = structuredClone(args.config);
      return structuredClone(mockState.config);
    case 'get_run_state':
      return structuredClone(mockState.runState);
    case 'start_autovisor':
      mockState.runState = {
        ...mockState.runState,
        status: 'running',
        mode: 'study',
        reviewMode: Boolean(mockState.config.reviewMode),
        shutdownOnComplete: Boolean(mockState.config.shutdownOnComplete),
        shutdown: null,
        startedAt: Date.now() / 1000,
        finishedAt: null,
        stage: '初始化',
        logs: [{ seq: 1, timestamp: Date.now() / 1000, level: 'info', message: '[演示模式] 正在启动 Autovisor GUI...' }],
      };
      return structuredClone(mockState.runState);
    case 'stop_autovisor':
      mockState.runState = { ...mockState.runState, status: 'cancelled', finishedAt: Date.now() / 1000 };
      return structuredClone(mockState.runState);
    case 'cancel_shutdown':
      mockState.runState = {
        ...mockState.runState,
        shutdownOnComplete: false,
        shutdown: { state: 'cancelled' },
      };
      return structuredClone(mockState.runState);
    case 'run_diagnostic':
      mockState.runState = {
        ...mockState.runState,
        status: 'completed',
        mode: args.kind || 'check_browser',
        stage: '诊断完成',
        logs: [{ seq: 1, timestamp: Date.now() / 1000, level: 'success', message: '[演示模式] 诊断命令已执行' }],
      };
      return structuredClone(mockState.runState);
    case 'list_log_files':
      return structuredClone(mockState.logFiles);
    case 'clear_cookies':
      mockState.runtime.cookieExists = false;
      mockState.runtime.cookieCount = 0;
      mockState.runtime.session = null;
      return structuredClone(mockState.runtime);
    case 'set_close_behavior': {
      if (!['ask', 'minimize', 'tray', 'exit'].includes(args.behavior)) {
        throw new Error('不支持的关闭行为选项');
      }
      mockState.runtime.closeBehavior = args.behavior;
      return structuredClone(mockState.runtime);
    }
    case 'close_prompt_shown':
      return true;
    case 'close_choice': {
      const action = args.action;
      if (args.remember && ['minimize', 'tray', 'exit'].includes(action)) {
        mockState.runtime.closeBehavior = action;
      }
      return structuredClone(mockState.runtime);
    }
    case 'test_notification': {
      const provider = String(args.provider || '').trim();
      const url = String(args.url || '').trim();
      const tgChatId = String(args.tgChatId || '').trim();
      if (!provider) {
        throw new Error('请先选择通知服务');
      }
      if (provider === 'Windows') {
        return '[演示模式] Windows 系统通知发送成功';
      }
      if (!url) {
        throw new Error('请先填写有效的通知 URL');
      }
      if (provider === 'Telegram' && !tgChatId) {
        throw new Error('使用 Telegram 通知时必须填写 Chat ID');
      }
      const labels = {
        ServerChan: 'Server酱',
        Qmsg: 'Qmsg酱',
        Bark: 'Bark',
        Telegram: 'Telegram',
      };
      return `[演示模式] ${labels[provider] || provider}通知发送成功`;
    }
    case 'test_llm_connection': {
      const llm = args.llm || {};
      const apiBase = String(llm.apiBase || '').trim();
      const model = String(llm.model || '').trim();
      const apiKey = String(llm.apiKey || '').trim();
      if (!/^https?:\/\//i.test(apiBase)) {
        throw new Error('API Base URL 格式无效，请以 http:// 或 https:// 开头');
      }
      if (!model) {
        throw new Error('请先填写模型名称');
      }
      if (!apiKey && !isLocalLlmEndpoint(llm)) {
        throw new Error('请先填写 API Key（本地 Ollama / LM Studio 可留空）');
      }
      return `[演示模式] 连接成功（128ms），模型 ${model} 回复：OK`;
    }
    case 'test_question_bank': {
      const token = String(args.token || '').trim();
      if (!token) {
        throw new Error('未配置第三方题库 Token');
      }
      return '[演示模式] 题库连接正常, 剩余 100 次';
    }
    case 'export_question_cache':
      return 'C:\\Autovisor\\autovisor-question-bank.json';
    case 'import_question_cache':
      return { ok: true, zhidao_questions: 0, ai_questions: 0 };
    case 'clear_question_cache':
      return { ok: true, zhidao_questions: 0, ai_questions: 0, json_files: 0 };
    case 'fetch_llm_models': {
      const llm = args.llm || {};
      const apiKey = String(llm.apiKey || '').trim();
      if (!apiKey && !isLocalLlmEndpoint(llm)) {
        throw new Error('请先填写 API Key 后再拉取模型列表');
      }
      const preset = getLlmProvider(llm.provider);
      return preset?.models?.length
        ? [...preset.models]
        : ['gpt-4o-mini', 'deepseek-chat', 'claude-sonnet-4-20250514'];
    }
    case 'set_debug_log':
      return Boolean(args.enabled);
    case 'pick_directory':
      return 'C:\\Autovisor\\userdata';
    case 'start_qr_login':
      return {
        ok: true,
        sessionId: 'mock-qr-session',
        // Minimal 1x1 white PNG
        imageBase64:
          'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+X2dIAAAAASUVORK5CYII=',
        savedPath: '',
      };
    case 'poll_qr_login':
      return { ok: true, status: 'waiting' };
    case 'save_qr_image':
      return {
        ok: true,
        savedPath: `${args.saveDir || 'C:\\temp'}\\zhs-qr-demo.png`,
      };
    case 'cancel_qr_login':
      return null;
    case 'check_for_update':
      return {
        ok: true,
        currentVersion: mockState.runtime.version,
        latestVersion: mockState.runtime.version,
        hasUpdate: false,
        releaseName: `Autovisor GUI v${mockState.runtime.version}`,
        releaseUrl: 'https://github.com/RRRRUDDDD/Autovisor-GUI/releases/latest',
        downloadUrl: '',
        notes: '',
        publishedAt: '',
      };
    default:
      return null;
  }
}

export const tauriApi = {
  getRuntimeInfo: (opts) =>
    opts?.refreshBrowsers
      ? call('get_runtime_info', { refreshBrowsers: true })
      : call('get_runtime_info'),
  restartBackend: () => call('restart_backend'),
  searchSchools: (query = '', proxy = '') => call('search_schools', { query, proxy: proxy || null }),
  zhsLogin: (request) => call('zhs_login', { request }),
  cancelZhsLogin: () => call('cancel_zhs_login'),
  refreshCourses: () => call('refresh_courses'),
  logoutZhs: () => call('logout_zhs'),
  clearSavedLogin: (mode = '') => call('clear_saved_login', { mode: mode || null }),
  pickBrowserExe: () => call('pick_browser_exe'),
  pickCookieFile: () => call('pick_cookie_file'),
  pickDirectory: () => call('pick_directory'),
  loadConfig: () => call('load_config'),
  saveConfig: (config) => call('save_config', { config }),
  getRunState: () => call('get_run_state'),
  startAutovisor: () => call('start_autovisor'),
  stopAutovisor: () => call('stop_autovisor'),
  cancelShutdown: () => call('cancel_shutdown'),
  runDiagnostic: (kind, target = '') => call('run_diagnostic', { kind, target }),
  testNotification: ({ provider = '', url = '', tgChatId = '', proxy = '' }) =>
    call('test_notification', { provider, url, tgChatId, proxy }),
  testLlmConnection: ({ llm, proxy = '' }) =>
    call('test_llm_connection', { llm, proxy }),
  testQuestionBank: ({ token = '', url = '', proxy = '' }) =>
    call('test_question_bank', { token, url, proxy }),
  exportQuestionCache: (courseId = '') =>
    call('export_question_cache', { courseId: courseId || null }),
  importQuestionCache: () => call('import_question_cache'),
  clearQuestionCache: (courseId = '') =>
    call('clear_question_cache', { courseId: courseId || null }),
  fetchLlmModels: ({ llm, proxy = '' }) =>
    call('fetch_llm_models', { llm, proxy }),
  setDebugLog: (enabled) => call('set_debug_log', { enabled: Boolean(enabled) }),
  startQrLogin: (proxy = '', saveDir = '') =>
    call('start_qr_login', { proxy, saveDir: saveDir || null }),
  pollQrLogin: (sessionId) => call('poll_qr_login', { sessionId }),
  saveQrImage: (sessionId, saveDir) => call('save_qr_image', { sessionId, saveDir }),
  cancelQrLogin: (sessionId) => call('cancel_qr_login', { sessionId }),
  checkForUpdate: (proxy = '') => call('check_for_update', { proxy }),
  clearCookies: () => call('clear_cookies'),
  listLogFiles: () => call('list_log_files'),
  readLogFile: (path) => call('read_log_file', { path }),
  openPath: (target) => call('open_path', { target }),
  openUrl: (url) => call('open_url', { url }),
  setCloseBehavior: (behavior) => call('set_close_behavior', { behavior }),
  closePromptShown: (promptId) => call('close_prompt_shown', { promptId }),
  closeChoice: (action, remember) => call('close_choice', { action, remember }),
  listenCloseRequested: async (handler) => {
    if (!isTauriEnv()) return () => {};
    return listen('close-requested', (event) => handler(event.payload));
  },
  listenRunState: async (handler) => {
    if (!isTauriEnv()) return () => {};
    return listen('autovisor-state', (event) => handler(event.payload));
  },
  listenBackendStatus: async (handler) => {
    if (!isTauriEnv()) return () => {};
    return listen('autovisor-backend-status', (event) => handler(event.payload));
  },
  listenLoginStatus: async (handler) => {
    if (!isTauriEnv()) return () => {};
    return listen('autovisor-login-status', (event) => handler(event.payload));
  },
};
