import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  GraduationCap,
  Github,
  Loader2,
  AlertCircle,
  MonitorPlay,
  BookOpen,
  RefreshCw,
  LogOut,
  UserCheck,
  Settings2,
  Sun,
  Moon,
  Play,
  Download,
  X,
} from 'lucide-react';
import Button from './components/ui/Button';
import LoginView from './components/LoginView';
import WorkspaceView from './components/WorkspaceView';
import SettingsView from './components/SettingsView';
import ProgressView from './components/ProgressView';
import CloseChoiceDialog from './components/CloseChoiceDialog';
import { ScheduledShutdownBanner } from './components/ShutdownBanner';
import { tauriApi } from './lib/tauriApi';
import {
  configSnapshot,
  defaultConfig,
  isValidLlmApiBase,
  isValidProxyUrl,
  normalizeConfig,
} from './lib/configModel';
import { useTheme } from './lib/theme';

const AUTO_SAVE_DELAY_MS = 250;

const NAV_ITEMS = [
  { id: 'study', label: '自动学习', heading: '选择课程并配置刷课参数', icon: BookOpen },
  { id: 'progress', label: '运行监控', heading: '实时学习进度与执行日志', icon: MonitorPlay },
  { id: 'settings', label: '系统设置', heading: 'AI 答题、浏览器、推送通知与凭证日志', icon: Settings2 },
];

