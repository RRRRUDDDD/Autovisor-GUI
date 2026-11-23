import { useEffect, useRef, useState } from 'react';
import {
  Loader2,
  ArrowLeft,
  CheckCircle2,
  AlertCircle,
  Play,
  Clock,
  MonitorPlay,
  StopCircle,
  ShieldAlert,
  BookCheck,
  FolderOpen,
  RotateCcw,
  Power,
} from 'lucide-react';
import Button from './ui/Button';
import ExecutionLog from './ExecutionLog';
import { ScheduledShutdownBanner, ShutdownStatusText } from './ShutdownBanner';

function formatElapsed(startSec, endSec) {
  if (!startSec) return '--';
  const end = endSec || Date.now() / 1000;
  const total = Math.max(0, Math.floor(end - startSec));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return `${h}时 ${String(m).padStart(2, '0')}分 ${String(s).padStart(2, '0')}秒`;
  return `${m}分 ${String(s).padStart(2, '0')}秒`;
}

function formatStartTime(sec) {
  if (!sec) return '--';
  const d = new Date(sec * 1000);
  return d.toLocaleTimeString('zh-CN', { hour12: false });
}

const MODE_LABELS = {
  study: '自动学习进度监控',
  check_browser: '浏览器与登录状态检查',
  check_course: '课程目录结构只读检测',
  import_cookies: '导入智慧树 Cookie',
};

