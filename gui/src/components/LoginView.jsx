import { useState, useEffect, useRef, useCallback } from 'react';
import {
  GraduationCap,
  LogIn,
  Loader2,
  KeyRound,
  AlertCircle,
  Eye,
  EyeOff,
  Smartphone,
  School,
  QrCode,
  Globe,
  CheckCircle2,
  Search,
  X,
  RefreshCw,
  Upload,
  Sun,
  Moon,
  Check,
  Network,
  Download,
} from 'lucide-react';
import Button from './ui/Button';
import Input from './ui/Input';
import Label from './ui/Label';
import { tauriApi } from '../lib/tauriApi';
import { isValidProxyUrl } from '../lib/configModel';

const LOGIN_TABS = [
  { id: 'phone', label: '手机号登录', icon: Smartphone },
  { id: 'student', label: '学号登录', icon: School },
  { id: 'qrcode', label: '扫码登录', icon: QrCode },
];

const LoginView = ({
  runtime,
  config,
  autoRestoreCookie = true,
  cookieImportState = null,
  isDark = false,
  onToggleTheme,
  onLoginSuccess,
  onSelectBrowser,
  onRedetectBrowsers,
  onPickBrowserExe,
  onImportCookies,
  onSaveProxy,
}) => {
  const initialMode =
    runtime?.session?.loginMode === 'student'
      ? 'student'
      : runtime?.session?.loginMode === 'qrcode'
        ? 'qrcode'
        : 'phone';

  const [mode, setMode] = useState(initialMode);
  const [username, setUsername] = useState(() => config?.username || '');
  const [password, setPassword] = useState('');
  const [schoolQuery, setSchoolQuery] = useState(() => runtime?.session?.schoolName || '');
  const [selectedSchool, setSelectedSchool] = useState(() =>
    runtime?.session?.schoolName
      ? { schoolId: runtime.session.schoolId || '', name: runtime.session.schoolName }
      : null
  );
  const [schoolOptions, setSchoolOptions] = useState([]);
  const [schoolLoading, setSchoolLoading] = useState(false);
  const [schoolDropdownOpen, setSchoolDropdownOpen] = useState(false);
  const [studentId, setStudentId] = useState(() =>
    runtime?.session?.loginMode === 'student' ? runtime?.session?.account || '' : ''
  );
  const [showPassword, setShowPassword] = useState(false);
  const [rememberCredentials, setRememberCredentials] = useState(false);
  const [proxyInput, setProxyInput] = useState(() => config?.proxy || '');
  const [showProxyInput, setShowProxyInput] = useState(() => Boolean(config?.proxy));
  const [loading, setLoading] = useState(false);
  const [restoringCookie, setRestoringCookie] = useState(false);
  const [statusInfo, setStatusInfo] = useState(null);
  const [error, setError] = useState('');
  const [qrState, setQrState] = useState({
    loading: false,
    sessionId: '',
    imageBase64: '',
    status: 'idle', // idle | waiting | scanned | confirmed | expired | cancelled | error
    error: '',
    savedPath: '',
    saving: false,
  });

  const schoolBoxRef = useRef(null);
  const autoAttemptedRef = useRef(false);
  const lastImportNonceRef = useRef(cookieImportState?.nonce || 0);
  const mountedRef = useRef(true);
  const qrSessionIdRef = useRef('');
  const qrPollTimerRef = useRef(null);

  const proxyValid = isValidProxyUrl(proxyInput);
  const effectiveProxy = proxyValid ? proxyInput.trim() : '';
  const importingCookies = Boolean(cookieImportState?.importing);

  useEffect(() => {
    const cfgProxy = config?.proxy || '';
    setProxyInput(cfgProxy);
    if (cfgProxy) setShowProxyInput(true);
  }, [config?.proxy]);

  useEffect(() => {
    if (cookieImportState?.error) {
      setError(cookieImportState.error);
    }
  }, [cookieImportState?.error]);

  const stopQrPolling = useCallback(() => {
    if (qrPollTimerRef.current) {
      clearTimeout(qrPollTimerRef.current);
      qrPollTimerRef.current = null;
    }
  }, []);

  const cancelActiveQrSession = useCallback(() => {
    stopQrPolling();
    const sid = qrSessionIdRef.current;
    qrSessionIdRef.current = '';
    if (sid) {
      void tauriApi.cancelQrLogin(sid).catch(() => {});
    }
  }, [stopQrPolling]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      cancelActiveQrSession();
    };
  }, [cancelActiveQrSession]);

  useEffect(() => {
    let unlisten = () => {};
    let active = true;
    tauriApi
      .listenLoginStatus((payload) => {
        if (active && payload) {
          setStatusInfo(payload);
        }
      })
      .then((fn) => {
        if (active) unlisten = fn;
        else fn();
      });
    return () => {
      active = false;
      unlisten();
    };
  }, []);

  useEffect(() => {
    const onPointerDown = (e) => {
      if (schoolBoxRef.current && !schoolBoxRef.current.contains(e.target)) {
        setSchoolDropdownOpen(false);
      }
    };
    document.addEventListener('mousedown', onPointerDown);
    return () => document.removeEventListener('mousedown', onPointerDown);
  }, []);

  useEffect(() => {
    if (mode !== 'student' || !schoolDropdownOpen) return undefined;
    let cancelled = false;
    setSchoolLoading(true);
    const timer = setTimeout(async () => {
      try {
        const list = await tauriApi.searchSchools(schoolQuery.trim(), effectiveProxy);
        if (!cancelled && mountedRef.current) {
          setSchoolOptions(Array.isArray(list) ? list : []);
        }
      } catch {
        if (!cancelled && mountedRef.current) {
          setSchoolOptions([]);
        }
      } finally {
        if (!cancelled && mountedRef.current) {
          setSchoolLoading(false);
        }
      }
    }, 180);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [mode, schoolQuery, schoolDropdownOpen, effectiveProxy]);

  const detectedBrowsers = runtime?.detectedBrowsers || [];
  const activeBrowser =
    detectedBrowsers.find(
      (b) => config?.exePath && b.exePath.toLowerCase() === config.exePath.toLowerCase()
    ) ||
    detectedBrowsers[0] ||
    null;

  const runLogin = useCallback(
    async (request, { isAutoRestore = false } = {}) => {
      setError('');
      setStatusInfo(null);
      setLoading(true);
      if (isAutoRestore) setRestoringCookie(true);
      try {
        const res = await tauriApi.zhsLogin(request);
        if (!mountedRef.current) return;
        setPassword('');
        await onLoginSuccess(res);
      } catch (err) {
        if (!mountedRef.current) return;
        const msg = err?.message || String(err) || '登录失败，请重试';
        if (msg.includes('已取消')) {
          return;
        }
        if (isAutoRestore) {
          if (msg.includes('失效') || msg.includes('过期') || msg.includes('Cookie')) {
            setError('已保存的登录凭证已过期或失效，请重新登录');
          } else {
            setError(`自动恢复登录失败：${msg}`);
          }
        } else {
          setError(msg);
        }
      } finally {
        if (mountedRef.current) {
          setLoading(false);
          setRestoringCookie(false);
          setStatusInfo(null);
        }
      }
    },
    [onLoginSuccess]
  );

  useEffect(() => {
    const nextNonce = cookieImportState?.nonce || 0;
    if (nextNonce > lastImportNonceRef.current) {
      lastImportNonceRef.current = nextNonce;
      autoAttemptedRef.current = false;
    }
    if (
      autoAttemptedRef.current ||
      importingCookies ||
      !autoRestoreCookie ||
      !runtime?.cookieExists
    ) {
      return;
    }
    autoAttemptedRef.current = true;
    void runLogin(
      {
        mode: 'cookie',
        browserExe: activeBrowser?.exePath || config?.exePath || '',
        proxy: effectiveProxy,
      },
      { isAutoRestore: true }
    );
  }, [
    cookieImportState?.nonce,
    importingCookies,
    autoRestoreCookie,
    runtime?.cookieExists,
    activeBrowser?.exePath,
    config?.exePath,
    effectiveProxy,
    runLogin,
  ]);

  const startInAppQrLogin = useCallback(async () => {
    cancelActiveQrSession();
    setError('');
    setQrState({
      loading: true,
      sessionId: '',
      imageBase64: '',
      status: 'idle',
      error: '',
      savedPath: '',
      saving: false,
    });
    try {
      const res = await tauriApi.startQrLogin(effectiveProxy, '');
      if (!mountedRef.current) {
        if (res?.sessionId) void tauriApi.cancelQrLogin(res.sessionId).catch(() => {});
        return;
      }
      const sid = res?.sessionId || '';
      const img = res?.imageBase64 || '';
      if (!sid || !img) {
        throw new Error('未获取到有效二维码图片');
      }
      qrSessionIdRef.current = sid;
      setQrState({
        loading: false,
        sessionId: sid,
        imageBase64: img,
        status: 'waiting',
        error: '',
        savedPath: res?.savedPath || '',
        saving: false,
      });

      const pollOnce = async () => {
        if (!mountedRef.current || qrSessionIdRef.current !== sid) return;
        try {
          const pollRes = await tauriApi.pollQrLogin(sid);
          if (!mountedRef.current || qrSessionIdRef.current !== sid) return;
          const st = pollRes?.status || 'waiting';
          if (st === 'confirmed') {
            qrSessionIdRef.current = '';
            stopQrPolling();
            setQrState((prev) => ({ ...prev, status: 'confirmed', error: '' }));
            await runLogin({
              mode: 'cookie',
              browserExe: activeBrowser?.exePath || config?.exePath || '',
              proxy: effectiveProxy,
            });
            return;
          }
          if (st === 'expired' || st === 'cancelled') {
            qrSessionIdRef.current = '';
            stopQrPolling();
            setQrState((prev) => ({ ...prev, status: st }));
            return;
          }
          setQrState((prev) => ({ ...prev, status: st }));
          qrPollTimerRef.current = setTimeout(pollOnce, 1500);
        } catch (err) {
          if (!mountedRef.current || qrSessionIdRef.current !== sid) return;
          qrSessionIdRef.current = '';
          stopQrPolling();
          setQrState((prev) => ({
            ...prev,
            status: 'error',
            error: err?.message || String(err) || '轮询二维码状态失败',
          }));
        }
      };

      qrPollTimerRef.current = setTimeout(pollOnce, 1500);
    } catch (err) {
      if (!mountedRef.current) return;
      setQrState({
        loading: false,
        sessionId: '',
        imageBase64: '',
        status: 'error',
        error: err?.message || String(err) || '获取二维码失败，请检查网络或改用浏览器扫码',
        savedPath: '',
        saving: false,
      });
    }
  }, [
    cancelActiveQrSession,
    effectiveProxy,
    stopQrPolling,
    runLogin,
    activeBrowser?.exePath,
    config?.exePath,
  ]);

  useEffect(() => {
    if (mode !== 'qrcode' || restoringCookie || loading) {
      if (mode !== 'qrcode') {
        cancelActiveQrSession();
      }
      return;
    }
    if (!qrSessionIdRef.current && !qrState.loading && qrState.status === 'idle') {
      void startInAppQrLogin();
    }
  }, [
    mode,
    restoringCookie,
    loading,
    qrState.loading,
    qrState.status,
    cancelActiveQrSession,
    startInAppQrLogin,
  ]);

  const handleSaveQrPng = async () => {
    if (!qrState.sessionId || qrState.saving) return;
    setQrState((prev) => ({ ...prev, saving: true, error: '' }));
    try {
      const dir = await tauriApi.pickDirectory();
      if (!dir) {
        if (mountedRef.current) setQrState((prev) => ({ ...prev, saving: false }));
        return;
      }
      const res = await tauriApi.saveQrImage(qrState.sessionId, dir);
      if (mountedRef.current) {
        setQrState((prev) => ({
          ...prev,
          saving: false,
          savedPath: res?.savedPath || dir,
        }));
      }
    } catch (err) {
      if (mountedRef.current) {
        setQrState((prev) => ({
          ...prev,
          saving: false,
          error: err?.message || String(err) || '保存二维码图片失败',
        }));
      }
    }
  };

  const handleBrowserQrFallback = async () => {
    cancelActiveQrSession();
    setQrState((prev) => ({ ...prev, status: 'idle', sessionId: '', imageBase64: '' }));
    const trimmedProxy = proxyInput.trim();
    if (trimmedProxy !== (config?.proxy || '').trim()) {
      await onSaveProxy?.(trimmedProxy);
    }
    await runLogin({
      mode: 'qrcode',
      username: '',
      schoolId: '',
      schoolName: '',
      studentId: '',
      password: '',
      browserExe: activeBrowser?.exePath || config?.exePath || '',
      proxy: trimmedProxy,
      rememberCredentials: false,
    });
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (loading || importingCookies) return;

    if (!proxyValid) {
      setError('代理格式无效，请以 http://、https:// 或 socks5:// 开头');
      return;
    }

    if (mode === 'phone') {
      if (!username.trim()) {
        setError('请输入手机号或账号');
        return;
      }
      if (!password) {
        setError('请输入登录密码');
        return;
      }
    } else if (mode === 'student') {
      const schName = (selectedSchool?.name || schoolQuery).trim();
      if (!schName) {
        setError('请输入并选择你的学校');
        return;
      }
      if (!studentId.trim()) {
        setError('请输入大学学号');
        return;
      }
      if (!password) {
        setError('请输入学号登录密码');
        return;
      }
    }

    const trimmedProxy = proxyInput.trim();
    if (trimmedProxy !== (config?.proxy || '').trim()) {
      await onSaveProxy?.(trimmedProxy);
    }

    await runLogin({
      mode,
      username: mode === 'phone' ? username.trim() : '',
      schoolId: mode === 'student' ? selectedSchool?.schoolId || '' : '',
      schoolName: mode === 'student' ? (selectedSchool?.name || schoolQuery).trim() : '',
      studentId: mode === 'student' ? studentId.trim() : '',
      password: mode === 'qrcode' ? '' : password,
      browserExe: activeBrowser?.exePath || config?.exePath || '',
      proxy: trimmedProxy,
      rememberCredentials: mode === 'phone' && rememberCredentials,
    });
  };

  const handleCancelLogin = async () => {
    try {
      await tauriApi.cancelZhsLogin();
    } catch {
      // ignore
    }
  };

  const errId = 'login-error';

  return (
    <main className="relative flex min-h-screen items-center justify-center bg-surface px-[clamp(1rem,4vw,4rem)] py-[clamp(2rem,6vh,4rem)]">
      {/* 右上角主题切换按钮 */}
      {onToggleTheme && (
        <div className="fixed right-5 top-5 z-20">
          <Button
            type="button"
            variant="tonal"
            size="icon"
            onClick={onToggleTheme}
            aria-label={isDark ? '切换到浅色模式' : '切换到深色模式'}
            title={isDark ? '切换到浅色模式' : '切换到深色模式'}
          >
            {isDark ? <Sun className="h-4.5 w-4.5" /> : <Moon className="h-4.5 w-4.5" />}
          </Button>
        </div>
      )}

      <div className="w-full max-w-[clamp(430px,36vw,520px)] animate-pop-in">
        {/* 品牌区 */}
        <div className="mb-7 flex flex-col items-center text-center">
          <div className="mb-4 flex h-16 w-16 items-center justify-center rounded-[22px] bg-primary-container text-on-primary-container shadow-elevation-1">
            <GraduationCap className="h-9 w-9" aria-hidden="true" />
          </div>
          <div className="flex items-center gap-2.5">
            <h1 className="text-[22px] font-semibold tracking-tight text-on-surface">
              知到 · 智慧树自动化学习助手
            </h1>
          </div>
          <p className="mt-1.5 text-sm text-on-surface-variant">
            先登录智慧树账号，进入后将自动识别你的全部课程队列
          </p>
        </div>

        {/* M3 吨级表面登录卡片 */}
        <div className="rounded-[28px] bg-surface-container-low p-[clamp(1.5rem,2.4vw,2rem)] shadow-elevation-1">
          {restoringCookie ? (
            <div className="flex flex-col items-center py-9 text-center">
              <div className="flex h-14 w-14 items-center justify-center rounded-full bg-primary-container text-on-primary-container">
                <Loader2 className="h-7 w-7 animate-spin" aria-hidden="true" />
              </div>
              <p className="mt-4 text-sm font-medium text-on-surface">
                {statusInfo?.message || '正在校验已保存的登录凭证并识别课程...'}
              </p>
              <p className="mt-1.5 text-xs text-on-surface-variant">
                自动使用本机 {activeBrowser?.name || '浏览器'} 后台同步课程队列
              </p>
              <Button
                variant="tonal"
                size="sm"
                className="mt-6"
                onClick={handleCancelLogin}
              >
                改用其他账号登录
              </Button>
            </div>
          ) : (
            <>
              {/* M3 Segmented Button（手机号 / 学号 / 扫码） */}
              <div
                role="tablist"
                aria-label="登录方式"
                className="mb-6 grid grid-cols-3 overflow-hidden rounded-full border border-outline/70"
              >
                {LOGIN_TABS.map((tab, idx) => {
                  const Icon = tab.icon;
                  const active = mode === tab.id;
                  return (
                    <button
                      key={tab.id}
                      type="button"
                      role="tab"
                      aria-selected={active}
                      disabled={loading}
                      onClick={() => {
                        if (mode === 'qrcode' && tab.id !== 'qrcode') {
                          cancelActiveQrSession();
                          setQrState((prev) => ({
                            ...prev,
                            status: 'idle',
                            sessionId: '',
                            imageBase64: '',
                            error: '',
                          }));
                        }
                        setMode(tab.id);
                        setError('');
                      }}
                      className={`flex h-10 items-center justify-center gap-1.5 text-[13px] font-medium transition-colors duration-150 ease-m3 ${
                        idx > 0 ? 'border-l border-outline/70' : ''
                      } ${
                        active
                          ? 'bg-secondary-container text-on-secondary-container'
                          : 'bg-transparent text-on-surface-variant hover:bg-on-surface/[0.05] hover:text-on-surface'
                      } disabled:opacity-50`}
                    >
                      {active ? (
                        <Check className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                      ) : (
                        <Icon className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                      )}
                      <span>{tab.label}</span>
                    </button>
                  );
                })}
              </div>

              <form
                onSubmit={handleSubmit}
                aria-describedby={error ? errId : undefined}
                className="space-y-4"
              >
                {mode === 'phone' && (
                  <>
                    <div className="space-y-1.5">
                      <Label htmlFor="login-phone">手机号</Label>
                      <Input
                        id="login-phone"
                        type="text"
                        inputMode="tel"
                        autoComplete="username"
                        placeholder="请输入智慧树绑定手机号"
                        value={username}
                        disabled={loading}
                        onChange={(e) => setUsername(e.target.value)}
                      />
                    </div>
                    <div className="space-y-1.5">
                      <Label htmlFor="login-password">密码</Label>
                      <div className="relative">
                        <Input
                          id="login-password"
                          type={showPassword ? 'text' : 'password'}
                          autoComplete="current-password"
                          placeholder="请输入密码"
                          value={password}
                          disabled={loading}
                          onChange={(e) => setPassword(e.target.value)}
                          className="pr-11"
                        />
                        <button
                          type="button"
                          onClick={() => setShowPassword((s) => !s)}
                          disabled={loading}
                          aria-label={showPassword ? '隐藏密码' : '显示密码'}
                          className="absolute inset-y-0 right-0 flex w-11 items-center justify-center rounded-r-xl text-on-surface-variant hover:text-on-surface"
                        >
                          {showPassword ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                        </button>
                      </div>
                    </div>
                    <label className="flex cursor-pointer items-center gap-2.5 pt-0.5 text-xs text-on-surface-variant">
                      <input
                        id="login-remember-credentials"
                        type="checkbox"
                        checked={rememberCredentials}
                        disabled={loading}
                        onChange={(e) => setRememberCredentials(e.target.checked)}
                        className="h-4 w-4 rounded accent-primary"
                      />
                      <span>记住账号密码（挂机中途掉线时自动重登）</span>
                    </label>
                  </>
                )}

                {mode === 'student' && (
                  <>
                    <div className="space-y-1.5" ref={schoolBoxRef}>
                      <Label htmlFor="login-school">学校</Label>
                      <div className="relative">
                        <Search
                          className="pointer-events-none absolute left-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-on-surface-variant"
                          aria-hidden="true"
                        />
                        <Input
                          id="login-school"
                          type="text"
                          autoComplete="off"
                          placeholder="输入学校名称关键字搜索（如：清华大学）"
                          value={schoolQuery}
                          disabled={loading}
                          onFocus={() => setSchoolDropdownOpen(true)}
                          onChange={(e) => {
                            setSchoolQuery(e.target.value);
                            setSelectedSchool(null);
                            setSchoolDropdownOpen(true);
                          }}
                          className="pl-10 pr-9"
                        />
                        {schoolQuery && !loading && (
                          <button
                            type="button"
                            onClick={() => {
                              setSchoolQuery('');
                              setSelectedSchool(null);
                              setSchoolDropdownOpen(true);
                            }}
                            aria-label="清空学校"
                            className="absolute inset-y-0 right-0 flex w-9 items-center justify-center text-on-surface-variant hover:text-on-surface"
                          >
                            <X className="h-3.5 w-3.5" />
                          </button>
                        )}

                        {schoolDropdownOpen && (
                          <div className="absolute left-0 right-0 top-[calc(100%+6px)] z-30 max-h-52 overflow-y-auto rounded-2xl bg-surface-container-high p-1.5 shadow-elevation-3">
                            {schoolLoading ? (
                              <div className="flex items-center justify-center gap-2 py-4 text-xs text-on-surface-variant">
                                <Loader2 className="h-3.5 w-3.5 animate-spin text-primary" />
                                正在检索智慧树高校库...
                              </div>
                            ) : schoolOptions.length === 0 ? (
                              <p className="px-3 py-3 text-center text-xs text-on-surface-variant">
                                未找到匹配学校，可直接输入完整校名继续登录
                              </p>
                            ) : (
                              schoolOptions.map((item) => {
                                const picked = selectedSchool?.schoolId === item.schoolId;
                                return (
                                  <button
                                    key={item.schoolId}
                                    type="button"
                                    onClick={() => {
                                      setSelectedSchool(item);
                                      setSchoolQuery(item.name);
                                      setSchoolDropdownOpen(false);
                                    }}
                                    className={`flex w-full items-center justify-between rounded-xl px-3.5 py-2.5 text-left text-[13px] transition-colors ${
                                      picked
                                        ? 'bg-secondary-container font-medium text-on-secondary-container'
                                        : 'text-on-surface hover:bg-on-surface/[0.06]'
                                    }`}
                                  >
                                    <span className="truncate">{item.name}</span>
                                    {picked && (
                                      <CheckCircle2 className="h-4 w-4 shrink-0 text-primary" />
                                    )}
                                  </button>
                                );
                              })
                            )}
                          </div>
                        )}
                      </div>
                      {selectedSchool?.schoolId && (
                        <p className="flex items-center gap-1.5 text-[11px] font-medium text-success">
                          <CheckCircle2 className="h-3.5 w-3.5" />
                          已绑定学校：{selectedSchool.name}（ID: {selectedSchool.schoolId}）
                        </p>
                      )}
                    </div>

                    <div className="space-y-1.5">
                      <Label htmlFor="login-student-id">大学学号</Label>
                      <Input
                        id="login-student-id"
                        type="text"
                        autoComplete="username"
                        placeholder="请输入学号"
                        value={studentId}
                        disabled={loading}
                        onChange={(e) => setStudentId(e.target.value)}
                      />
                    </div>

                    <div className="space-y-1.5">
                      <Label htmlFor="login-student-password">密码</Label>
                      <div className="relative">
                        <Input
                          id="login-student-password"
                          type={showPassword ? 'text' : 'password'}
                          autoComplete="current-password"
                          placeholder="请输入密码"
                          value={password}
                          disabled={loading}
                          onChange={(e) => setPassword(e.target.value)}
                          className="pr-11"
                        />
                        <button
                          type="button"
                          onClick={() => setShowPassword((s) => !s)}
                          disabled={loading}
                          aria-label={showPassword ? '隐藏密码' : '显示密码'}
                          className="absolute inset-y-0 right-0 flex w-11 items-center justify-center rounded-r-xl text-on-surface-variant hover:text-on-surface"
                        >
                          {showPassword ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                        </button>
                      </div>
                    </div>
                  </>
                )}

                {mode === 'qrcode' && (
                  <div className="rounded-2xl bg-surface-container px-5 py-5 text-center">
                    <div className="flex flex-col items-center">
                      {qrState.loading ? (
                        <div className="flex h-44 w-44 flex-col items-center justify-center rounded-2xl bg-surface-container-highest text-on-surface-variant">
                          <Loader2 className="h-7 w-7 animate-spin text-primary" aria-hidden="true" />
                          <span className="mt-2 text-xs">正在获取二维码...</span>
                        </div>
                      ) : qrState.imageBase64 ? (
                        <div className="relative">
                          <img
                            src={`data:image/png;base64,${qrState.imageBase64}`}
                            alt="智慧树登录二维码"
                            className={`h-44 w-44 rounded-2xl bg-white p-2 shadow-elevation-1 transition-opacity ${
                              qrState.status === 'expired' || qrState.status === 'cancelled'
                                ? 'opacity-35'
                                : 'opacity-100'
                            }`}
                          />
                          {(qrState.status === 'expired' || qrState.status === 'cancelled') && (
                            <div className="absolute inset-0 flex items-center justify-center">
                              <Button
                                type="button"
                                size="sm"
                                onClick={() => void startInAppQrLogin()}
                              >
                                <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
                                刷新二维码
                              </Button>
                            </div>
                          )}
                        </div>
                      ) : (
                        <div className="flex h-44 w-44 flex-col items-center justify-center rounded-2xl bg-surface-container-highest px-3 text-on-surface-variant">
                          <QrCode className="h-8 w-8 opacity-60" aria-hidden="true" />
                          <Button
                            type="button"
                            size="sm"
                            variant="tonal"
                            className="mt-3"
                            onClick={() => void startInAppQrLogin()}
                            disabled={loading || !proxyValid}
                          >
                            获取登录二维码
                          </Button>
                        </div>
                      )}

                      <div className="mt-3 flex flex-wrap items-center justify-center gap-2">
                        <span
                          className={`rounded-full px-3 py-1 text-xs font-medium ${
                            qrState.status === 'scanned'
                              ? 'bg-primary-container text-on-primary-container'
                              : qrState.status === 'confirmed'
                                ? 'bg-success-container text-on-success-container'
                                : qrState.status === 'expired' ||
                                    qrState.status === 'cancelled' ||
                                    qrState.status === 'error'
                                  ? 'bg-warning-container text-on-warning-container'
                                  : 'bg-surface-container-highest text-on-surface-variant'
                          }`}
                        >
                          {qrState.loading
                            ? '正在加载二维码'
                            : qrState.status === 'waiting'
                              ? '请使用智慧树 / 知到 App 扫码'
                              : qrState.status === 'scanned'
                                ? '已扫码，请在手机端确认登录'
                                : qrState.status === 'confirmed'
                                  ? '扫码成功，正在同步课程...'
                                  : qrState.status === 'expired'
                                    ? '二维码已过期，请点击刷新'
                                    : qrState.status === 'cancelled'
                                      ? '已在手机端取消登录'
                                      : qrState.status === 'error'
                                        ? '获取或轮询失败'
                                        : '应用内免开浏览器扫码'}
                        </span>
                      </div>

                      {qrState.error && (
                        <p role="alert" className="mt-2 text-xs text-error">
                          {qrState.error}
                        </p>
                      )}
                      {qrState.savedPath && (
                        <p role="status" className="mt-2 break-all text-[11px] text-success">
                          二维码已保存至：{qrState.savedPath}
                        </p>
                      )}

                      <div className="mt-3.5 flex flex-wrap items-center justify-center gap-2">
                        <Button
                          type="button"
                          variant="tonal"
                          size="sm"
                          onClick={() => void startInAppQrLogin()}
                          disabled={qrState.loading || loading || !proxyValid}
                        >
                          <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
                          刷新二维码
                        </Button>
                        <Button
                          type="button"
                          variant="tonal"
                          size="sm"
                          onClick={() => void handleSaveQrPng()}
                          disabled={!qrState.sessionId || qrState.saving || loading}
                        >
                          <Download className="h-3.5 w-3.5" aria-hidden="true" />
                          {qrState.saving ? '保存中...' : '保存二维码 PNG'}
                        </Button>
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          onClick={() => void handleBrowserQrFallback()}
                          disabled={loading || importingCookies || !activeBrowser || !proxyValid}
                        >
                          改用浏览器扫码（备用）
                        </Button>
                      </div>
                    </div>
                  </div>
                )}

                {/* 自动识别的本机浏览器信息 */}
                <div className="flex items-center justify-between gap-2 rounded-2xl bg-surface-container px-4 py-2.5 text-xs">
                  <div className="flex min-w-0 items-center gap-2">
                    <Globe
                      className={`h-4 w-4 shrink-0 ${
                        activeBrowser ? 'text-primary' : 'text-warning'
                      }`}
                      aria-hidden="true"
                    />
                    {activeBrowser ? (
                      <span className="truncate text-on-surface-variant" title={activeBrowser.exePath}>
                        自动识别浏览器：
                        <strong className="font-semibold text-on-surface">{activeBrowser.name}</strong>
                      </span>
                    ) : (
                      <span className="truncate font-medium text-warning">
                        未检测到 Edge / Chrome 浏览器
                      </span>
                    )}
                  </div>
                  <div className="flex shrink-0 items-center gap-2">
                    {detectedBrowsers.length > 1 && (
                      <select
                        aria-label="切换已识别浏览器"
                        value={activeBrowser?.exePath || ''}
                        disabled={loading}
                        onChange={(e) => {
                          const picked = detectedBrowsers.find((b) => b.exePath === e.target.value);
                          if (picked) onSelectBrowser?.(picked);
                        }}
                        className="h-7 rounded-full border border-outline/60 bg-surface-container-lowest px-2.5 text-[11px] text-on-surface focus:border-primary focus:outline-none"
                      >
                        {detectedBrowsers.map((b) => (
                          <option key={b.exePath} value={b.exePath}>
                            {b.name}
                          </option>
                        ))}
                      </select>
                    )}
                    <button
                      type="button"
                      onClick={activeBrowser ? onRedetectBrowsers : onPickBrowserExe}
                      disabled={loading}
                      className="inline-flex items-center gap-1 rounded-full px-2 py-1 text-[11px] font-medium text-primary hover:bg-primary/10 disabled:opacity-50"
                    >
                      <RefreshCw className="h-3 w-3" />
                      {activeBrowser ? '重扫' : '手动选择'}
                    </button>
                  </div>
                </div>

                {/* 可选：登录前网络代理配置 */}
                <div className="rounded-2xl bg-surface-container px-4 py-2.5 text-xs">
                  <div className="flex items-center justify-between gap-2">
                    <span className="flex items-center gap-2 text-on-surface-variant">
                      <Network className="h-3.5 w-3.5 shrink-0 text-primary" aria-hidden="true" />
                      <span>
                        网络代理：
                        <strong className="font-medium text-on-surface">
                          {proxyInput.trim() ? '已配置' : '直连'}
                        </strong>
                      </span>
                    </span>
                    <button
                      type="button"
                      disabled={loading || importingCookies}
                      onClick={() => setShowProxyInput((v) => !v)}
                      className="rounded-full px-2 py-1 text-[11px] font-medium text-primary hover:bg-primary/10 disabled:opacity-50"
                    >
                      {showProxyInput ? '收起' : '配置代理'}
                    </button>
                  </div>
                  {showProxyInput && (
                    <div className="mt-2.5 space-y-1.5 animate-fade-in">
                      <Input
                        id="login-proxy"
                        aria-label="登录代理地址"
                        value={proxyInput}
                        disabled={loading || importingCookies}
                        onChange={(e) => {
                          setProxyInput(e.target.value);
                          if (error) setError('');
                        }}
                        onBlur={() => {
                          if (proxyValid && proxyInput.trim() !== (config?.proxy || '').trim()) {
                            void onSaveProxy?.(proxyInput.trim());
                          }
                        }}
                        placeholder="http://127.0.0.1:7890 或 socks5://127.0.0.1:1080"
                        className="h-9 font-mono text-xs"
                      />
                      {!proxyValid ? (
                        <p role="alert" className="text-[11px] text-error">
                          代理格式无效，请以 http://、https:// 或 socks5:// 开头
                        </p>
                      ) : (
                        <p className="text-[11px] text-on-surface-variant">
                          将同时用于学校检索、登录浏览器与后续刷课任务（留空直连）
                        </p>
                      )}
                    </div>
                  )}
                </div>

                {/* 实时登录 / Cookie 导入进度提示 */}
                {importingCookies && (
                  <div
                    role="status"
                    className="flex items-start gap-2.5 rounded-2xl bg-primary-container px-4 py-3 text-xs leading-relaxed text-on-primary-container animate-fade-in"
                  >
                    <Loader2 className="mt-0.5 h-3.5 w-3.5 shrink-0 animate-spin" aria-hidden="true" />
                    <span>正在校验并导入智慧树 Cookie，完成后将自动恢复登录...</span>
                  </div>
                )}

                {loading && statusInfo && (
                  <div
                    role="status"
                    className={`flex items-start gap-2.5 rounded-2xl px-4 py-3 text-xs leading-relaxed animate-fade-in ${
                      statusInfo.needsAction
                        ? 'bg-warning-container text-on-warning-container'
                        : 'bg-primary-container text-on-primary-container'
                    }`}
                  >
                    <Loader2 className="mt-0.5 h-3.5 w-3.5 shrink-0 animate-spin" aria-hidden="true" />
                    <span>{statusInfo.message}</span>
                  </div>
                )}

                {error && (
                  <div
                    id={errId}
                    role="alert"
                    className="flex items-start gap-2.5 rounded-2xl bg-error-container px-4 py-3 text-[13px] leading-relaxed text-on-error-container animate-fade-in"
                  >
                    <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
                    <span>{error}</span>
                  </div>
                )}

                {(mode !== 'qrcode' || loading) && (
                  <div className="flex gap-2.5 pt-2">
                    <Button
                      type="submit"
                      size="lg"
                      className="flex-1"
                      disabled={loading || importingCookies || !activeBrowser || !proxyValid}
                    >
                      {loading ? (
                        <>
                          <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
                          {statusInfo?.phase === 'fetching_courses' ? '正在识别课程...' : '等待登录完成...'}
                        </>
                      ) : (
                        <>
                          <LogIn className="h-4 w-4" aria-hidden="true" />
                          登录并自动识别课程
                        </>
                      )}
                    </Button>
                    {loading && (
                      <Button
                        type="button"
                        variant="tonal"
                        size="lg"
                        onClick={handleCancelLogin}
                      >
                        取消
                      </Button>
                    )}
                  </div>
                )}
              </form>
            </>
          )}
        </div>

        {/* 底部辅助信息与 Cookie 导入 */}
        <div className="mt-6 flex flex-col items-center gap-2 text-center text-xs text-on-surface-variant">
          <div className="flex flex-wrap items-center justify-center gap-3">
            <span className="inline-flex items-center gap-1.5">
              <KeyRound className="h-3.5 w-3.5" aria-hidden="true" />
              {mode === 'phone' && rememberCredentials
                ? '凭证保存在本机 data/cookies.json；已勾选记住密码，登录成功后将保存至本机 config.ini'
                : '密码默认不落盘；仅在勾选记住密码时保存在本机 config.ini'}
            </span>
            {!loading && !importingCookies && (
              <button
                type="button"
                onClick={onImportCookies}
                className="inline-flex items-center gap-1 rounded-full px-2.5 py-1 font-medium text-primary hover:bg-primary/10"
              >
                <Upload className="h-3.5 w-3.5" />
                导入 Cookie
              </button>
            )}
          </div>
          <p className="leading-relaxed opacity-80">
            本程序仅供学习和研究使用，请勿用于商业或非法用途。
          </p>
        </div>
      </div>
    </main>
  );
};

export default LoginView;
