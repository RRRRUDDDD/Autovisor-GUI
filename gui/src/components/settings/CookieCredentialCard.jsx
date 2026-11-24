import { useState } from 'react';
import { CheckCircle2, Eye, EyeOff, KeyRound, Trash2 } from 'lucide-react';
import Button from '../ui/Button';
import Input from '../ui/Input';
import Label from '../ui/Label';

export default function CookieCredentialCard({
  config,
  runtime,
  taskRunning,
  updateField,
  onImportCookies,
  onClearCookies,
}) {
  const [editingCredentials, setEditingCredentials] = useState(false);
  const [showSavedPassword, setShowSavedPassword] = useState(false);

  return (
    <section className="rounded-[24px] bg-surface-container-low p-6 lg:col-span-2">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div className="flex min-w-0 items-center gap-3">
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl bg-primary-container text-on-primary-container">
            <KeyRound className="h-5 w-5" aria-hidden="true" />
          </div>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="text-base font-semibold text-on-surface">智慧树登录 Cookie 管理</h2>
              <span
                className={`inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-medium ${
                  runtime?.cookieExists
                    ? 'bg-success-container text-on-success-container'
                    : 'bg-surface-container-highest text-on-surface-variant'
                }`}
              >
                {runtime?.cookieExists && <CheckCircle2 className="h-3 w-3" aria-hidden="true" />}
                {runtime?.cookieExists ? '已保存' : '未缓存'}
              </span>
            </div>
            <p className="mt-0.5 text-xs text-on-surface-variant">
              {runtime?.cookieExists
                ? `当前本地已缓存 ${runtime.cookieCount} 条有效凭证（data/cookies.json）`
                : '本地暂无缓存的 Cookie，首次登录完成后会自动保存'}
            </p>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <Button
            size="sm"
            variant="tonal"
            onClick={onImportCookies}
            disabled={taskRunning}
          >
            从 JSON 文件导入 Cookie
          </Button>
          <Button
            size="sm"
            variant="destructive-tonal"
            onClick={onClearCookies}
            disabled={!runtime?.cookieExists || taskRunning}
          >
            <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
            清除本地 Cookie
          </Button>
        </div>
      </div>

      <div className="mt-5 border-t border-outline-variant/60 pt-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="text-sm font-semibold text-on-surface">自动重登凭证（可选）</h3>
              <span
                className={`rounded-full px-2.5 py-0.5 text-xs font-medium ${
                  config.username && config.password
                    ? 'bg-primary-container text-on-primary-container'
                    : 'bg-surface-container-highest text-on-surface-variant'
                }`}
              >
                {config.username && config.password
                  ? `已保存账号 ${
                      config.username.length >= 7
                        ? `${config.username.slice(0, 3)}****${config.username.slice(-4)}`
                        : `${config.username.slice(0, 1)}***`
                    }`
                  : config.username
                    ? '仅保存账号（未存密码）'
                    : '未保存'}
              </span>
            </div>
            <p className="mt-0.5 text-xs text-on-surface-variant">
              默认不保存密码；仅用于长时挂机中途 Cookie 失效时自动重登，明文保存在本机 config.ini，随时可清除
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button
              type="button"
              size="sm"
              variant="tonal"
              onClick={() => setEditingCredentials((v) => !v)}
              disabled={taskRunning}
            >
              {editingCredentials ? '收起凭证编辑' : '编辑自动重登账密'}
            </Button>
            <Button
              type="button"
              size="sm"
              variant="destructive-tonal"
              onClick={() => {
                updateField({ username: '', password: '' });
                setEditingCredentials(false);
              }}
              disabled={(!config.username && !config.password) || taskRunning}
            >
              <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
              清除已保存账密
            </Button>
          </div>
        </div>

        {editingCredentials && (
          <div className="mt-3 grid grid-cols-1 gap-3 rounded-2xl bg-surface-container p-4 sm:grid-cols-2 animate-fade-in">
            <div className="space-y-1.5">
              <Label htmlFor="saved-username">手机号 / 账号</Label>
              <Input
                id="saved-username"
                value={config.username || ''}
                onChange={(e) => updateField({ username: e.target.value })}
                placeholder="用于 Cookie 失效时自动重登的手机号"
                className="font-mono text-xs"
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="saved-password">登录密码</Label>
              <div className="relative">
                <Input
                  id="saved-password"
                  type={showSavedPassword ? 'text' : 'password'}
                  value={config.password || ''}
                  onChange={(e) => updateField({ password: e.target.value })}
                  placeholder="留空则不自动填写密码"
                  className="pr-11 font-mono text-xs"
                />
                <button
                  type="button"
                  onClick={() => setShowSavedPassword((s) => !s)}
                  aria-label={showSavedPassword ? '隐藏已保存密码' : '显示已保存密码'}
                  className="absolute inset-y-0 right-0 flex w-11 items-center justify-center rounded-r-xl text-on-surface-variant hover:text-on-surface"
                >
                  {showSavedPassword ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </section>
  );
}
