import { memo, useEffect, useMemo, useRef, useState } from 'react';
import { ScrollText, FolderOpen, Copy, Search, ArrowDownToLine } from 'lucide-react';
import Button from './ui/Button';
import { LOG_LIMIT, filterLogs, formatLogsForCopy, logLevel, logTime } from '../lib/logPresentation';

const levelColors = {
  error: 'text-rose-300',
  warning: 'text-amber-300',
  success: 'text-emerald-300',
  info: 'text-slate-200',
  debug: 'text-slate-400',
};
const copyNotice = '已遮盖常见敏感字段（API Key、手机号、密码），分享前仍需自行检查';

const ExecutionLog = memo(function ExecutionLog({
  logs = [],
  truncated = false,
  logFilePath = '',
  onOpenLogFile,
  debugEnabled = false,
  onToggleDebug,
}) {
  const [level, setLevel] = useState('all');
  const [query, setQuery] = useState('');
  const [following, setFollowing] = useState(true);
  const [copyStatus, setCopyStatus] = useState('');
  const [manualText, setManualText] = useState(null);
  const [copying, setCopying] = useState(false);
  const containerRef = useRef(null);
  const manuallyPaused = useRef(false);
  const mounted = useRef(true);

  const filtered = useMemo(() => filterLogs(logs, level, query), [logs, level, query]);
  const displayLogs = useMemo(
    () =>
      filtered.map((entry) => {
        const normalizedLevel = logLevel(entry.level);
        return {
          seq: entry.seq,
          time: logTime(entry.timestamp),
          level: normalizedLevel,
          color: levelColors[normalizedLevel] || 'text-slate-300',
          message: entry.message,
        };
      }),
    [filtered]
  );

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    const container = containerRef.current;
    if (container && following) {
      container.scrollTop = container.scrollHeight;
    }
  }, [filtered, following]);

  const copy = async () => {
    if (copying) return;
    const text = formatLogsForCopy(filtered);
    setCopying(true);
    setManualText(null);
    try {
      if (!navigator.clipboard?.writeText) throw new Error('unavailable');
      await navigator.clipboard.writeText(text);
      if (mounted.current) setCopyStatus(`已复制当前匹配日志。${copyNotice}`);
    } catch (error) {
      if (mounted.current) {
        setManualText(text);
        setCopyStatus(
          `${error?.message === 'unavailable' ? '当前环境不支持剪贴板' : '剪贴板写入被拒绝或失败'}，请手动复制下方文本。${copyNotice}`
        );
      }
    } finally {
      if (mounted.current) setCopying(false);
    }
  };

  return (
    <section
      aria-label="日志工具"
      className="min-w-0 overflow-hidden rounded-[24px] bg-surface-container-low"
    >
      <div className="flex flex-wrap items-center justify-between gap-2 px-5 pt-5 pb-3">
        <h2 className="flex items-center gap-2 text-base font-semibold text-on-surface">
          <ScrollText className="h-4.5 w-4.5 text-primary" aria-hidden="true" />
          执行日志
        </h2>
        <div className="flex items-center gap-2">
          <span className="rounded-full bg-surface-container-high px-2.5 py-0.5 text-xs text-on-surface-variant tnum">
            保留最近 {LOG_LIMIT} 条
          </span>
          {logFilePath && onOpenLogFile && (
            <Button variant="tonal" size="sm" onClick={() => onOpenLogFile(logFilePath)}>
              <FolderOpen className="h-3.5 w-3.5" aria-hidden="true" />
              打开日志文件
            </Button>
          )}
        </div>
      </div>

      <div className="space-y-3 px-5 pb-5">
        <div className="flex flex-wrap items-center gap-2.5">
          <label className="flex items-center gap-2 text-xs text-on-surface-variant">
            级别
            <select
              aria-label="日志级别"
              value={level}
              onChange={(event) => setLevel(event.target.value)}
              className="h-9 rounded-xl border border-outline/60 bg-surface-container-lowest px-3 py-1 text-xs leading-normal text-on-surface focus:border-primary focus:outline-none focus:ring-2 focus:ring-primary/25"
            >
              <option value="all">全部</option>
              <option value="info">常规（隐藏调试）</option>
              <option value="warnings">错误与警告</option>
              <option value="errors">仅错误</option>
            </select>
          </label>
          <div className="relative min-w-0 flex-1 basis-44">
            <Search
              className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-on-surface-variant"
              aria-hidden="true"
            />
            <input
              type="search"
              aria-label="搜索日志"
              placeholder="搜索日志关键词"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              className="h-9 w-full rounded-full bg-surface-container-highest pl-8 pr-3.5 text-xs text-on-surface placeholder:text-on-surface-variant/80 focus:outline-none focus:ring-2 focus:ring-primary"
            />
          </div>
          <Button variant="tonal" size="sm" onClick={copy} disabled={!filtered.length || copying}>
            <Copy className="h-3.5 w-3.5" aria-hidden="true" />
            {copying ? '复制中' : '复制当前结果'}
          </Button>
        </div>

        <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-xs text-on-surface-variant">
          <span role="status" className="tnum">
            匹配 {filtered.length} / {logs.length} 条
          </span>
          <label className="flex cursor-pointer items-center gap-2">
            <input
              type="checkbox"
              checked={following}
              className="h-4 w-4 rounded accent-primary"
              onChange={(event) => {
                manuallyPaused.current = !event.target.checked;
                setFollowing(event.target.checked);
              }}
            />
            自动跟随
          </label>
          {onToggleDebug && (
            <label className="flex cursor-pointer items-center gap-2">
              <input
                type="checkbox"
                checked={Boolean(debugEnabled)}
                className="h-4 w-4 rounded accent-primary"
                onChange={(event) => onToggleDebug(event.target.checked)}
              />
              调试日志
            </label>
          )}
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              manuallyPaused.current = false;
              setFollowing(true);
              if (containerRef.current) {
                containerRef.current.scrollTop = containerRef.current.scrollHeight;
              }
            }}
          >
            <ArrowDownToLine className="h-3.5 w-3.5" />
            回到最新
          </Button>
          {!following && (
            <span className="rounded-full bg-warning-container px-2.5 py-0.5 text-[11px] font-medium text-on-warning-container">
              跟随已暂停
            </span>
          )}
        </div>

        {truncated && (
          <p className="text-xs text-on-surface-variant">
            较早的日志已省略，完整输出已保存在 Autovisor GUI 日志文件中。
          </p>
        )}

        <div
          ref={containerRef}
          role="log"
          aria-label="执行日志"
          aria-live="off"
          tabIndex={0}
          onScroll={(event) => {
            const container = event.currentTarget;
            if (!manuallyPaused.current) {
              setFollowing(
                container.scrollHeight - container.scrollTop - container.clientHeight <= 24
              );
            }
          }}
          className="min-h-[12rem] max-h-[clamp(18rem,48vh,38rem)] overflow-y-auto overscroll-contain rounded-2xl bg-[#12151C] p-4 font-mono text-xs leading-relaxed text-slate-200 [overflow-wrap:anywhere] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
        >
          {!displayLogs.length ? (
            <p className="text-slate-400">
              {logs.length ? '没有匹配的日志，请调整级别或关键词。' : '等待 Autovisor GUI 输出日志...'}
            </p>
          ) : (
            displayLogs.map((entry) => (
              <div key={entry.seq} className="mb-0.5 whitespace-pre-wrap">
                <span className="mr-2 text-slate-500 tnum">[{entry.time}]</span>
                <span className="mr-2 text-slate-400 uppercase">[{entry.level}]</span>
                <span className={entry.color}>{entry.message}</span>
              </div>
            ))
          )}
        </div>

        {copyStatus && (
          <p role="status" className="text-xs text-on-surface-variant">
            {copyStatus}
          </p>
        )}
        {manualText !== null && (
          <div className="space-y-2">
            <textarea
              aria-label="手动复制日志"
              readOnly
              value={manualText}
              rows={6}
              onFocus={(event) => event.target.select()}
              className="w-full rounded-2xl border border-outline/60 bg-surface-container-lowest p-3.5 font-mono text-xs text-on-surface focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
            />
            <Button size="sm" variant="ghost" onClick={() => setManualText(null)}>
              关闭手动复制
            </Button>
          </div>
        )}
      </div>
    </section>
  );
});

export default ExecutionLog;
