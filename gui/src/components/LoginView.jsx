import { useState, useEffect, useRef, useCallback } from 'react';
import {
  GraduationCap,
  LogIn,
  Loader2,
  KeyRound,
  AlertCircle,
  Smartphone,
  School,
  QrCode,
  Upload,
  Sun,
  Moon,
  Check,
} from 'lucide-react';
import Button from './ui/Button';
import PhoneLoginForm from './login/PhoneLoginForm';
import StudentLoginForm from './login/StudentLoginForm';
import QrLoginPanel from './login/QrLoginPanel';
import LoginEnvBar from './login/LoginEnvBar';
import { useQrLogin } from './login/useQrLogin';
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
  onClearSavedLogin,
}) => {
  const savedLogin = runtime?.savedLogin || null;
  const savedMode = savedLogin?.loginMode || runtime?.session?.loginMode || '';
  const initialMode =
    savedMode === 'student'
      ? 'student'
      : savedMode === 'qrcode'
        ? 'qrcode'
        : 'phone';

  const hasSavedPhone = Boolean(
    savedLogin?.rememberPhone &&
      (savedLogin?.phoneUsername ||
        savedLogin?.phonePassword ||
        savedLogin?.hasSavedPhonePassword)
  );
  const hasSavedStudent = Boolean(
    savedLogin?.rememberStudent &&
      (savedLogin?.schoolName ||
        savedLogin?.studentId ||
        savedLogin?.studentPassword ||
        savedLogin?.hasSavedStudentPassword)
  );
  const fallbackConfigPhone = Boolean(
    !savedLogin && config?.username && config?.password
  );

  const [mode, setMode] = useState(initialMode);
  const [username, setUsername] = useState(() =>
    hasSavedPhone ? savedLogin.phoneUsername || '' : config?.username || ''
  );
  const [phonePassword, setPhonePassword] = useState(() =>
    hasSavedPhone
      ? savedLogin.phonePassword || ''
      : fallbackConfigPhone
        ? config?.password || ''
        : ''
  );
  const [schoolQuery, setSchoolQuery] = useState(() =>
    hasSavedStudent
      ? savedLogin.schoolName || ''
      : runtime?.session?.schoolName || ''
  );
  const [selectedSchool, setSelectedSchool] = useState(() => {
    if (hasSavedStudent && savedLogin.schoolName) {
      return { schoolId: savedLogin.schoolId || '', name: savedLogin.schoolName };
    }
    if (runtime?.session?.schoolName) {
      return { schoolId: runtime.session.schoolId || '', name: runtime.session.schoolName };
    }
    return null;
  });
  const [studentId, setStudentId] = useState(() =>
    hasSavedStudent
      ? savedLogin.studentId || ''
      : runtime?.session?.loginMode === 'student'
        ? runtime?.session?.account || ''
        : ''
  );
  const [studentPassword, setStudentPassword] = useState(() =>
    hasSavedStudent ? savedLogin.studentPassword || '' : ''
  );
  const [showPassword, setShowPassword] = useState(false);
  const [rememberCredentials, setRememberCredentials] = useState(
    () => hasSavedPhone || fallbackConfigPhone
  );
  const [rememberStudentCredentials, setRememberStudentCredentials] = useState(
    () => hasSavedStudent
  );
  const [clearingSaved, setClearingSaved] = useState(false);
  const [proxyInput, setProxyInput] = useState(() => config?.proxy || '');
  const [showProxyInput, setShowProxyInput] = useState(() => Boolean(config?.proxy));
  const [loading, setLoading] = useState(false);
  const [restoringCookie, setRestoringCookie] = useState(false);
  const [statusInfo, setStatusInfo] = useState(null);
  const [error, setError] = useState('');

  const autoAttemptedRef = useRef(false);
  const lastImportNonceRef = useRef(cookieImportState?.nonce || 0);
  const mountedRef = useRef(true);
  const modeRef = useRef(mode);
  modeRef.current = mode;

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
        if (!request?.rememberCredentials) {
          if (request?.mode === 'phone') setPhonePassword('');
          else if (request?.mode === 'student') setStudentPassword('');
        }
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

  const clearError = useCallback(() => setError(''), []);

  const {
    qrState,
    startInAppQrLogin,
    cancelActiveQrSession,
    resetQrState,
    handleSaveQrPng,
  } = useQrLogin({
    mode,
    modeRef,
    mountedRef,
    restoringCookie,
    loading,
    effectiveProxy,
    browserExe: activeBrowser?.exePath || config?.exePath || '',
    onClearError: clearError,
    onConfirmed: runLogin,
  });

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      cancelActiveQrSession();
    };
  }, [cancelActiveQrSession]);

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

  const handleBrowserQrFallback = async () => {
    cancelActiveQrSession();
    resetQrState();
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
      if (!phonePassword) {
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
      if (!studentPassword) {
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
      password: mode === 'phone' ? phonePassword : mode === 'student' ? studentPassword : '',
      browserExe: activeBrowser?.exePath || config?.exePath || '',
      proxy: trimmedProxy,
      rememberCredentials:
        mode === 'phone'
          ? rememberCredentials
          : mode === 'student'
            ? rememberStudentCredentials
            : false,
    });
  };

  const handleClearRemembered = async (targetMode) => {
    if (clearingSaved || loading) return;
    setClearingSaved(true);
    try {
      if (onClearSavedLogin) {
        await onClearSavedLogin(targetMode);
      } else {
        await tauriApi.clearSavedLogin(targetMode);
      }
      if (!mountedRef.current) return;
      if (targetMode === 'phone') {
        setUsername('');
        setPhonePassword('');
        setRememberCredentials(false);
      } else if (targetMode === 'student') {
        setSchoolQuery('');
        setSelectedSchool(null);
        setStudentId('');
        setStudentPassword('');
        setRememberStudentCredentials(false);
      }
    } catch (err) {
      if (mountedRef.current) {
        setError(err?.message || String(err) || '清除已记住的账号密码失败');
      }
    } finally {
      if (mountedRef.current) setClearingSaved(false);
    }
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
              <Button variant="tonal" size="sm" className="mt-6" onClick={handleCancelLogin}>
                改用其他账号登录
              </Button>
            </div>
          ) : (
            <>
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
                        modeRef.current = tab.id;
                        if (mode === 'qrcode' && tab.id !== 'qrcode') {
                          cancelActiveQrSession();
                          resetQrState();
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
                  <PhoneLoginForm
                    username={username}
                    onChangeUsername={setUsername}
                    password={phonePassword}
                    onChangePassword={setPhonePassword}
                    showPassword={showPassword}
                    onToggleShowPassword={() => setShowPassword((s) => !s)}
                    rememberCredentials={rememberCredentials}
                    onChangeRemember={setRememberCredentials}
                    hasSavedPhone={hasSavedPhone}
                    loading={loading}
                    clearingSaved={clearingSaved}
                    onClearRemembered={() => void handleClearRemembered('phone')}
                  />
                )}

                {mode === 'student' && (
                  <StudentLoginForm
                    schoolQuery={schoolQuery}
                    onChangeSchoolQuery={setSchoolQuery}
                    selectedSchool={selectedSchool}
                    onSelectSchool={setSelectedSchool}
                    studentId={studentId}
                    onChangeStudentId={setStudentId}
                    password={studentPassword}
                    onChangePassword={setStudentPassword}
                    showPassword={showPassword}
                    onToggleShowPassword={() => setShowPassword((s) => !s)}
                    rememberCredentials={rememberStudentCredentials}
                    onChangeRemember={setRememberStudentCredentials}
                    hasSavedStudent={hasSavedStudent}
                    effectiveProxy={effectiveProxy}
                    loading={loading}
                    clearingSaved={clearingSaved}
                    mountedRef={mountedRef}
                    onClearRemembered={() => void handleClearRemembered('student')}
                  />
                )}

                {mode === 'qrcode' && (
                  <QrLoginPanel
                    qrState={qrState}
                    loading={loading}
                    importingCookies={importingCookies}
                    activeBrowser={activeBrowser}
                    proxyValid={proxyValid}
                    onStartQr={() => void startInAppQrLogin()}
                    onSaveQrPng={() => void handleSaveQrPng()}
                    onBrowserFallback={() => void handleBrowserQrFallback()}
                  />
                )}

                <LoginEnvBar
                  detectedBrowsers={detectedBrowsers}
                  activeBrowser={activeBrowser}
                  loading={loading}
                  importingCookies={importingCookies}
                  proxyInput={proxyInput}
                  proxyValid={proxyValid}
                  showProxyInput={showProxyInput}
                  configProxy={config?.proxy}
                  onSelectBrowser={onSelectBrowser}
                  onRedetectBrowsers={onRedetectBrowsers}
                  onPickBrowserExe={onPickBrowserExe}
                  onToggleProxyInput={() => setShowProxyInput((v) => !v)}
                  onChangeProxy={(val) => {
                    setProxyInput(val);
                    if (error) setError('');
                  }}
                  onSaveProxy={onSaveProxy}
                />

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

        <div className="mt-6 flex flex-col items-center gap-2 text-center text-xs text-on-surface-variant">
          <div className="flex flex-wrap items-center justify-center gap-3">
            <span className="inline-flex items-center gap-1.5">
              <KeyRound className="h-3.5 w-3.5" aria-hidden="true" />
              {mode === 'phone' && rememberCredentials
                ? '凭证保存在本机 data/cookies.json；已勾选记住登录信息，登录成功后自动保存'
                : mode === 'student' && rememberStudentCredentials
                  ? '凭证保存在本机 data/cookies.json；已勾选记住学校与学号密码，下次打开自动填充'
                  : '密码默认不落盘；仅在勾选记住登录信息后保存在本机'}
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
