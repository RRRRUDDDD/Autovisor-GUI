import { Download, Loader2, QrCode, RefreshCw } from 'lucide-react';
import Button from '../ui/Button';

export default function QrLoginPanel({
  qrState,
  loading,
  importingCookies,
  activeBrowser,
  proxyValid,
  onStartQr,
  onSaveQrPng,
  onBrowserFallback,
}) {
  return (
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
                <Button type="button" size="sm" onClick={onStartQr}>
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
              onClick={onStartQr}
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
            onClick={onStartQr}
            disabled={qrState.loading || loading || !proxyValid}
          >
            <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
            刷新二维码
          </Button>
          <Button
            type="button"
            variant="tonal"
            size="sm"
            onClick={onSaveQrPng}
            disabled={!qrState.sessionId || qrState.saving || loading}
          >
            <Download className="h-3.5 w-3.5" aria-hidden="true" />
            {qrState.saving ? '保存中...' : '保存二维码 PNG'}
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={onBrowserFallback}
            disabled={loading || importingCookies || !activeBrowser || !proxyValid}
          >
            改用浏览器扫码（备用）
          </Button>
        </div>
      </div>
    </div>
  );
}
