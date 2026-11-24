import { useCallback, useEffect, useRef, useState } from 'react';
import { ExternalLink, FolderOpen, RefreshCw, ScrollText } from 'lucide-react';
import Button from '../ui/Button';
import Label from '../ui/Label';
import NumberInput from '../ui/NumberInput';
import Switch from '../ui/Switch';

function formatLogModifiedTime(file) {
  const epoch = Number(file?.modifiedEpoch);
  if (Number.isFinite(epoch) && epoch > 0) {
    const dt = new Date(epoch * 1000);
    if (!Number.isNaN(dt.getTime())) {
      const mm = String(dt.getMonth() + 1).padStart(2, '0');
      const dd = String(dt.getDate()).padStart(2, '0');
      const hh = String(dt.getHours()).padStart(2, '0');
      const mi = String(dt.getMinutes()).padStart(2, '0');
      return `${mm}-${dd} ${hh}:${mi}`;
    }
  }
  return file?.modifiedAt || '--';
}

export default function LogHistoryCard({
  config,
  logsDir,
  updateField,
  onNumberValidity,
  onToggleDebugLog,
  onListLogFiles,
  onReadLogFile,
  onOpenPath,
}) {
  const [logFiles, setLogFiles] = useState([]);
  const [loadingLogs, setLoadingLogs] = useState(false);
  const [selectedLog, setSelectedLog] = useState(null);
  const [logContent, setLogContent] = useState('');
  const logReadSeqRef = useRef(0);

  const refreshLogs = useCallback(async () => {
    if (!onListLogFiles) return;
    setLoadingLogs(true);
    try {
      const files = await onListLogFiles();
      setLogFiles(Array.isArray(files) ? files : []);
    } finally {
      setLoadingLogs(false);
    }
  }, [onListLogFiles]);

  useEffect(() => {
    if (!onListLogFiles) return undefined;
    let active = true;
    setLoadingLogs(true);
    Promise.resolve(onListLogFiles())
      .then((files) => {
        if (active) setLogFiles(Array.isArray(files) ? files : []);
      })
      .finally(() => {
        if (active) setLoadingLogs(false);
      });
    return () => {
      active = false;
    };
  }, [onListLogFiles]);

  const handleOpenLog = async (file) => {
    if (!onReadLogFile) return;
    const seq = ++logReadSeqRef.current;
    setSelectedLog(file);
    try {
      const text = await onReadLogFile(file.path);
      if (logReadSeqRef.current !== seq) return;
      setLogContent(text || '（日志文件为空）');
    } catch (err) {
      if (logReadSeqRef.current !== seq) return;
      setLogContent(`读取日志失败：${err?.message || String(err)}`);
    }
  };

  return (
    <section className="flex flex-col rounded-[24px] bg-surface-container-low p-6 lg:col-span-2">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-3">
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl bg-tertiary-container text-on-tertiary-container">
            <ScrollText className="h-5 w-5" aria-hidden="true" />
          </div>
          <div>
            <h2 className="text-base font-semibold text-on-surface">历史运行日志</h2>
            <p className="text-xs text-on-surface-variant">点击任意记录在线预览末尾输出</p>
          </div>
        </div>
        <div className="flex items-center gap-1.5">
          <Button variant="ghost" size="sm" onClick={refreshLogs} disabled={loadingLogs}>
            <RefreshCw className={`h-3.5 w-3.5 ${loadingLogs ? 'animate-spin' : ''}`} aria-hidden="true" />
            刷新
          </Button>
          {logsDir && onOpenPath && (
            <Button variant="tonal" size="sm" onClick={() => onOpenPath(logsDir)}>
              <FolderOpen className="h-3.5 w-3.5" aria-hidden="true" />
              打开日志文件夹
            </Button>
          )}
        </div>
      </div>

      <div className="mb-4 rounded-2xl bg-surface-container px-4 py-2">
        <Switch
          id="enableDebugLog"
          checked={Boolean(config.enableDebugLog)}
          onChange={(enableDebugLog) => {
            if (onToggleDebugLog) {
              onToggleDebugLog(enableDebugLog);
            } else {
              updateField({ enableDebugLog });
            }
          }}
          label="实时输出 DEBUG 调试日志"
          description="开启后运行监控页将实时显示底层 [DEBUG] 调试信息（支持任务运行中动态切换，默认关闭）"
        />
      </div>

      <div className="mb-4 grid grid-cols-2 gap-3 rounded-2xl bg-surface-container/60 p-4 lg:grid-cols-4">
        <div className="space-y-1.5">
          <Label htmlFor="logMaxSizeMb">日志上限（MB）</Label>
          <NumberInput
            id="logMaxSizeMb"
            min={1}
            integer
            value={config.logMaxSizeMb ?? 10}
            onValueChange={(logMaxSizeMb) => updateField({ logMaxSizeMb })}
            onValidityChange={(valid) => onNumberValidity('logMaxSizeMb', valid)}
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="logRetentionDays">保留天数（0 表示关闭）</Label>
          <NumberInput
            id="logRetentionDays"
            min={0}
            integer
            value={config.logRetentionDays ?? 14}
            onValueChange={(logRetentionDays) => updateField({ logRetentionDays })}
            onValidityChange={(valid) => onNumberValidity('logRetentionDays', valid)}
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="logMaxFiles">文件数上限</Label>
          <NumberInput
            id="logMaxFiles"
            min={1}
            integer
            value={config.logMaxFiles ?? 200}
            onValueChange={(logMaxFiles) => updateField({ logMaxFiles })}
            onValidityChange={(valid) => onNumberValidity('logMaxFiles', valid)}
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="logCompressAfterDays">压缩归档天数（0 表示关闭）</Label>
          <NumberInput
            id="logCompressAfterDays"
            min={0}
            integer
            value={config.logCompressAfterDays ?? 3}
            onValueChange={(logCompressAfterDays) => updateField({ logCompressAfterDays })}
            onValidityChange={(valid) => onNumberValidity('logCompressAfterDays', valid)}
          />
        </div>
      </div>

      {logFiles.length === 0 ? (
        <div className="flex flex-col items-center justify-center rounded-2xl bg-surface-container py-12 text-center">
          <ScrollText className="h-8 w-8 text-on-surface-variant/60" aria-hidden="true" />
          <p className="mt-3 text-sm font-semibold text-on-surface">暂无历史日志文件</p>
          <p className="mt-1 text-xs text-on-surface-variant">
            运行 Autovisor GUI 后，日志会自动保存在数据目录的 logs/ 下。
          </p>
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
          <div className="max-h-64 space-y-1 overflow-y-auto rounded-2xl bg-surface-container p-2 lg:col-span-4 lg:max-h-[24rem]">
            {logFiles.map((file) => {
              const active = selectedLog?.path === file.path;
              return (
                <button
                  key={file.path}
                  type="button"
                  onClick={() => handleOpenLog(file)}
                  className={`flex w-full items-center justify-between gap-2 rounded-xl px-3 py-2.5 text-left text-xs transition-colors ease-m3 ${
                    active
                      ? 'bg-secondary-container font-semibold text-on-secondary-container'
                      : 'text-on-surface hover:bg-on-surface/[0.06]'
                  }`}
                >
                  <span className="truncate font-mono">{file.name}</span>
                  <span className="shrink-0 opacity-75 tnum">
                    {file.sizeLabel} · {formatLogModifiedTime(file)}
                  </span>
                </button>
              );
            })}
          </div>

          <div className="flex min-h-0 flex-col lg:col-span-8">
            <div className="mb-2 flex items-center justify-between gap-2">
              <Label className="truncate text-xs">
                {selectedLog ? `预览末尾内容：${selectedLog.name}` : '点击左侧任意日志文件查看详情'}
              </Label>
              {selectedLog && onOpenPath && (
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => onOpenPath(selectedLog.path)}
                >
                  <ExternalLink className="h-3.5 w-3.5" aria-hidden="true" />
                  外部编辑器打开
                </Button>
              )}
            </div>
            <pre className="min-h-[16rem] max-h-[24rem] flex-1 overflow-auto rounded-2xl bg-[#12151C] p-4 font-mono text-xs leading-relaxed text-slate-200 whitespace-pre-wrap [overflow-wrap:anywhere]">
              {logContent || '选择日志文件以查看运行细节...'}
            </pre>
          </div>
        </div>
      )}
    </section>
  );
}
