import { useState } from 'react';
import { Cpu, Download, ExternalLink, FileCode2, FolderOpen, Loader2, RefreshCw } from 'lucide-react';
import Button from '../ui/Button';
import { APP_VERSION } from '../../lib/appVersion';

export default function RuntimeEnvCard({
  runtime,
  taskRunning,
  restartingBackend,
  checkingUpdate,
  updateInfo,
  updateError,
  onCheckUpdate,
  onRestartBackend,
  onOpenPath,
  onOpenUrl,
}) {
  const [showUpdateNotes, setShowUpdateNotes] = useState(false);

  return (
    <section className="flex flex-col rounded-[24px] bg-surface-container-low p-6 lg:col-span-2">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl bg-primary-container text-on-primary-container">
            <Cpu className="h-5 w-5" aria-hidden="true" />
          </div>
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="text-base font-semibold text-on-surface">运行环境与版本</h2>
              <span className="rounded-full bg-surface-container-highest px-2.5 py-0.5 font-mono text-xs font-medium text-on-surface-variant">
                v{runtime?.version || APP_VERSION}
              </span>
              {runtime?.backendVersion && (
                <span className="rounded-full bg-surface-container-highest px-2.5 py-0.5 font-mono text-xs font-medium text-on-surface-variant">
                  Autovisor {runtime.backendVersion}
                </span>
              )}
              <span
                className={`rounded-full px-2.5 py-0.5 text-xs font-medium ${
                  runtime?.backendPhase === 'ready'
                    ? 'bg-success-container text-on-success-container'
                    : runtime?.backendPhase === 'failed'
                      ? 'bg-error-container text-on-error-container'
                      : 'bg-warning-container text-on-warning-container'
                }`}
              >
                引擎：
                {runtime?.backendPhase === 'ready'
                  ? '就绪'
                  : runtime?.backendPhase === 'failed'
                    ? '异常'
                    : runtime?.backendPhase === 'starting'
                      ? '启动中'
                      : runtime?.backendPhase === 'stopping'
                        ? '停止中'
                        : '未就绪'}
              </span>
            </div>
            <p className="mt-0.5 text-xs text-on-surface-variant">
              检查版本更新、重启内置学习引擎或快速打开数据目录与配置文件
            </p>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {onCheckUpdate && (
            <Button
              type="button"
              variant="tonal"
              size="sm"
              onClick={onCheckUpdate}
              disabled={checkingUpdate}
            >
              {checkingUpdate ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
              ) : (
                <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
              )}
              {checkingUpdate ? '检查中...' : '检查更新'}
            </Button>
          )}
          {onRestartBackend && (
            <Button
              type="button"
              variant="tonal"
              size="sm"
              onClick={onRestartBackend}
              disabled={restartingBackend || taskRunning}
              title={taskRunning ? '任务运行中，请先停止任务后再重启引擎' : '重启内置学习引擎'}
            >
              {restartingBackend ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
              ) : (
                <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
              )}
              {restartingBackend ? '重启中...' : '重启内置引擎'}
            </Button>
          )}
        </div>
      </div>

      <div className="space-y-3">
        {runtime?.backendPhase === 'failed' && runtime?.backendError && (
          <p role="alert" className="text-xs font-medium text-error">
            {runtime.backendError}
          </p>
        )}
        {updateError && (
          <p role="alert" className="text-xs font-medium text-error">
            {updateError}
          </p>
        )}
        {updateInfo && !updateError && (
          <div
            className={`rounded-2xl px-4 py-3 text-xs ${
              updateInfo.hasUpdate
                ? 'bg-primary-container/60 text-on-primary-container'
                : 'bg-surface-container text-on-surface-variant'
            }`}
          >
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="font-medium">
                {updateInfo.hasUpdate
                  ? `发现新版本 v${updateInfo.latestVersion}（当前 v${updateInfo.currentVersion}）`
                  : `当前已是最新版本（v${updateInfo.currentVersion}）`}
              </span>
              {updateInfo.hasUpdate && (
                <div className="flex flex-wrap items-center gap-2">
                  {updateInfo.notes && (
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      onClick={() => setShowUpdateNotes((v) => !v)}
                    >
                      {showUpdateNotes ? '收起更新日志' : '查看更新日志'}
                    </Button>
                  )}
                  {updateInfo.downloadUrl && onOpenUrl && (
                    <Button
                      type="button"
                      size="sm"
                      onClick={() => onOpenUrl(updateInfo.downloadUrl)}
                    >
                      <Download className="h-3.5 w-3.5" aria-hidden="true" />
                      直接下载
                    </Button>
                  )}
                  {updateInfo.releaseUrl && onOpenUrl && (
                    <Button
                      type="button"
                      variant="tonal"
                      size="sm"
                      onClick={() => onOpenUrl(updateInfo.releaseUrl)}
                    >
                      <ExternalLink className="h-3.5 w-3.5" aria-hidden="true" />
                      打开下载页
                    </Button>
                  )}
                </div>
              )}
            </div>
            {updateInfo.hasUpdate && showUpdateNotes && updateInfo.notes && (
              <pre className="mt-2.5 max-h-48 overflow-auto rounded-xl bg-surface/80 p-3 font-mono text-[11px] leading-relaxed text-on-surface whitespace-pre-wrap">
                {updateInfo.notes}
              </pre>
            )}
          </div>
        )}

        <div className="flex flex-wrap items-center gap-2 pt-1">
          {runtime?.baseDir && onOpenPath && (
            <Button
              type="button"
              variant="tonal"
              size="sm"
              onClick={() => onOpenPath(runtime.baseDir)}
            >
              <FolderOpen className="h-3.5 w-3.5" aria-hidden="true" />
              打开数据目录
            </Button>
          )}
          {runtime?.configPath && onOpenPath && (
            <Button
              type="button"
              variant="tonal"
              size="sm"
              onClick={() => onOpenPath(runtime.configPath)}
            >
              <FileCode2 className="h-3.5 w-3.5" aria-hidden="true" />
              打开 config.ini
            </Button>
          )}
          {taskRunning && (
            <span className="text-xs text-on-surface-variant">
              当前任务运行中，重启引擎前请先停止任务
            </span>
          )}
        </div>
      </div>
    </section>
  );
}
