import { useCallback, useState } from 'react';
import {
  AlertCircle,
  Check,
  CheckCircle2,
  Loader2,
  Play,
  SlidersHorizontal,
} from 'lucide-react';
import Button from '../ui/Button';
import Label from '../ui/Label';
import NumberInput from '../ui/NumberInput';
import Switch from '../ui/Switch';

const SPEED_PRESETS = [1.0, 1.2, 1.5, 1.8];

const TIME_PRESETS = [
  { label: '不限', value: 0 },
  { label: '25分', value: 25 },
  { label: '30分', value: 30 },
  { label: '45分', value: 45 },
  { label: '60分', value: 60 },
];

export default function StudyParamsSidebar({
  config,
  runtime,
  enabledCount,
  proxyValid,
  llmBaseValid,
  dirty,
  saving,
  saveStatus,
  starting,
  startError,
  taskRunning,
  hasRunHistory,
  refreshingCourses,
  onChangeConfig,
  onRetrySave,
  onStartStudy,
  onReturnToProgress,
}) {
  const [invalidFields, setInvalidFields] = useState({});

  const setFieldValid = useCallback((field, valid) => {
    setInvalidFields((current) => {
      if (Boolean(current[field]) === !valid) return current;
      const next = { ...current };
      if (valid) delete next[field];
      else next[field] = true;
      return next;
    });
  }, []);

  const updateField = (patch) => {
    onChangeConfig((prev) => ({ ...prev, ...patch }));
  };

  const canStart =
    enabledCount > 0 &&
    proxyValid &&
    llmBaseValid &&
    Object.keys(invalidFields).length === 0 &&
    !starting &&
    !taskRunning &&
    !refreshingCourses;

  return (
    <aside
      aria-label="任务配置"
      className="flex min-h-0 flex-col gap-3 animate-stagger-up"
      style={{ animationDelay: '60ms' }}
    >
      <div className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-[24px] bg-surface-container-low">
        <div className="flex shrink-0 items-center justify-between gap-2 px-5 pt-4 pb-2.5">
          <div className="flex items-center gap-2">
            <SlidersHorizontal className="h-4.5 w-4.5 text-primary" aria-hidden="true" />
            <h2 className="text-[15px] font-semibold text-on-surface">学习参数</h2>
          </div>
          {runtime?.cookieExists && (
            <span className="inline-flex items-center gap-1 rounded-full bg-success-container px-2.5 py-0.5 text-[11px] font-medium text-on-success-container">
              <CheckCircle2 className="h-3 w-3" />
              已登录免密
            </span>
          )}
        </div>

        <div
          className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-5 pb-4"
          role="region"
          tabIndex={0}
          aria-label="任务参数"
        >
          <div className="space-y-3.5 pt-0.5">
            <div className="space-y-2 rounded-2xl bg-surface-container px-3.5 py-3">
              <div className="flex items-center justify-between">
                <Label htmlFor="limitSpeed" className="text-on-surface">
                  播放倍速
                </Label>
                <span className="rounded-full bg-primary-container px-2.5 py-0.5 font-mono text-xs font-semibold text-on-primary-container tnum">
                  {Number(config.limitSpeed).toFixed(1)}x
                </span>
              </div>
              <input
                id="limitSpeed"
                type="range"
                min="0.5"
                max="1.8"
                step="0.1"
                value={config.limitSpeed}
                onChange={(e) => updateField({ limitSpeed: parseFloat(e.target.value) })}
                className="m3-slider w-full cursor-pointer"
              />
              <div className="flex flex-wrap items-center justify-between gap-1.5">
                <div className="flex flex-wrap gap-1.5">
                  {SPEED_PRESETS.map((spd) => {
                    const active = Math.abs(Number(config.limitSpeed) - spd) < 0.01;
                    return (
                      <button
                        key={spd}
                        type="button"
                        onClick={() => updateField({ limitSpeed: spd })}
                        className={`rounded-full px-2.5 py-0.5 font-mono text-[11px] font-medium transition-colors tnum ${
                          active
                            ? 'bg-secondary-container text-on-secondary-container'
                            : 'bg-surface-container-highest/70 text-on-surface-variant hover:bg-surface-container-highest'
                        }`}
                      >
                        {spd.toFixed(1)}x
                      </button>
                    );
                  })}
                </div>
                <span className="text-[11px] text-on-surface-variant tnum">上限 1.8x</span>
              </div>
            </div>

            <div className="space-y-1.5">
              <div className="flex items-center justify-between">
                <Label htmlFor="limitMaxTime">单门课程时长限制（分钟）</Label>
                <span className="text-xs text-on-surface-variant">0 = 不限制</span>
              </div>
              <NumberInput
                id="limitMaxTime"
                min={0}
                max={1440}
                step="5"
                value={config.limitMaxTime}
                onValueChange={(limitMaxTime) => updateField({ limitMaxTime })}
                onValidityChange={(valid) => setFieldValid('limitMaxTime', valid)}
                className="h-9"
              />
              <div className="flex flex-wrap gap-1.5 pt-0.5">
                {TIME_PRESETS.map((preset) => {
                  const active = Number(config.limitMaxTime) === preset.value;
                  return (
                    <button
                      key={preset.value}
                      type="button"
                      onClick={() => updateField({ limitMaxTime: preset.value })}
                      className={`inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-medium transition-colors ease-m3 ${
                        active
                          ? 'bg-secondary-container text-on-secondary-container'
                          : 'border border-outline/50 bg-transparent text-on-surface-variant hover:bg-on-surface/[0.06]'
                      }`}
                    >
                      {active && <Check className="h-3 w-3" strokeWidth={2.8} />}
                      {preset.label}
                    </button>
                  );
                })}
              </div>
            </div>

            <div className="space-y-0 border-t border-outline-variant/60 pt-2">
              <Switch
                id="reviewMode"
                checked={config.reviewMode === true}
                onChange={(reviewMode) => updateField({ reviewMode })}
                label="复习模式（刷习惯分）"
                description="忽略已完成状态重播视频；设有时长上限时循环刷满每日习惯分"
              />
              {config.reviewMode === true && (
                <div className="mb-2 mt-1 flex flex-wrap items-center justify-between gap-2 rounded-2xl bg-tertiary-container/60 px-3.5 py-2.5 text-xs text-on-tertiary-container animate-fade-in">
                  <span className="leading-relaxed">
                    {Number(config.limitMaxTime) > 0
                      ? `将循环重播视频直至每门课满 ${config.limitMaxTime} 分钟，并跳过文档与刷题阶段`
                      : '当前时长限制为「不限」，将完整重播一轮后结束；刷习惯分建议设为 25–30 分钟'}
                  </span>
                  {Number(config.limitMaxTime) === 0 && (
                    <button
                      type="button"
                      onClick={() => updateField({ limitMaxTime: 30 })}
                      className="shrink-0 rounded-full bg-tertiary px-2.5 py-1 text-[11px] font-medium text-on-tertiary transition-opacity hover:opacity-90"
                    >
                      设为 30 分钟
                    </button>
                  )}
                </div>
              )}
            </div>

            <div className="space-y-0 border-t border-outline-variant/60 pt-2">
              <Switch
                id="enableAutoCaptcha"
                checked={config.enableAutoCaptcha}
                onChange={(enableAutoCaptcha) => updateField({ enableAutoCaptcha })}
                label="自动跳过验证码"
                description="自动处理课中滑块与点选验证"
              />
              <Switch
                id="soundOff"
                checked={config.soundOff}
                onChange={(soundOff) => updateField({ soundOff })}
                label="静音播放"
                description="自动将视频音量设为 0，避免后台挂机出声"
              />
              <Switch
                id="enableHideWindow"
                checked={config.enableHideWindow !== false}
                onChange={(enableHideWindow) => updateField({ enableHideWindow })}
                label="隐藏浏览器窗口"
                description="移出屏幕后台学习（遇安全验证会自动前置）"
              />
              <Switch
                id="shutdownOnComplete"
                checked={config.shutdownOnComplete === true}
                onChange={(shutdownOnComplete) => updateField({ shutdownOnComplete })}
                label="刷课任务完成后自动关机"
                description="仅正常完成或达到单课时长上限时倒计时 60 秒自动关机；手动停止或出错不会关机"
              />
              {config.shutdownOnComplete === true && (
                <div className="mb-1 mt-1 rounded-2xl bg-warning-container/65 px-3.5 py-2.5 text-xs leading-relaxed text-on-warning-container animate-fade-in">
                  已开启完成后自动关机，刷课结束后将倒计时 60 秒关闭计算机，期间可在运行监控页随时取消。
                </div>
              )}
            </div>
          </div>
        </div>
      </div>

      <div className="shrink-0 space-y-2 rounded-[24px] bg-primary-container/50 p-3.5">
        {taskRunning ? (
          <Button className="h-11 w-full" onClick={onReturnToProgress}>
            <Loader2 className="h-4.5 w-4.5 animate-spin" aria-hidden="true" />
            返回运行中的学习任务
          </Button>
        ) : (
          <Button className="h-11 w-full" onClick={onStartStudy} disabled={!canStart}>
            {starting ? (
              <>
                <Loader2 className="h-4.5 w-4.5 animate-spin" aria-hidden="true" />
                {config.reviewMode ? '正在启动复习任务' : '正在启动学习任务'}
              </>
            ) : (
              <>
                <Play className="h-4.5 w-4.5" aria-hidden="true" />
                {config.reviewMode ? '开始复习刷分' : '开始刷课'}
              </>
            )}
          </Button>
        )}

        <p className="text-center text-xs text-on-primary-container/85">
          {!proxyValid
            ? '代理服务器地址格式无效，请在「系统设置」中修正'
            : !llmBaseValid
              ? '大模型 API Base 地址格式无效，请在「系统设置」中修正'
              : taskRunning
                ? '当前有任务在后台运行，可随时查看实时监控'
                : enabledCount
                  ? `将按顺序${config.reviewMode ? '复习' : '学习'} ${enabledCount} 门课程 · ${Number(config.limitSpeed).toFixed(1)}x${config.limitMaxTime > 0 ? ` · 每课 ${config.limitMaxTime} 分钟` : ' · 不限时'}${config.shutdownOnComplete ? ' · 完成后自动关机' : ''}`
                  : '请至少勾选一门课程'}
        </p>

        {hasRunHistory && !taskRunning && (
          <Button variant="ghost" size="sm" className="w-full" onClick={onReturnToProgress}>
            查看上次运行记录
          </Button>
        )}

        {startError && (
          <div
            role="alert"
            className="flex items-start gap-2 rounded-2xl bg-error-container px-3.5 py-2.5 text-xs leading-relaxed text-on-error-container animate-fade-in"
          >
            <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
            <span>{startError}</span>
          </div>
        )}

        {saveStatus?.type === 'error' ? (
          <div
            role="alert"
            className="flex items-center justify-between gap-2 rounded-2xl bg-error-container px-3.5 py-2 text-xs text-on-error-container animate-fade-in"
          >
            <span className="flex min-w-0 items-center gap-1.5 font-medium">
              <AlertCircle className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
              <span className="truncate">{saveStatus.message}</span>
            </span>
            {onRetrySave && (
              <button
                type="button"
                onClick={onRetrySave}
                disabled={saving || !proxyValid || !llmBaseValid}
                className="shrink-0 rounded-full bg-on-error-container/10 px-2.5 py-0.5 font-medium hover:bg-on-error-container/15 disabled:opacity-50"
              >
                重试
              </button>
            )}
          </div>
        ) : saving || (dirty && proxyValid && llmBaseValid) ? (
          <p
            role="status"
            className="flex items-center justify-center gap-1.5 text-xs font-medium text-on-primary-container/80"
          >
            <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" />
            正在自动保存配置...
          </p>
        ) : saveStatus?.type === 'success' ? (
          <p
            role="status"
            className="flex items-center justify-center gap-1.5 text-xs font-medium text-success animate-fade-in"
          >
            <CheckCircle2 className="h-3.5 w-3.5" aria-hidden="true" />
            {saveStatus.message}
          </p>
        ) : null}
      </div>
    </aside>
  );
}
