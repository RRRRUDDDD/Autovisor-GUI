import { CheckCircle2, Globe, RefreshCw, Stethoscope } from 'lucide-react';
import Button from '../ui/Button';
import Input from '../ui/Input';
import Label from '../ui/Label';
import Select from '../ui/Select';
import Switch from '../ui/Switch';

export default function BrowserNetworkCard({
  config,
  detectedBrowsers,
  proxyValid,
  taskRunning,
  updateField,
  onSelectBrowser,
  onRedetectBrowsers,
  onPickBrowserExe,
  onCheckBrowser,
}) {
  return (
    <section className="flex flex-col rounded-[24px] bg-surface-container-low p-6">
      <div className="mb-5 flex items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl bg-primary-container text-on-primary-container">
            <Globe className="h-5 w-5" aria-hidden="true" />
          </div>
          <div>
            <h2 className="text-base font-semibold text-on-surface">浏览器与网络</h2>
            <p className="text-xs text-on-surface-variant">自动识别本机浏览器、代理与 CDP 调试</p>
          </div>
        </div>
        <Button variant="tonal" size="sm" onClick={onRedetectBrowsers}>
          <RefreshCw className="h-3.5 w-3.5" />
          重新扫描
        </Button>
      </div>

      <div className="space-y-4">
        <div className="space-y-2">
          <Label>本机已识别浏览器</Label>
          {detectedBrowsers.length > 0 ? (
            <div className="space-y-2">
              {detectedBrowsers.map((browser) => {
                const selected =
                  config.exePath &&
                  config.exePath.toLowerCase() === browser.exePath.toLowerCase();
                return (
                  <button
                    key={browser.exePath}
                    type="button"
                    onClick={() => onSelectBrowser?.(browser)}
                    className={`flex w-full items-start justify-between gap-3 rounded-2xl px-4 py-3 text-left transition-all duration-150 ease-m3 ${
                      selected
                        ? 'bg-secondary-container text-on-secondary-container'
                        : 'bg-surface-container hover:bg-surface-container-high text-on-surface'
                    }`}
                  >
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="text-sm font-medium">{browser.name}</span>
                        {browser.recommended && (
                          <span className="rounded-full bg-primary-container px-2 py-0.5 text-[11px] font-medium text-on-primary-container">
                            推荐
                          </span>
                        )}
                      </div>
                      <p
                        className="mt-1 truncate font-mono text-[11px] opacity-75"
                        title={browser.exePath}
                      >
                        {browser.exePath}
                      </p>
                    </div>
                    {selected && (
                      <CheckCircle2 className="mt-0.5 h-4.5 w-4.5 shrink-0 text-primary" />
                    )}
                  </button>
                );
              })}
            </div>
          ) : (
            <div className="rounded-2xl bg-warning-container px-4 py-3 text-xs text-on-warning-container">
              未自动检测到 Edge / Chrome，请点击下方手动指定浏览器可执行文件路径。
            </div>
          )}
        </div>

        <div className="space-y-1.5">
          <Label htmlFor="proxy">代理服务器（可选）</Label>
          <Input
            id="proxy"
            value={config.proxy || ''}
            onChange={(e) => updateField({ proxy: e.target.value })}
            placeholder="如 http://127.0.0.1:7890 或 socks5://127.0.0.1:1080"
            className="font-mono text-xs"
          />
          {!proxyValid ? (
            <p role="alert" className="text-xs text-error">
              代理格式无效，请以 http://、https:// 或 socks5:// 开头
            </p>
          ) : (
            <p className="text-xs text-on-surface-variant">
              留空则直连；同时作用于账号登录、拉取课程、浏览器刷课、更新检查与推送通知。
            </p>
          )}
        </div>

        <div className="space-y-1.5">
          <Label htmlFor="browserDriver">浏览器内核 / 驱动（driver）</Label>
          <Select
            id="browserDriver"
            value={config.driver || 'Edge'}
            onChange={(e) => {
              const nextDriver = e.target.value;
              const patch = { driver: nextDriver };
              if (nextDriver === 'Chromium') {
                const matched = detectedBrowsers.find(
                  (b) => (b.driver || '').toLowerCase() === 'chromium'
                );
                patch.exePath = matched ? matched.exePath : '';
              } else {
                const curExe = (config.exePath || '').trim();
                const curLower = curExe.toLowerCase();
                const curMatched = detectedBrowsers.find(
                  (b) => (b.exePath || '').toLowerCase() === curLower
                );
                const curInferred = curMatched
                  ? curMatched.driver
                  : curLower.includes('msedge')
                    ? 'Edge'
                    : curLower.includes('chromium') || curLower.includes('ms-playwright')
                      ? 'Chromium'
                      : curExe
                        ? 'Chrome'
                        : '';
                if (!curExe || (curInferred || '').toLowerCase() !== nextDriver.toLowerCase()) {
                  const matched = detectedBrowsers.find(
                    (b) => (b.driver || '').toLowerCase() === nextDriver.toLowerCase()
                  );
                  patch.exePath = matched ? matched.exePath : '';
                }
              }
              updateField(patch);
            }}
          >
            <option value="Edge">Microsoft Edge</option>
            <option value="Chrome">Google Chrome</option>
            <option value="Chromium">Playwright 内置 Chromium</option>
          </Select>
          {config.driver === 'Chromium' && (
            <p className="text-xs text-on-surface-variant">
              使用 Playwright 内置 Chromium 时将忽略下方手动可执行路径
            </p>
          )}
        </div>

        <div className="space-y-1.5">
          <Label htmlFor="exePath">手动覆盖路径（一般无需填写）</Label>
          <div className="flex gap-2">
            <Input
              id="exePath"
              value={config.exePath}
              onChange={(e) => {
                const nextPath = e.target.value;
                const patch = { exePath: nextPath };
                if (config.driver !== 'Chromium') {
                  const lower = nextPath.toLowerCase();
                  if (lower.includes('msedge')) {
                    patch.driver = 'Edge';
                  } else if (lower.includes('chrome')) {
                    patch.driver = 'Chrome';
                  }
                }
                updateField(patch);
              }}
              placeholder="已由设备自动识别填充"
              className="font-mono text-xs"
            />
            <Button variant="tonal" className="h-11 shrink-0" onClick={onPickBrowserExe}>
              浏览
            </Button>
          </div>
        </div>

        <div className="border-t border-outline-variant/60 pt-3">
          <Switch
            id="attachExistingChrome"
            checked={config.attachExistingChrome}
            onChange={(attachExistingChrome) => updateField({ attachExistingChrome })}
            label="连接已打开的 Chrome（CDP 调试）"
            description="进阶选项：直接复用已开启远程调试端口的本地 Chrome"
          />
          {config.attachExistingChrome && (
            <div className="mt-3 space-y-1.5 animate-fade-in">
              <Label htmlFor="cdpUrl">CDP 端点地址</Label>
              <Input
                id="cdpUrl"
                value={config.cdpUrl}
                onChange={(e) => updateField({ cdpUrl: e.target.value })}
                placeholder="http://127.0.0.1:9222"
                className="font-mono text-xs"
              />
            </div>
          )}
        </div>

        {onCheckBrowser && (
          <div className="flex flex-wrap items-center gap-3 border-t border-outline-variant/60 pt-3">
            <Button
              type="button"
              variant="tonal"
              size="sm"
              onClick={onCheckBrowser}
              disabled={
                taskRunning ||
                !proxyValid ||
                (config.driver !== 'Chromium' &&
                  !config.exePath?.trim() &&
                  detectedBrowsers.length === 0)
              }
            >
              <Stethoscope className="h-3.5 w-3.5" aria-hidden="true" />
              检测浏览器与登录状态
            </Button>
            <span className="text-xs text-on-surface-variant">
              只读验证浏览器启动、隐身配置与当前 Cookie 登录态
            </span>
          </div>
        )}
      </div>
    </section>
  );
}
