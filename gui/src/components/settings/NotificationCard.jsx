import { useMemo, useState } from 'react';
import { Bell, Loader2, Send } from 'lucide-react';
import Button from '../ui/Button';
import Input from '../ui/Input';
import Label from '../ui/Label';
import Select from '../ui/Select';
import Switch from '../ui/Switch';
import { NOTIFICATION_PROVIDERS, isNotificationConfigured } from '../../lib/configModel';

export default function NotificationCard({
  config,
  isWindows,
  proxyValid,
  updateField,
  onTestNotification,
}) {
  const [testingNotify, setTestingNotify] = useState(false);
  const [notifyResult, setNotifyResult] = useState(null);

  const notifyProvider = config.notificationProvider || '';
  const notifyProviderMeta = useMemo(
    () => NOTIFICATION_PROVIDERS.find((item) => item.value === notifyProvider) || NOTIFICATION_PROVIDERS[0],
    [notifyProvider]
  );
  const notifyConfigured = useMemo(() => isNotificationConfigured(config), [config]);

  const handleTestNotification = async () => {
    if (!onTestNotification) return;
    setTestingNotify(true);
    setNotifyResult(null);
    try {
      const message = await onTestNotification({
        provider: config.notificationProvider || '',
        url: config.notificationUrl || '',
        tgChatId: config.notificationTgChatId || '',
        proxy: config.proxy || '',
      });
      setNotifyResult({ type: 'success', message: message || '测试推送发送成功' });
    } catch (err) {
      setNotifyResult({
        type: 'error',
        message: err?.message || String(err) || '测试推送发送失败',
      });
    } finally {
      setTestingNotify(false);
    }
  };

  return (
    <section className="flex flex-col rounded-[24px] bg-surface-container-low p-6">
      <div className="mb-5 flex items-center gap-3">
        <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl bg-secondary-container text-on-secondary-container">
          <Bell className="h-5 w-5" aria-hidden="true" />
        </div>
        <div>
          <h2 className="text-base font-semibold text-on-surface">消息推送通知</h2>
          <p className="text-xs text-on-surface-variant">
            支持 Windows 系统通知、Server酱、Qmsg酱、Bark 与 Telegram
          </p>
        </div>
      </div>

      <div className="space-y-4">
        <div className="space-y-1.5">
          <Label htmlFor="notification-provider">通知服务</Label>
          <Select
            id="notification-provider"
            value={notifyProvider}
            onChange={(e) => {
              updateField({ notificationProvider: e.target.value });
              setNotifyResult(null);
            }}
          >
            {NOTIFICATION_PROVIDERS.map((item) => (
              <option
                key={item.value || 'none'}
                value={item.value}
                disabled={!isWindows && item.value === 'Windows'}
              >
                {!isWindows && item.value === 'Windows'
                  ? `${item.label}（仅 Windows 可用）`
                  : item.label}
              </option>
            ))}
          </Select>
          {!isWindows && notifyProvider === 'Windows' && (
            <p role="alert" className="text-xs text-warning">
              Windows 系统通知仅 Windows 可用，请选择其他推送渠道或设为「不使用通知」
            </p>
          )}
        </div>

        {notifyProvider && notifyProvider !== 'Windows' && (
          <>
            <div className="space-y-1.5">
              <Label htmlFor="notification-url">通知 URL</Label>
              <Input
                id="notification-url"
                type="text"
                placeholder="https://..."
                value={config.notificationUrl || ''}
                onChange={(e) => {
                  updateField({ notificationUrl: e.target.value });
                  setNotifyResult(null);
                }}
                className="font-mono text-xs"
              />
              {notifyProviderMeta.hint && (
                <p className="break-all font-mono text-xs text-on-surface-variant">
                  {notifyProviderMeta.hint}
                </p>
              )}
            </div>

            {notifyProvider === 'Telegram' && (
              <div className="space-y-1.5">
                <Label htmlFor="tg-chat-id">Chat ID</Label>
                <Input
                  id="tg-chat-id"
                  type="text"
                  placeholder="123456789"
                  value={config.notificationTgChatId || ''}
                  onChange={(e) => {
                    updateField({ notificationTgChatId: e.target.value });
                    setNotifyResult(null);
                  }}
                  className="font-mono text-xs"
                />
              </div>
            )}
          </>
        )}

        <div className="space-y-0.5 border-t border-outline-variant/60 pt-3">
          <Switch
            id="notifyOnVerify"
            checked={config.notifyOnVerify !== false}
            onChange={(notifyOnVerify) => updateField({ notifyOnVerify })}
            label="遇安全验证 / 需人工登录时提醒"
            description="自动识别失败或登录态失效时立即发送提醒"
          />
          <Switch
            id="notifyOnError"
            checked={config.notifyOnError !== false}
            onChange={(notifyOnError) => updateField({ notifyOnError })}
            label="任务异常中断时提醒"
            description="课程进度未确认或发生未处理异常时通知"
          />
          <Switch
            id="notifyOnFinish"
            checked={config.notifyOnFinish !== false}
            onChange={(notifyOnFinish) => updateField({ notifyOnFinish })}
            label="学习完成 / 达到时限时提醒"
            description="全部课程完成或达到每课时长限制时发送汇总"
          />
        </div>

        <div className="flex flex-wrap items-center gap-3 pt-1">
          <Button
            type="button"
            variant="tonal"
            size="sm"
            onClick={handleTestNotification}
            disabled={
              testingNotify ||
              !notifyConfigured ||
              !proxyValid ||
              (!isWindows && notifyProvider === 'Windows')
            }
          >
            {testingNotify ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
            ) : (
              <Send className="h-3.5 w-3.5" aria-hidden="true" />
            )}
            {testingNotify ? '发送中...' : '发送测试推送'}
          </Button>
          {notifyResult && (
            <p
              role={notifyResult.type === 'error' ? 'alert' : 'status'}
              className={`text-xs font-medium ${
                notifyResult.type === 'error' ? 'text-error' : 'text-success'
              }`}
            >
              {notifyResult.message}
            </p>
          )}
        </div>
      </div>
    </section>
  );
}