export default function App() {
  const { isDark, toggleLightDark } = useTheme();
  const [view, setView] = useState('login'); // 'login' | 'shell'
  const [activeTab, setActiveTab] = useState('study'); // 'study' | 'progress' | 'settings'
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');
  const [runtime, setRuntime] = useState(null);
  const [session, setSession] = useState(null);
  const [autoRestoreCookie, setAutoRestoreCookie] = useState(true);
  const [config, setConfig] = useState(defaultConfig);
  const [savedSnapshot, setSavedSnapshot] = useState(null);
  const [saving, setSaving] = useState(false);
  const [saveStatus, setSaveStatus] = useState(null);
  const [refreshingCourses, setRefreshingCourses] = useState(false);
  const [courseSyncNotice, setCourseSyncNotice] = useState(null);
  const [starting, setStarting] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [cancellingShutdown, setCancellingShutdown] = useState(false);
  const [cancelShutdownError, setCancelShutdownError] = useState('');
  const [loggingOut, setLoggingOut] = useState(false);
  const [restartingBackend, setRestartingBackend] = useState(false);
  const [updateInfo, setUpdateInfo] = useState(null);
  const [checkingUpdate, setCheckingUpdate] = useState(false);
  const [updateError, setUpdateError] = useState('');
  const [updateDismissed, setUpdateDismissed] = useState(false);
  const [startError, setStartError] = useState('');
  const [runState, setRunState] = useState(null);
  const [cookieImportState, setCookieImportState] = useState({
    importing: false,
    error: '',
    nonce: 0,
  });

  const latestConfigRef = useRef(config);
  latestConfigRef.current = config;
  const savedSnapshotRef = useRef(savedSnapshot);
  savedSnapshotRef.current = savedSnapshot;
  const autoSaveTimerRef = useRef(null);
  const savePromiseRef = useRef(null);
  const mountedRef = useRef(true);
  const autoUpdateCheckedRef = useRef(false);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      if (autoSaveTimerRef.current) {
        clearTimeout(autoSaveTimerRef.current);
        autoSaveTimerRef.current = null;
      }
    };
  }, []);

  const applyBackendConfig = useCallback((nextConfig) => {
    if (autoSaveTimerRef.current) {
      clearTimeout(autoSaveTimerRef.current);
      autoSaveTimerRef.current = null;
    }
    const normalized = normalizeConfig(nextConfig);
    const snap = configSnapshot(normalized);
    latestConfigRef.current = normalized;
    savedSnapshotRef.current = snap;
    setConfig(normalized);
    setSavedSnapshot(snap);
    return normalized;
  }, []);

  const loadAll = useCallback(async () => {
    setLoading(true);
    setLoadError('');
    try {
      const [rt, cfg, st] = await Promise.all([
        tauriApi.getRuntimeInfo(),
        tauriApi.loadConfig(),
        tauriApi.getRunState(),
      ]);
      setRuntime(rt);
      applyBackendConfig(cfg);
      setRunState(st);
      if (st?.status === 'running') {
        setSession(rt?.session || { loggedIn: true, displayName: '智慧树学员' });
        setView('shell');
        setActiveTab('progress');
      }
    } catch (err) {
      setLoadError(err?.message || String(err) || '初始化配置失败');
    } finally {
      setLoading(false);
    }
  }, [applyBackendConfig]);

  useEffect(() => {
    loadAll();
  }, [loadAll]);

  useEffect(() => {
    let unlistenState = () => {};
    let unlistenBackend = () => {};
    let active = true;
    tauriApi
      .listenRunState((nextState) => {
        if (!active) return;
        setRunState(nextState);
        if (nextState?.shutdown?.state !== 'scheduled' && !nextState?.shutdownOnComplete) {
          setCancelShutdownError('');
        }
        if (nextState?.status !== 'running') {
          setStopping(false);
          tauriApi
            .getRuntimeInfo()
            .then((rt) => {
              if (!active) return;
              if (rt) setRuntime(rt);
              if (nextState?.mode === 'import_cookies') {
                if (nextState.status === 'completed') {
                  setAutoRestoreCookie(true);
                  setCookieImportState((prev) => ({
                    importing: false,
                    error: '',
                    nonce: prev.nonce + 1,
                  }));
                } else if (nextState.status === 'error') {
                  setCookieImportState((prev) => ({
                    ...prev,
                    importing: false,
                    error: nextState.errorMessage || '导入 Cookie 失败，请检查文件格式',
                  }));
                } else {
                  setCookieImportState((prev) => ({ ...prev, importing: false }));
                }
              }
            })
            .catch(() => {
              if (active && nextState?.mode === 'import_cookies') {
                setCookieImportState((prev) => ({ ...prev, importing: false }));
              }
            });
        }
      })
      .then((fn) => {
        if (active) unlistenState = fn;
        else fn();
      });
    tauriApi
      .listenBackendStatus((status) => {
        if (!active || !status) return;
        setRuntime((prev) =>
          prev
            ? {
                ...prev,
                backendPhase: status.phase || prev.backendPhase,
                backendError: status.error || '',
                version: status.version || prev.version,
                backendVersion: status.backendVersion || prev.backendVersion,
              }
            : prev
        );
      })
      .then((fn) => {
        if (active) unlistenBackend = fn;
        else fn();
      });
    return () => {
      active = false;
      unlistenState();
      unlistenBackend();
    };
  }, []);

  const currentSnapshot = useMemo(() => configSnapshot(config), [config]);
  const dirty = savedSnapshot !== null && currentSnapshot !== savedSnapshot;
  const visibleSaveStatus = saveStatus && saveStatus.snapshot === currentSnapshot ? saveStatus : null;
  const configValidForSave = useMemo(
    () => isValidProxyUrl(config.proxy) && isValidLlmApiBase(config.llm?.apiBase),
    [config.proxy, config.llm?.apiBase]
  );

  const persistConfigNow = useCallback(async ({ force = false } = {}) => {
    if (autoSaveTimerRef.current) {
      clearTimeout(autoSaveTimerRef.current);
      autoSaveTimerRef.current = null;
    }

    if (savePromiseRef.current) {
      try {
        await savePromiseRef.current;
      } catch {
        // Error state is handled by the active save run.
      }
    }

    const runLoop = async () => {
      let forceFirst = force;
      while (mountedRef.current) {
        const target = latestConfigRef.current;
        const normalized = normalizeConfig(target);
        const snap = configSnapshot(normalized);

        if (!forceFirst && savedSnapshotRef.current === snap) {
          return true;
        }
        if (!isValidProxyUrl(target.proxy) || !isValidLlmApiBase(target.llm?.apiBase)) {
          return false;
        }

        forceFirst = false;
        setSaving(true);
        setSaveStatus((prev) => (prev?.type === 'error' ? null : prev));
        try {
          await tauriApi.saveConfig(normalized);
          if (!mountedRef.current) return true;
          savedSnapshotRef.current = snap;
          setSavedSnapshot(snap);
          setSaveStatus({ type: 'success', message: '配置已自动保存', snapshot: snap });
          if (configSnapshot(latestConfigRef.current) === snap) {
            return true;
          }
        } catch (err) {
          if (!mountedRef.current) return false;
          setSaveStatus({
            type: 'error',
            message: err?.message || String(err) || '自动保存配置失败，请检查文件权限',
            snapshot: snap,
          });
          return false;
        } finally {
          if (mountedRef.current) {
            setSaving(false);
          }
        }
      }
      return true;
    };

    const promise = runLoop();
    savePromiseRef.current = promise;
    try {
      return await promise;
    } finally {
      if (savePromiseRef.current === promise) {
        savePromiseRef.current = null;
      }
    }
  }, []);

  const handleRetrySave = useCallback(
    () => persistConfigNow({ force: true }),
    [persistConfigNow]
  );

  useEffect(() => {
    if (autoSaveTimerRef.current) {
      clearTimeout(autoSaveTimerRef.current);
      autoSaveTimerRef.current = null;
    }
    if (savedSnapshot === null || currentSnapshot === savedSnapshot || !configValidForSave) {
      return undefined;
    }
    if (saveStatus?.type === 'error' && saveStatus.snapshot === currentSnapshot) {
      return undefined;
    }
    autoSaveTimerRef.current = setTimeout(() => {
      autoSaveTimerRef.current = null;
      void persistConfigNow();
    }, AUTO_SAVE_DELAY_MS);
    return () => {
      if (autoSaveTimerRef.current) {
        clearTimeout(autoSaveTimerRef.current);
        autoSaveTimerRef.current = null;
      }
    };
  }, [currentSnapshot, savedSnapshot, configValidForSave, saveStatus, persistConfigNow]);

  const handleLoginSuccess = useCallback(
    async (result) => {
      const [rt, cfg] = await Promise.all([tauriApi.getRuntimeInfo(), tauriApi.loadConfig()]);
      setRuntime(rt);
      const normalized = applyBackendConfig({
        ...cfg,
        courses: Array.isArray(result?.courses) ? result.courses : cfg?.courses || [],
      });
      setSession(result?.profile || rt?.session || { loggedIn: true, displayName: '智慧树学员' });
      setSaveStatus(null);
      setStartError('');
      setCourseSyncNotice(
        normalized.courses.length > 0
          ? { type: 'success', message: `已自动识别 ${normalized.courses.length} 门课程` }
          : { type: 'info', message: '登录成功，暂未识别到进行中的课程，可点击「刷新课程」重试' }
      );
      setActiveTab('study');
      setView('shell');
    },
    [applyBackendConfig]
  );

  const handleRefreshCourses = useCallback(async () => {
    setRefreshingCourses(true);
    setCourseSyncNotice(null);
    setStartError('');
    try {
      await persistConfigNow();
      const res = await tauriApi.refreshCourses();
      const [rt, cfg] = await Promise.all([tauriApi.getRuntimeInfo(), tauriApi.loadConfig()]);
      setRuntime(rt);
      const normalized = applyBackendConfig({
        ...cfg,
        courses: Array.isArray(res?.courses) ? res.courses : cfg?.courses || [],
      });
      if (res?.profile) setSession(res.profile);
      setCourseSyncNotice(
        normalized.courses.length > 0
          ? {
              type: 'success',
              message: `课程队列已同步，共识别到 ${normalized.courses.length} 门课程`,
            }
          : {
              type: 'info',
              message: '已完成同步，暂未识别到进行中的课程，可重试或手动补充课程链接',
            }
      );
    } catch (err) {
      const msg = err?.message || String(err) || '刷新课程失败';
      setCourseSyncNotice({ type: 'error', message: msg });
    } finally {
      setRefreshingCourses(false);
    }
  }, [applyBackendConfig, persistConfigNow]);

  const handleLogout = useCallback(async () => {
    if (loggingOut) return;
    setLoggingOut(true);
    setStartError('');
    try {
      await persistConfigNow();
      if (runState?.status === 'running') {
        const stopped = await tauriApi.stopAutovisor();
        setRunState(stopped);
      }
      const rt = await tauriApi.logoutZhs();
      setRuntime(rt);
      setSession(null);
      setAutoRestoreCookie(false);
      setCourseSyncNotice(null);
      setSaveStatus(null);
      setView('login');
    } catch (err) {
      setStartError(err?.message || String(err) || '退出登录失败');
    } finally {
      setLoggingOut(false);
    }
  }, [loggingOut, persistConfigNow, runState?.status]);

  const handleStartStudy = useCallback(async () => {
    setStartError('');
    setStarting(true);
    try {
      const ok = await persistConfigNow();
      if (!ok) {
        setStartError('配置保存失败，已取消启动');
        return;
      }
      const nextState = await tauriApi.startAutovisor();
      setRunState(nextState);
      setActiveTab('progress');
    } catch (err) {
      setStartError(err?.message || String(err) || '启动 Autovisor GUI 失败');
    } finally {
      setStarting(false);
    }
  }, [persistConfigNow]);

  const handleStop = useCallback(async () => {
    setStopping(true);
    try {
      const nextState = await tauriApi.stopAutovisor();
      setRunState(nextState);
    } catch (err) {
      setStartError(err?.message || String(err) || '停止任务失败');
    } finally {
      setStopping(false);
    }
  }, []);

  const handleCancelShutdown = useCallback(async () => {
    if (cancellingShutdown) return;
    setCancellingShutdown(true);
    setCancelShutdownError('');
    try {
      const nextState = await tauriApi.cancelShutdown();
      if (nextState) setRunState(nextState);
    } catch (err) {
      setCancelShutdownError(
        err?.message || String(err) || '取消关机失败，可在命令提示符运行 shutdown /a 手动取消'
      );
    } finally {
      setCancellingShutdown(false);
    }
  }, [cancellingShutdown]);

  const handleChangeCloseBehavior = useCallback(async (behavior) => {
    const rt = await tauriApi.setCloseBehavior(behavior);
    if (rt) setRuntime(rt);
    return rt;
  }, []);

  const handleRunCourseCheck = useCallback(
    async (url) => {
      setStartError('');
      try {
        await persistConfigNow();
        const nextState = await tauriApi.runDiagnostic('check_course', url);
        setRunState(nextState);
        setActiveTab('progress');
      } catch (err) {
        setStartError(err?.message || String(err) || '启动课程检查失败');
      }
    },
    [persistConfigNow]
  );

  const handleCheckBrowser = useCallback(async () => {
    setStartError('');
    try {
      await persistConfigNow();
      const nextState = await tauriApi.runDiagnostic('check_browser', '');
      setRunState(nextState);
      setActiveTab('progress');
    } catch (err) {
      setStartError(err?.message || String(err) || '启动浏览器检测失败');
    }
  }, [persistConfigNow]);

  const handleOpenPath = useCallback((target) => tauriApi.openPath(target), []);
  const handleListLogFiles = useCallback(() => tauriApi.listLogFiles(), []);
  const handleReadLogFile = useCallback((path) => tauriApi.readLogFile(path), []);

  const handleRestartBackend = useCallback(async () => {
    if (restartingBackend) return;
    setRestartingBackend(true);
    setStartError('');
    try {
      const rt = await tauriApi.restartBackend();
      setRuntime(rt);
    } catch (err) {
      setStartError(err?.message || String(err) || '重启内置学习引擎失败');
      const rt = await tauriApi.getRuntimeInfo().catch(() => null);
      if (rt) setRuntime(rt);
    } finally {
      setRestartingBackend(false);
    }
  }, [restartingBackend]);

  const handleSelectBrowser = useCallback((browser) => {
    if (!browser) return;
    setConfig((prev) => ({
      ...prev,
      driver: browser.driver || prev.driver,
      exePath: browser.exePath || '',
    }));
  }, []);

  const handleRedetectBrowsers = useCallback(async () => {
    try {
      const rt = await tauriApi.getRuntimeInfo({ refreshBrowsers: true });
      setRuntime(rt);
      const first = rt?.detectedBrowsers?.[0];
      if (first) {
        setConfig((prev) => ({
          ...prev,
          driver: first.driver,
          exePath: first.exePath,
        }));
      }
    } catch {
      // ignore
    }
  }, []);

  const handlePickBrowserExe = useCallback(async () => {
    try {
      const picked = await tauriApi.pickBrowserExe();
      if (picked) {
        const lower = picked.toLowerCase();
        const inferredDriver = lower.includes('msedge') ? 'Edge' : 'Chrome';
        setConfig((prev) => ({ ...prev, driver: inferredDriver, exePath: picked }));
      }
    } catch {
      // 用户取消选择
    }
  }, []);

  const handleSaveProxy = useCallback(
    async (nextProxy) => {
      const normalized = normalizeConfig({ ...latestConfigRef.current, proxy: nextProxy });
      latestConfigRef.current = normalized;
      setConfig(normalized);
      await persistConfigNow();
    },
    [persistConfigNow]
  );

  const handleImportCookies = useCallback(async () => {
    try {
      const picked = await tauriApi.pickCookieFile();
      if (!picked) return;
      if (view === 'login') {
        setAutoRestoreCookie(true);
        setCookieImportState((prev) => ({ ...prev, importing: true, error: '' }));
      }
      const nextState = await tauriApi.runDiagnostic('import_cookies', picked);
      setRunState(nextState);
      if (view !== 'login') {
        setActiveTab('progress');
      } else if (nextState?.status && nextState.status !== 'running') {
        const rt = await tauriApi.getRuntimeInfo().catch(() => null);
        if (rt) setRuntime(rt);
        if (nextState.status === 'completed') {
          setCookieImportState((prev) => ({
            importing: false,
            error: '',
            nonce: prev.nonce + 1,
          }));
        } else {
          setCookieImportState((prev) => ({
            ...prev,
            importing: false,
            error: nextState.errorMessage || '导入 Cookie 失败，请检查文件格式',
          }));
        }
      }
    } catch (err) {
      const msg = err?.message || String(err) || '导入 Cookie 失败';
      if (view === 'login') {
        setCookieImportState((prev) => ({ ...prev, importing: false, error: msg }));
      } else {
        setStartError(msg);
      }
    }
  }, [view]);

  const handleClearCookies = useCallback(async () => {
    try {
      const rt = await tauriApi.clearCookies();
      setRuntime(rt);
      setSession(null);
      setAutoRestoreCookie(false);
      setView('login');
    } catch (err) {
      setStartError(err?.message || String(err) || '清除 Cookie 失败');
    }
  }, []);

  const handleTestNotification = useCallback(async (payload) => {
    return tauriApi.testNotification(payload);
  }, []);

  const handleTestLlmConnection = useCallback(async (payload) => {
    return tauriApi.testLlmConnection(payload);
  }, []);

  const handleFetchLlmModels = useCallback(async (payload) => {
    return tauriApi.fetchLlmModels(payload);
  }, []);

  const handleTestQuestionBank = useCallback(async (payload) => {
    return tauriApi.testQuestionBank(payload);
  }, []);

  const handleExportQuestionCache = useCallback(async () => {
    return tauriApi.exportQuestionCache();
  }, []);

  const handleImportQuestionCache = useCallback(async () => {
    return tauriApi.importQuestionCache();
  }, []);

  const handleCheckUpdate = useCallback(async ({ silent = false } = {}) => {
    setCheckingUpdate(true);
    if (!silent) {
      setUpdateError('');
    }
    try {
      const proxy = isValidProxyUrl(latestConfigRef.current?.proxy)
        ? latestConfigRef.current.proxy || ''
        : '';
      const info = await tauriApi.checkForUpdate(proxy);
      if (!mountedRef.current) return info;
      if (info && typeof info === 'object') {
        setUpdateInfo(info);
        if (!silent) {
          setUpdateError('');
          if (info.hasUpdate) {
            setUpdateDismissed(false);
          }
        }
      }
      return info;
    } catch (err) {
      if (!mountedRef.current) return null;
      if (!silent) {
        setUpdateError(err?.message || String(err) || '检查更新失败，请稍后重试');
      }
      return null;
    } finally {
      if (mountedRef.current) {
        setCheckingUpdate(false);
      }
    }
  }, []);

  const handleOpenUrl = useCallback((url) => tauriApi.openUrl(url), []);

  const handleToggleDebugLog = useCallback((enabled) => {
    const next = Boolean(enabled);
    setConfig((prev) =>
      prev.enableDebugLog === next ? prev : { ...prev, enableDebugLog: next }
    );
    void tauriApi.setDebugLog(next).catch(() => {});
  }, []);

  useEffect(() => {
    if (loading || runtime?.backendPhase !== 'ready' || autoUpdateCheckedRef.current) {
      return;
    }
    autoUpdateCheckedRef.current = true;
    void handleCheckUpdate({ silent: true });
  }, [loading, runtime?.backendPhase, handleCheckUpdate]);

  const taskRunning = runState?.status === 'running';
  const shutdownScheduled = runState?.shutdown?.state === 'scheduled';
  const enabledCount = (config.courses || []).filter((c) => c.enabled).length;
  const canQuickStart =
    enabledCount > 0 &&
    isValidProxyUrl(config.proxy) &&
    isValidLlmApiBase(config.llm?.apiBase) &&
    !starting &&
    !taskRunning &&
    !refreshingCourses;

  const currentNav = NAV_ITEMS.find((item) => item.id === activeTab) || NAV_ITEMS[0];
  const userLabel = session?.displayName || session?.account || '已登录学员';
  const userSub =
    session?.schoolName && session.schoolName !== userLabel ? session.schoolName : '';

  let content;
  if (loading) {
    content = (
      <div className="flex min-h-screen flex-col items-center justify-center gap-4 bg-surface">
        <div className="flex h-14 w-14 items-center justify-center rounded-[20px] bg-primary-container text-on-primary-container">
          <Loader2 className="h-7 w-7 animate-spin" aria-hidden="true" />
        </div>
        <p className="text-sm text-on-surface-variant">正在检测本机浏览器与登录环境...</p>
      </div>
    );
  } else if (view === 'login') {
    content = (
      <>
        {shutdownScheduled && (
          <div className="bg-surface px-4 pt-4">
            <div className="mx-auto max-w-5xl">
              <ScheduledShutdownBanner
                shutdownInfo={runState.shutdown}
                cancelling={cancellingShutdown}
                cancelError={cancelShutdownError}
                onCancel={handleCancelShutdown}
                className="mb-0"
              />
            </div>
          </div>
        )}
        <LoginView
          runtime={runtime}
          config={config}
          autoRestoreCookie={autoRestoreCookie}
          cookieImportState={cookieImportState}
          isDark={isDark}
          onToggleTheme={toggleLightDark}
          onLoginSuccess={handleLoginSuccess}
          onSelectBrowser={handleSelectBrowser}
          onRedetectBrowsers={handleRedetectBrowsers}
          onPickBrowserExe={handlePickBrowserExe}
          onImportCookies={handleImportCookies}
          onSaveProxy={handleSaveProxy}
        />
      </>
    );
  } else {
    content = (
    <div className="m3-app-shell min-h-screen bg-surface text-on-surface pb-16 md:pb-0">
      {/* 左侧 M3 Navigation Rail（桌面端左侧 / 窄屏自动变为底部 Navigation Bar） */}
      <aside
        aria-label="主导航"
        className="fixed inset-x-0 bottom-0 z-30 flex h-16 shrink-0 flex-row items-center justify-around bg-surface-container px-2 shadow-elevation-2 md:static md:h-auto md:w-[88px] md:flex-col md:justify-between md:bg-surface md:px-0 md:py-4 md:shadow-none"
      >
        {/* 顶部品牌 + M3 FAB 快捷入口（桌面端） */}
        <div className="hidden flex-col items-center gap-3.5 md:flex">
          <div
            className="flex h-11 w-11 items-center justify-center rounded-2xl bg-primary-container text-on-primary-container"
            title="知到 · 智慧树自动化学习助手"
          >
            <GraduationCap className="h-6 w-6" aria-hidden="true" />
          </div>

          <button
            type="button"
            onClick={taskRunning ? () => setActiveTab('progress') : handleStartStudy}
            disabled={!taskRunning && !canQuickStart}
            title={
              taskRunning
                ? '查看运行中的学习任务'
                : canQuickStart
                  ? '立即开始刷课'
                  : '请先在「自动学习」勾选至少一门课程'
            }
            aria-label={taskRunning ? '查看运行中的学习任务' : '快捷开始刷课'}
            className={`flex h-14 w-14 items-center justify-center rounded-2xl shadow-elevation-2 transition-all duration-200 ease-m3 hover:shadow-elevation-3 active:scale-95 disabled:pointer-events-none disabled:opacity-40 disabled:shadow-none ${
              taskRunning
                ? 'bg-tertiary-container text-on-tertiary-container'
                : 'bg-primary text-on-primary'
            }`}
          >
            {starting ? (
              <Loader2 className="h-5 w-5 animate-spin" aria-hidden="true" />
            ) : taskRunning ? (
              <MonitorPlay className="h-5 w-5 animate-pulse" aria-hidden="true" />
            ) : (
              <Play className="h-5 w-5 fill-current" aria-hidden="true" />
            )}
          </button>
        </div>

        {/* 核心导航目的地（单套 DOM，响应式切换 Rail / Bottom Bar） */}
        <nav
          className="flex w-full flex-row items-center justify-around md:flex-col md:gap-2"
          aria-label="功能视图"
        >
          {NAV_ITEMS.map((item) => {
            const Icon = item.icon;
            const active = activeTab === item.id;
            const showRunningDot = item.id === 'progress' && taskRunning;
            const showSettingsAttentionDot =
              item.id === 'settings' &&
              (!configValidForSave || visibleSaveStatus?.type === 'error');
            return (
              <button
                key={item.id}
                type="button"
                onClick={() => setActiveTab(item.id)}
                aria-current={active ? 'page' : undefined}
                className="group flex flex-1 flex-col items-center gap-1 py-1 focus-visible:outline-none md:w-full md:flex-none"
              >
                <span
                  className={`relative flex h-8 w-14 items-center justify-center rounded-full transition-all duration-200 ease-m3 group-focus-visible:ring-2 group-focus-visible:ring-primary ${
                    active
                      ? 'bg-secondary-container text-on-secondary-container'
                      : 'text-on-surface-variant group-hover:bg-on-surface/[0.07] group-hover:text-on-surface'
                  }`}
                >
                  <Icon className="h-4.5 w-4.5" aria-hidden="true" />
                  {showRunningDot && (
                    <span
                      aria-hidden="true"
                      className="absolute right-3 top-1.5 h-2 w-2 rounded-full bg-primary ring-2 ring-surface"
                    />
                  )}
                  {showSettingsAttentionDot && (
                    <span
                      aria-hidden="true"
                      className="absolute right-3 top-1.5 h-2 w-2 rounded-full bg-warning ring-2 ring-surface"
                    />
                  )}
                </span>
                <span
                  className={`text-[11px] leading-tight transition-colors ${
                    active ? 'font-semibold text-on-surface' : 'font-medium text-on-surface-variant'
                  }`}
                >
                  {item.label}
                </span>
              </button>
            );
          })}
        </nav>

        {/* 底部工具动作：主题切换 / GitHub / 退出登录（桌面端） */}
        <div className="hidden flex-col items-center gap-1.5 md:flex">
          <button
            type="button"
            onClick={toggleLightDark}
            aria-label={isDark ? '切换到浅色模式' : '切换到深色模式'}
            title={isDark ? '切换到浅色模式' : '切换到深色模式'}
            className="flex h-10 w-10 items-center justify-center rounded-full text-on-surface-variant transition-colors hover:bg-on-surface/[0.08] hover:text-on-surface focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
          >
            {isDark ? <Sun className="h-4.5 w-4.5" /> : <Moon className="h-4.5 w-4.5" />}
          </button>

          <button
            type="button"
            onClick={() => tauriApi.openUrl('https://github.com/RRRRUDDDD/Autovisor-GUI')}
            aria-label="项目主页"
            title="打开 Autovisor GUI GitHub 仓库"
            className="flex h-10 w-10 items-center justify-center rounded-full text-on-surface-variant transition-colors hover:bg-on-surface/[0.08] hover:text-on-surface focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
          >
            <Github className="h-4.5 w-4.5" aria-hidden="true" />
          </button>

          <button
            type="button"
            onClick={handleLogout}
            disabled={loggingOut}
            aria-label="退出登录"
            title="退出当前账号并切换登录"
            className="flex h-10 w-10 items-center justify-center rounded-full text-on-surface-variant transition-colors hover:bg-error-container hover:text-on-error-container focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:opacity-45"
          >
            {loggingOut ? (
              <Loader2 className="h-4.5 w-4.5 animate-spin" aria-hidden="true" />
            ) : (
              <LogOut className="h-4.5 w-4.5" aria-hidden="true" />
            )}
          </button>
        </div>
      </aside>

      {/* 右侧主内容区 */}
      <div className="m3-shell-body">
        {/* 轻量单行顶部应用栏（释放桌面纵向空间） */}
        <header className="flex h-14 shrink-0 items-center justify-between gap-3 px-4 md:pr-6 md:pl-2">
          <div className="flex min-w-0 items-center gap-2.5">
            <h1 className="truncate text-lg font-semibold tracking-tight text-on-surface sm:text-xl">
              {currentNav.heading}
            </h1>
          </div>

          <div className="flex shrink-0 items-center gap-2">
            {taskRunning && activeTab !== 'progress' && (
              <Button variant="primary-tonal" size="sm" onClick={() => setActiveTab('progress')}>
                <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
                学习中 · 查看进度
              </Button>
            )}

            {/* 账号状态 Chip */}
            <div
              className="hidden items-center gap-1.5 rounded-full bg-surface-container-high px-3 py-1.5 text-xs text-on-surface sm:flex"
              title={userSub ? `${userLabel} · ${userSub}` : userLabel}
            >
              <UserCheck className="h-3.5 w-3.5 text-primary" aria-hidden="true" />
              <span className="max-w-[9rem] truncate font-medium">{userLabel}</span>
              {userSub && (
                <span className="max-w-[7rem] truncate text-on-surface-variant">· {userSub}</span>
              )}
            </div>

            {/* 窄屏顶部补充主题切换与退出入口 */}
            <button
              type="button"
              onClick={toggleLightDark}
              aria-label="切换主题"
              className="flex h-9 w-9 items-center justify-center rounded-full text-on-surface-variant hover:bg-on-surface/[0.08] md:hidden"
            >
              {isDark ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
            </button>
          </div>
        </header>

        {/* 主视图容器 */}
        <main
          className={`flex min-h-0 flex-1 flex-col px-4 pb-4 md:pr-6 md:pl-2 ${
            activeTab === 'study' ? 'md:overflow-hidden' : 'overflow-y-auto overscroll-contain'
          }`}
        >
          {/* 全局错误或缺失核心提示 */}
          {loadError && (
            <div
              role="alert"
              className="mb-4 flex shrink-0 flex-wrap items-center justify-between gap-3 rounded-2xl bg-error-container px-5 py-3.5 text-sm text-on-error-container"
            >
              <span className="flex items-center gap-2 font-medium">
                <AlertCircle className="h-4 w-4 shrink-0" />
                {loadError}
              </span>
              <Button size="sm" variant="tonal" onClick={loadAll}>
                重新加载
              </Button>
            </div>
          )}

          {runtime?.backendPhase === 'failed' && (
            <div
              role="alert"
              className="mb-4 flex shrink-0 flex-wrap items-center justify-between gap-3 rounded-2xl bg-warning-container px-5 py-3.5 text-sm text-on-warning-container"
            >
              <span className="flex items-center gap-2 font-medium">
                <AlertCircle className="h-4 w-4 shrink-0" />
                {runtime.backendError || '内置学习引擎异常，点击右侧可重新拉起服务。'}
              </span>
              <Button size="sm" onClick={handleRestartBackend} disabled={restartingBackend}>
                {restartingBackend ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <RefreshCw className="h-3.5 w-3.5" />
                )}
                {restartingBackend ? '重启中...' : '重试启动引擎'}
              </Button>
            </div>
          )}

          {shutdownScheduled && activeTab !== 'progress' && (
            <ScheduledShutdownBanner
              shutdownInfo={runState.shutdown}
              cancelling={cancellingShutdown}
              cancelError={cancelShutdownError}
              onCancel={handleCancelShutdown}
            />
          )}

          {updateInfo?.hasUpdate && !updateDismissed && (
            <div
              role="status"
              className="mb-4 flex shrink-0 flex-wrap items-center justify-between gap-3 rounded-2xl bg-primary-container/75 px-5 py-3 text-sm text-on-primary-container animate-fade-in"
            >
              <span className="flex items-center gap-2 font-medium">
                <Download className="h-4 w-4 shrink-0" aria-hidden="true" />
                发现新版本 v{updateInfo.latestVersion}（当前 v{updateInfo.currentVersion}）
              </span>
              <div className="flex items-center gap-2">
                <Button
                  size="sm"
                  onClick={() => handleOpenUrl(updateInfo.downloadUrl || updateInfo.releaseUrl)}
                >
                  前往下载
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => setUpdateDismissed(true)}
                >
                  <X className="h-3.5 w-3.5" aria-hidden="true" />
                  稍后提醒
                </Button>
              </div>
            </div>
          )}

          {activeTab === 'study' && (
            <WorkspaceView
              config={config}
              onChangeConfig={setConfig}
              runtime={runtime}
              dirty={dirty}
              saving={saving}
              saveStatus={visibleSaveStatus}
              refreshingCourses={refreshingCourses}
              courseSyncNotice={courseSyncNotice}
              starting={starting}
              startError={startError}
              runState={runState}
              onRefreshCourses={handleRefreshCourses}
              onRetrySave={handleRetrySave}
              onStartStudy={handleStartStudy}
              onReturnToProgress={() => setActiveTab('progress')}
              onCheckCourse={handleRunCourseCheck}
            />
          )}

          {activeTab === 'progress' && (
            <ProgressView
              runState={runState}
              config={config}
              onBack={() => setActiveTab('study')}
              onStop={handleStop}
              onRestart={handleStartStudy}
              onOpenPath={handleOpenPath}
              onCancelShutdown={handleCancelShutdown}
              onToggleDebugLog={handleToggleDebugLog}
              stopping={stopping}
              cancellingShutdown={cancellingShutdown}
              cancelShutdownError={cancelShutdownError}
            />
          )}

          {activeTab === 'settings' && (
            <SettingsView
              config={config}
              onChangeConfig={setConfig}
              runtime={runtime}
              dirty={dirty}
              saving={saving}
              saveStatus={visibleSaveStatus}
              onRetrySave={handleRetrySave}
              taskRunning={taskRunning}
              onSelectBrowser={handleSelectBrowser}
              onRedetectBrowsers={handleRedetectBrowsers}
              onPickBrowserExe={handlePickBrowserExe}
              onCheckBrowser={handleCheckBrowser}
              onRestartBackend={handleRestartBackend}
              restartingBackend={restartingBackend}
              onTestNotification={handleTestNotification}
              onTestLlmConnection={handleTestLlmConnection}
              onFetchLlmModels={handleFetchLlmModels}
              onTestQuestionBank={handleTestQuestionBank}
              onExportQuestionCache={handleExportQuestionCache}
              onImportQuestionCache={handleImportQuestionCache}
              onImportCookies={handleImportCookies}
              onClearCookies={handleClearCookies}
              onOpenPath={handleOpenPath}
              onOpenUrl={handleOpenUrl}
              onListLogFiles={handleListLogFiles}
              onReadLogFile={handleReadLogFile}
              onChangeCloseBehavior={handleChangeCloseBehavior}
              onToggleDebugLog={handleToggleDebugLog}
              onCheckUpdate={() => handleCheckUpdate({ silent: false })}
              checkingUpdate={checkingUpdate}
              updateInfo={updateInfo}
              updateError={updateError}
            />
          )}
        </main>
      </div>
    </div>
    );
  }

  return (
    <>
      {content}
      <CloseChoiceDialog onRuntimeUpdated={setRuntime} />
    </>
  );
}