const ProgressView = ({
  runState,
  config,
  onBack,
  onStop,
  onRestart,
  onOpenPath,
  onCancelShutdown,
  onToggleDebugLog,
  stopping = false,
  cancellingShutdown = false,
  cancelShutdownError = '',
}) => {
  const [stopConfirming, setStopConfirming] = useState(false);
  const [nowTick, setNowTick] = useState(() => Date.now() / 1000);
  const stopConfirmTimer = useRef(null);

  const status = runState?.status || 'idle';
  const running = status === 'running';
  const isReviewMode = runState?.mode === 'study' && Boolean(runState?.reviewMode);
  const terminal = ['completed', 'error', 'cancelled', 'time_limit'].includes(status);
  const hasLogs = (runState?.logs?.length || 0) > 0;

  useEffect(() => {
    if (!running) return undefined;
    const timer = setInterval(() => setNowTick(Date.now() / 1000), 1000);
    return () => clearInterval(timer);
  }, [running]);

  useEffect(() => {
    return () => {
      if (stopConfirmTimer.current) clearTimeout(stopConfirmTimer.current);
    };
  }, []);

  const clearStopConfirm = () => {
    if (stopConfirmTimer.current) {
      clearTimeout(stopConfirmTimer.current);
      stopConfirmTimer.current = null;
    }
    setStopConfirming(false);
  };

  const handleStopClick = async () => {
    if (stopping || !running) return;
    if (!stopConfirming) {
      setStopConfirming(true);
      stopConfirmTimer.current = setTimeout(() => {
        stopConfirmTimer.current = null;
        setStopConfirming(false);
      }, 5000);
      return;
    }
    clearStopConfirm();
    await onStop();
  };

  if (status === 'idle' && !hasLogs) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center rounded-[28px] bg-surface-container-low px-8 py-20 text-center animate-stagger-up">
        <div className="flex h-16 w-16 items-center justify-center rounded-[22px] bg-primary-container text-on-primary-container">
          <MonitorPlay className="h-8 w-8" aria-hidden="true" />
        </div>
        <h2 className="mt-5 text-lg font-semibold text-on-surface">当前暂无运行中的学习任务</h2>
        <p className="mt-2 max-w-md text-sm leading-relaxed text-on-surface-variant">
          在「自动学习」勾选课程并启动后，这里会实时展示播放进度、当前课时、累计时长和终端日志。
        </p>
        <div className="mt-6 flex flex-wrap items-center justify-center gap-3">
          <Button onClick={onBack}>
            <ArrowLeft className="h-4 w-4" aria-hidden="true" />
            前往课程队列
          </Button>
        </div>
      </div>
    );
  }

  const statusMeta = (() => {
    switch (status) {
      case 'running':
        return stopping
          ? {
              text: '正在停止',
              badgeCls: 'bg-surface-container-highest text-on-surface-variant',
              icon: Loader2,
            }
          : {
              text: isReviewMode ? '复习中' : runState?.mode === 'study' ? '学习中' : '执行中',
              badgeCls: isReviewMode
                ? 'bg-tertiary-container text-on-tertiary-container'
                : 'bg-primary-container text-on-primary-container',
              icon: Play,
            };
      case 'completed':
        return {
          text: '已完成',
          badgeCls: 'bg-success-container text-on-success-container',
          icon: CheckCircle2,
        };
      case 'time_limit':
        return {
          text: '已达时限',
          badgeCls: 'bg-warning-container text-on-warning-container',
          icon: Clock,
        };
      case 'error':
        return {
          text: '异常结束',
          badgeCls: 'bg-error-container text-on-error-container',
          icon: AlertCircle,
        };
      case 'cancelled':
        return {
          text: '已停止',
          badgeCls: 'bg-surface-container-highest text-on-surface-variant',
          icon: StopCircle,
        };
      default:
        return {
          text: '就绪',
          badgeCls: 'bg-surface-container-highest text-on-surface-variant',
          icon: Clock,
        };
    }
  })();

  const lessonPct = Math.max(0, Math.min(100, Math.round(runState?.lessonProgress || 0)));
  const courseTotal =
    runState?.courseTotal || (config?.courses || []).filter((c) => c.enabled).length;
  const courseIndex = runState?.courseIndex || 0;
  const completedLessons = runState?.completedLessons || [];
  const shutdownInfo = runState?.shutdown || null;
  const shutdownScheduled = shutdownInfo?.state === 'scheduled';
  const shutdownCancelled = shutdownInfo?.state === 'cancelled';
  const shutdownFailed = shutdownInfo?.state === 'failed';
  const shutdownArmed =
    running && runState?.shutdownOnComplete === true && !shutdownCancelled;
  const showShutdownStatus =
    shutdownArmed || shutdownScheduled || shutdownCancelled || shutdownFailed;

  return (
    <div className="space-y-5 pb-6">
      {/* 顶部任务控制条 */}
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-[24px] bg-surface-container-low px-5 py-3.5 animate-stagger-up">
        <div className="flex min-w-0 items-center gap-3">
          <span
            className={`inline-flex items-center gap-1.5 rounded-full px-3 py-1 text-xs font-semibold ${statusMeta.badgeCls}`}
          >
            <statusMeta.icon
              className={`h-3.5 w-3.5 ${running && stopping ? 'animate-spin' : ''}`}
              aria-hidden="true"
            />
            {statusMeta.text}
          </span>
          <span className="truncate text-sm font-semibold text-on-surface">
            {isReviewMode
              ? '复习模式进度监控（刷习惯分）'
              : MODE_LABELS[runState?.mode] || '学习进度监控'}
          </span>
        </div>

        <div className="flex shrink-0 flex-wrap items-center gap-2">
          {running && (
            <Button
              variant={stopConfirming ? 'destructive' : 'destructive-tonal'}
              size="sm"
              onClick={handleStopClick}
              onBlur={clearStopConfirm}
              disabled={stopping}
            >
              {stopping ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
              ) : (
                <StopCircle className="h-3.5 w-3.5" aria-hidden="true" />
              )}
              {stopping ? '正在停止' : stopConfirming ? '确认停止' : '停止任务'}
            </Button>
          )}
          {terminal && runState?.mode === 'study' && (
            <Button variant="tonal" size="sm" onClick={onRestart}>
              <RotateCcw className="h-3.5 w-3.5" aria-hidden="true" />
              再次运行
            </Button>
          )}
          <Button onClick={onBack} size="sm" variant={terminal ? 'default' : 'outline'}>
            <ArrowLeft className="h-3.5 w-3.5" aria-hidden="true" />
            返回课程配置
          </Button>
        </div>
      </div>

      {/* 完成后关机状态提示条 */}
      {shutdownScheduled && (
        <ScheduledShutdownBanner
          shutdownInfo={shutdownInfo}
          cancelling={cancellingShutdown}
          cancelError={cancelShutdownError}
          onCancel={onCancelShutdown}
          className=""
        />
      )}
      {shutdownArmed && (
        <div
          role="status"
          className="flex flex-wrap items-center justify-between gap-3 rounded-[24px] bg-warning-container/65 px-5 py-3.5 text-on-warning-container animate-fade-in"
        >
          <div className="flex min-w-0 items-start gap-2.5">
            <Power className="mt-0.5 h-4.5 w-4.5 shrink-0" aria-hidden="true" />
            <div className="min-w-0">
              <p className="text-sm font-medium">
                已开启完成后自动关机，刷课任务结束后将倒计时 60 秒自动关机。
              </p>
              {cancelShutdownError && (
                <p className="mt-1 text-xs font-medium text-error">{cancelShutdownError}</p>
              )}
            </div>
          </div>
          {onCancelShutdown && (
            <Button
              variant="outline"
              size="sm"
              onClick={onCancelShutdown}
              disabled={cancellingShutdown}
            >
              {cancellingShutdown && (
                <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
              )}
              {cancellingShutdown ? '正在取消' : '取消关机'}
            </Button>
          )}
        </div>
      )}
      {shutdownCancelled && (
        <p
          role="status"
          className="rounded-[24px] bg-surface-container-low px-5 py-3.5 text-sm text-on-surface-variant animate-fade-in"
        >
          已取消自动关机，系统不会自动关闭。
        </p>
      )}
      {shutdownFailed && (
        <div
          role="alert"
          className="flex items-start gap-2.5 rounded-[24px] bg-error-container px-5 py-4 text-sm text-on-error-container animate-fade-in"
        >
          <Power className="mt-0.5 h-4.5 w-4.5 shrink-0" aria-hidden="true" />
          <span>自动关机启动失败：{shutdownInfo?.error || '系统拒绝了关机请求'}</span>
        </div>
      )}

      {/* 需要用户介入提醒 */}
      {running && runState?.needsAttention && (
        <div
          role="alert"
          className="flex items-start gap-3 rounded-[24px] bg-warning-container px-5 py-4 text-sm text-on-warning-container animate-fade-in"
        >
          <ShieldAlert className="mt-0.5 h-5 w-5 shrink-0" aria-hidden="true" />
          <div className="min-w-0">
            <p className="font-semibold">需要留意浏览器窗口</p>
            <p className="mt-0.5 text-[13px] leading-relaxed opacity-90">
              {runState.needsAttention}
            </p>
          </div>
        </div>
      )}

      <div className="grid grid-cols-1 gap-5 lg:grid-cols-[minmax(0,1fr)_clamp(20rem,28vw,25rem)] lg:items-start">
        {/* 右侧栏：实时进度 + 运行指标 */}
        <aside
          className="order-first space-y-5 animate-stagger-up lg:order-2 lg:sticky lg:top-4"
          style={{ animationDelay: '60ms' }}
        >
          {/* 实时进度卡片 */}
          <section className="rounded-[24px] bg-surface-container-low p-5">
            <div className="mb-3 flex items-center justify-between gap-2">
              <h2 className="text-base font-semibold text-on-surface">当前进度</h2>
              {runState?.mode === 'study' && courseTotal > 0 && (
                <span className="rounded-full bg-secondary-container px-2.5 py-0.5 text-xs font-medium text-on-secondary-container tnum">
                  课程 {courseIndex} / {courseTotal}
                </span>
              )}
            </div>

            <div className="mb-2 flex items-center justify-between text-xs text-on-surface-variant tnum">
              <span>
                阶段：<strong className="font-semibold text-on-surface">{runState?.stage || '等待启动'}</strong>
              </span>
              <span className="font-mono font-semibold text-primary">
                {status === 'completed' ? 100 : lessonPct}%
                {runState?.remainingMinutes !== null && runState?.remainingMinutes !== undefined
                  ? ` · 剩余 ${runState.remainingMinutes} min`
                  : ''}
              </span>
            </div>

            {/* M3 Linear Progress Indicator */}
            <div
              className="h-3 w-full overflow-hidden rounded-full bg-surface-container-highest"
              role="progressbar"
              aria-valuenow={lessonPct}
              aria-valuemin={0}
              aria-valuemax={100}
            >
              <div
                className={`h-full rounded-full transition-[width] duration-500 ease-m3 ${
                  status === 'error'
                    ? 'bg-error'
                    : status === 'completed'
                      ? 'bg-success'
                      : 'bg-primary'
                }`}
                style={{ width: `${status === 'completed' ? 100 : lessonPct}%` }}
              />
            </div>
            <p className="mt-1.5 text-xs text-on-surface-variant">
              {runState?.progressDesc || '课时播放进度'}
            </p>

            {/* 正在学习的课程 / 课时 */}
            {running && (runState?.currentCourse || runState?.currentLesson) && (
              <div className="mt-4 flex items-start gap-3 rounded-2xl bg-primary-container/55 p-4 text-on-primary-container">
                <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-surface-container-lowest/90 text-primary">
                  <Loader2 className="h-4.5 w-4.5 animate-spin" aria-hidden="true" />
                </div>
                <div className="min-w-0 flex-1">
                  <p className="text-[11px] font-semibold uppercase tracking-wide opacity-80">
                    {isReviewMode ? '正在复习' : '正在学习'}
                  </p>
                  {runState.currentCourse && (
                    <h3
                      className="mt-0.5 truncate text-sm font-semibold"
                      title={runState.currentCourse}
                    >
                      {runState.currentCourse}
                    </h3>
                  )}
                  {runState.currentLesson && (
                    <p className="mt-1.5 flex items-start gap-1.5 text-xs">
                      <span className="shrink-0 rounded-full bg-primary px-2 py-0.5 text-[10px] font-medium text-on-primary">
                        课时
                      </span>
                      <span className="min-w-0 break-words">{runState.currentLesson}</span>
                    </p>
                  )}
                  {runState.catalogType && (
                    <p className="mt-1 text-[11px] opacity-75">目录适配：{runState.catalogType}</p>
                  )}
                </div>
              </div>
            )}

            {/* 错误提示 */}
            {runState?.errorMessage && (
              <div
                role="alert"
                className="mt-4 flex items-start gap-2.5 rounded-2xl bg-error-container px-4 py-3 text-on-error-container animate-fade-in"
              >
                <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
                <div className="min-w-0">
                  <p className="text-xs font-semibold">执行异常</p>
                  <p className="mt-0.5 break-words text-xs leading-relaxed opacity-90">
                    {runState.errorMessage}
                  </p>
                </div>
              </div>
            )}
          </section>

          {/* 任务运行信息（4 格 M3 Tonal Metric Tiles） */}
          <section className="rounded-[24px] bg-surface-container-low p-5">
            <h2 className="mb-3.5 text-base font-semibold text-on-surface">运行统计</h2>
            <dl className="grid grid-cols-2 gap-2.5 text-xs">
              <div className="rounded-2xl bg-surface-container p-3.5">
                <dt className="text-on-surface-variant">开始时间</dt>
                <dd className="mt-1 text-sm font-semibold text-on-surface tnum">
                  {formatStartTime(runState?.startedAt)}
                </dd>
              </div>
              <div className="rounded-2xl bg-surface-container p-3.5">
                <dt className="text-on-surface-variant">运行耗时</dt>
                <dd className="mt-1 text-sm font-semibold text-on-surface tnum">
                  {formatElapsed(
                    runState?.startedAt,
                    runState?.finishedAt || (running ? nowTick : null)
                  )}
                </dd>
              </div>
              <div className="rounded-2xl bg-surface-container p-3.5">
                <dt className="text-on-surface-variant">本轮完成课时</dt>
                <dd className="mt-1 text-sm font-semibold text-on-surface tnum">
                  {completedLessons.length} 节
                </dd>
              </div>
              <div className="rounded-2xl bg-surface-container p-3.5">
                <dt className="text-on-surface-variant">当前课累计</dt>
                <dd className="mt-1 text-sm font-semibold text-on-surface tnum">
                  {Number(runState?.courseElapsedMin || 0).toFixed(1)} min
                </dd>
              </div>
            </dl>

            {showShutdownStatus && (
              <div className="mt-3 flex items-center justify-between rounded-2xl bg-surface-container px-4 py-2.5 text-xs">
                <span className="text-on-surface-variant">完成后关机</span>
                <span
                  className={`font-semibold tnum ${
                    shutdownFailed
                      ? 'text-error'
                      : shutdownCancelled
                        ? 'text-on-surface-variant'
                        : 'text-warning'
                  }`}
                >
                  <ShutdownStatusText
                    shutdownScheduled={shutdownScheduled}
                    shutdownArmed={shutdownArmed}
                    shutdownCancelled={shutdownCancelled}
                    shutdownInfo={shutdownInfo}
                  />
                </span>
              </div>
            )}

            {runState?.exitCode !== null && runState?.exitCode !== undefined && (
              <div className="mt-3 flex items-center justify-between rounded-2xl bg-surface-container px-4 py-2.5 text-xs">
                <span className="text-on-surface-variant">进程退出码</span>
                <span className="font-mono font-medium text-on-surface tnum">
                  Exit {runState.exitCode}
                </span>
              </div>
            )}

            {runState?.logFilePath && (
              <div className="mt-3">
                <Button
                  variant="tonal"
                  size="sm"
                  className="w-full justify-start"
                  onClick={() => onOpenPath(runState.logFilePath)}
                >
                  <FolderOpen className="h-3.5 w-3.5 shrink-0" />
                  <span className="truncate font-mono text-xs">{runState.logFilePath}</span>
                </Button>
              </div>
            )}
          </section>
        </aside>

        {/* 主区域：已完成课时 + 实时日志 */}
        <div className="min-w-0 space-y-5 lg:order-1 animate-stagger-up">
          {completedLessons.length > 0 && (
            <section className="rounded-[24px] bg-surface-container-low p-5">
              <div className="mb-3 flex items-center justify-between">
                <h2 className="flex items-center gap-2 text-base font-semibold text-on-surface">
                  <BookCheck className="h-4.5 w-4.5 text-success" aria-hidden="true" />
                  本轮已完成课时
                </h2>
                <span className="rounded-full bg-success-container px-2.5 py-0.5 text-xs font-medium text-on-success-container tnum">
                  共 {completedLessons.length} 节
                </span>
              </div>
              <ul className="max-h-56 space-y-1.5 overflow-y-auto">
                {completedLessons.map((item, idx) => (
                  <li
                    key={`${item.title}-${idx}`}
                    className="flex items-center justify-between gap-3 rounded-2xl bg-surface-container px-3.5 py-2.5 text-xs"
                  >
                    <span className="flex min-w-0 items-center gap-2">
                      <CheckCircle2 className="h-4 w-4 shrink-0 text-success" />
                      <span className="truncate font-medium text-on-surface">{item.title}</span>
                    </span>
                    <span className="shrink-0 font-mono text-on-surface-variant tnum">
                      {item.time}
                    </span>
                  </li>
                ))}
              </ul>
            </section>
          )}

          <ExecutionLog
            logs={runState?.logs || []}
            truncated={Boolean(runState?.logsTruncated)}
            logFilePath={runState?.logFilePath || ''}
            onOpenLogFile={(path) => onOpenPath(path)}
            debugEnabled={Boolean(config?.enableDebugLog)}
            onToggleDebug={onToggleDebugLog}
          />
        </div>
      </div>
    </div>
  );
};

export default ProgressView;
