import { useCallback, useEffect, useRef, useState } from 'react';
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
  isValidLlmApiBase,
  isValidProxyUrl,
  normalizeConfig,
} from './lib/configModel';
import { useTheme } from './lib/theme';
import { RunStateContext } from './lib/runStateStore';
import { useAutoSaveConfig } from './hooks/useAutoSaveConfig';
import { isActiveRunStatus, useRunStateStream } from './hooks/useRunStateStream';
import { useUpdateChecker } from './hooks/useUpdateChecker';

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
  const [refreshingCourses, setRefreshingCourses] = useState(false);
  const [courseSyncNotice, setCourseSyncNotice] = useState(null);
  const [starting, setStarting] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [cancellingShutdown, setCancellingShutdown] = useState(false);
  const [cancelShutdownError, setCancelShutdownError] = useState('');
  const [loggingOut, setLoggingOut] = useState(false);
  const [restartingBackend, setRestartingBackend] = useState(false);
  const [startError, setStartError] = useState('');

  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const {
    config,
    setConfig,
    latestConfigRef,
    saving,
    setSaveStatus,
    dirty,
    visibleSaveStatus,
    configValidForSave,
    setSettingsFieldsValid,
    applyBackendConfig,
    persistConfigNow,
    handleRetrySave,
  } = useAutoSaveConfig({ mountedRef });

  const {
    runStateStore,
    taskRunning,
    hasRunHistory,
    shutdownScheduled,
    shutdownInfo,
    prevRunStatusRef,
    cookieImportState,
    setCookieImportState,
    cookieImportAttemptRef,
    finalizeCookieImport,
  } = useRunStateStream({
    setRuntime,
    setAutoRestoreCookie,
    setStopping,
    setCancelShutdownError,
  });

  const {
    updateInfo,
    checkingUpdate,
    updateError,
    updateDismissed,
    setUpdateDismissed,
    handleManualCheckUpdate,
  } = useUpdateChecker({
    loading,
    backendPhase: runtime?.backendPhase,
    latestConfigRef,
    mountedRef,
  });

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
      runStateStore.reset(st);
      prevRunStatusRef.current = st?.status || 'idle';
      if (isActiveRunStatus(st?.status)) {
        setSession(rt?.session || { loggedIn: true, displayName: '智慧树学员' });
        setView('shell');
        setActiveTab('progress');
      }
    } catch (err) {
      setLoadError(err?.message || String(err) || '初始化配置失败');
    } finally {
      setLoading(false);
    }
  }, [applyBackendConfig, runStateStore, prevRunStatusRef]);

  useEffect(() => {
    loadAll();
  }, [loadAll]);

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
    [applyBackendConfig, setSaveStatus]
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
      if (isActiveRunStatus(runStateStore.getSnapshot()?.status)) {
        const stopped = await tauriApi.stopAutovisor();
        runStateStore.applyPayload(stopped);
        prevRunStatusRef.current = stopped?.status || 'idle';
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
  }, [loggingOut, persistConfigNow, runStateStore, prevRunStatusRef, setSaveStatus]);

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
      runStateStore.applyPayload(nextState);
      prevRunStatusRef.current = nextState?.status || 'running';
      setActiveTab('progress');
    } catch (err) {
      setStartError(err?.message || String(err) || '启动 Autovisor GUI 失败');
    } finally {
      setStarting(false);
    }
  }, [persistConfigNow, runStateStore, prevRunStatusRef]);

  const handleStop = useCallback(async () => {
    setStopping(true);
    try {
      const nextState = await tauriApi.stopAutovisor();
      runStateStore.applyPayload(nextState);
      prevRunStatusRef.current = nextState?.status || 'cancelled';
    } catch (err) {
      setStartError(err?.message || String(err) || '停止任务失败');
    } finally {
      setStopping(false);
    }
  }, [runStateStore, prevRunStatusRef]);

  const handleCancelShutdown = useCallback(async () => {
    if (cancellingShutdown) return;
    setCancellingShutdown(true);
    setCancelShutdownError('');
    try {
      const nextState = await tauriApi.cancelShutdown();
      if (nextState) {
        runStateStore.applyPayload(nextState);
        prevRunStatusRef.current = nextState.status || prevRunStatusRef.current;
      }
    } catch (err) {
      setCancelShutdownError(
        err?.message || String(err) || '取消关机失败，可在命令提示符运行 shutdown /a 手动取消'
      );
    } finally {
      setCancellingShutdown(false);
    }
  }, [cancellingShutdown, runStateStore, prevRunStatusRef]);

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
        runStateStore.applyPayload(nextState);
        prevRunStatusRef.current = nextState?.status || 'running';
        setActiveTab('progress');
      } catch (err) {
        setStartError(err?.message || String(err) || '启动课程检查失败');
      }
    },
    [persistConfigNow, runStateStore, prevRunStatusRef]
  );

  const handleCheckBrowser = useCallback(async () => {
    setStartError('');
    try {
      await persistConfigNow();
      const nextState = await tauriApi.runDiagnostic('check_browser', '');
      runStateStore.applyPayload(nextState);
      prevRunStatusRef.current = nextState?.status || 'running';
      setActiveTab('progress');
    } catch (err) {
      setStartError(err?.message || String(err) || '启动浏览器检测失败');
    }
  }, [persistConfigNow, runStateStore, prevRunStatusRef]);

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

  const handleSelectBrowser = useCallback(
    (browser) => {
      if (!browser) return;
      setConfig((prev) => ({
        ...prev,
        driver: browser.driver || prev.driver,
        exePath: browser.exePath || '',
      }));
    },
    [setConfig]
  );

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
  }, [setConfig]);

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
  }, [setConfig]);

  const handleSaveProxy = useCallback(
    async (nextProxy) => {
      const normalized = normalizeConfig({ ...latestConfigRef.current, proxy: nextProxy });
      latestConfigRef.current = normalized;
      setConfig(normalized);
      await persistConfigNow();
    },
    [latestConfigRef, setConfig, persistConfigNow]
  );

  const handleImportCookies = useCallback(async () => {
    try {
      const picked = await tauriApi.pickCookieFile();
      if (!picked) return;
      cookieImportAttemptRef.current += 1;
      if (view === 'login') {
        setAutoRestoreCookie(true);
        setCookieImportState((prev) => ({ ...prev, importing: true, error: '' }));
      }
      const nextState = await tauriApi.runDiagnostic('import_cookies', picked);
      runStateStore.applyPayload(nextState);
      prevRunStatusRef.current = nextState?.status || 'idle';
      if (view !== 'login') {
        setActiveTab('progress');
      } else if (nextState?.status && !isActiveRunStatus(nextState.status)) {
        const rt = await tauriApi.getRuntimeInfo().catch(() => null);
        if (rt) setRuntime(rt);
        finalizeCookieImport(nextState);
      }
    } catch (err) {
      const msg = err?.message || String(err) || '导入 Cookie 失败';
      if (view === 'login') {
        setCookieImportState((prev) => ({ ...prev, importing: false, error: msg }));
      } else {
        setStartError(msg);
      }
    }
  }, [
    view,
    runStateStore,
    prevRunStatusRef,
    cookieImportAttemptRef,
    setCookieImportState,
    finalizeCookieImport,
  ]);

  const handleClearSavedLogin = useCallback(async (mode = '') => {
    const rt = await tauriApi.clearSavedLogin(mode);
    if (rt) setRuntime(rt);
    return rt;
  }, []);

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

  const handleTestNotification = useCallback((payload) => tauriApi.testNotification(payload), []);
  const handleTestLlmConnection = useCallback((payload) => tauriApi.testLlmConnection(payload), []);
  const handleFetchLlmModels = useCallback((payload) => tauriApi.fetchLlmModels(payload), []);
  const handleTestQuestionBank = useCallback((payload) => tauriApi.testQuestionBank(payload), []);
  const handleExportQuestionCache = useCallback(() => tauriApi.exportQuestionCache(), []);
  const handleImportQuestionCache = useCallback(() => tauriApi.importQuestionCache(), []);
  const handleClearQuestionCache = useCallback(() => tauriApi.clearQuestionCache(), []);
  const handleOpenUrl = useCallback((url) => tauriApi.openUrl(url), []);

  const handleToggleDebugLog = useCallback(
    (enabled) => {
      const next = Boolean(enabled);
      setConfig((prev) =>
        prev.enableDebugLog === next ? prev : { ...prev, enableDebugLog: next }
      );
      void tauriApi.setDebugLog(next).catch(() => {});
    },
    [setConfig]
  );

  const handleReturnToProgress = useCallback(() => setActiveTab('progress'), []);
  const handleBackToStudy = useCallback(() => setActiveTab('study'), []);
  const handleOpenSettings = useCallback(() => setActiveTab('settings'), []);

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
                shutdownInfo={shutdownInfo}
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
          onClearSavedLogin={handleClearSavedLogin}
        />
      </>
    );
  } else {
    content = (
      <div className="m3-app-shell min-h-screen bg-surface text-on-surface pb-16 md:pb-0">
        <aside
          aria-label="主导航"
          className="fixed inset-x-0 bottom-0 z-30 flex h-16 shrink-0 flex-row items-center justify-around bg-surface-container px-2 shadow-elevation-2 md:static md:h-auto md:w-[88px] md:flex-col md:justify-between md:bg-surface md:px-0 md:py-4 md:shadow-none"
        >
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

        <div className="m3-shell-body">
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

          <main
            className={`flex min-h-0 flex-1 flex-col px-4 pb-4 md:pr-6 md:pl-2 ${
              activeTab === 'study' ? 'md:overflow-hidden' : 'overflow-y-auto overscroll-contain'
            }`}
          >
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
                shutdownInfo={shutdownInfo}
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
                taskRunning={taskRunning}
                hasRunHistory={hasRunHistory}
                onRefreshCourses={handleRefreshCourses}
                onRetrySave={handleRetrySave}
                onStartStudy={handleStartStudy}
                onReturnToProgress={handleReturnToProgress}
                onCheckCourse={handleRunCourseCheck}
              />
            )}

            {activeTab === 'progress' && (
              <ProgressView
                config={config}
                onBack={handleBackToStudy}
                onStop={handleStop}
                onRestart={handleStartStudy}
                onOpenPath={handleOpenPath}
                onOpenSettings={handleOpenSettings}
                onRelogin={handleLogout}
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
                onValidityChange={setSettingsFieldsValid}
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
                onClearQuestionCache={handleClearQuestionCache}
                onImportCookies={handleImportCookies}
                onClearCookies={handleClearCookies}
                onOpenPath={handleOpenPath}
                onOpenUrl={handleOpenUrl}
                onListLogFiles={handleListLogFiles}
                onReadLogFile={handleReadLogFile}
                onChangeCloseBehavior={handleChangeCloseBehavior}
                onToggleDebugLog={handleToggleDebugLog}
                onCheckUpdate={handleManualCheckUpdate}
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
    <RunStateContext.Provider value={runStateStore}>
      {content}
      <CloseChoiceDialog onRuntimeUpdated={setRuntime} />
    </RunStateContext.Provider>
  );
}
