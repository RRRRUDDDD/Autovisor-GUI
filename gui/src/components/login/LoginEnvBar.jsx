import { Globe, Network, RefreshCw } from 'lucide-react';
import Input from '../ui/Input';

export default function LoginEnvBar({
  detectedBrowsers,
  activeBrowser,
  loading,
  importingCookies,
  proxyInput,
  proxyValid,
  showProxyInput,
  configProxy,
  onSelectBrowser,
  onRedetectBrowsers,
  onPickBrowserExe,
  onToggleProxyInput,
  onChangeProxy,
  onSaveProxy,
}) {
  return (
    <>
      <div className="flex items-center justify-between gap-2 rounded-2xl bg-surface-container px-4 py-2.5 text-xs">
        <div className="flex min-w-0 items-center gap-2">
          <Globe
            className={`h-4 w-4 shrink-0 ${activeBrowser ? 'text-primary' : 'text-warning'}`}
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
            onClick={onToggleProxyInput}
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
              onChange={(e) => onChangeProxy(e.target.value)}
              onBlur={() => {
                if (proxyValid && proxyInput.trim() !== (configProxy || '').trim()) {
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
    </>
  );
}
