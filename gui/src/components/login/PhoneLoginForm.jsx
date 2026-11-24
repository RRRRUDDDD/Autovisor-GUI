import { Eye, EyeOff } from 'lucide-react';
import Input from '../ui/Input';
import Label from '../ui/Label';

export default function PhoneLoginForm({
  username,
  onChangeUsername,
  password,
  onChangePassword,
  showPassword,
  onToggleShowPassword,
  rememberCredentials,
  onChangeRemember,
  hasSavedPhone,
  loading,
  clearingSaved,
  onClearRemembered,
  passwordPlaceholder = '请输入密码',
}) {
  return (
    <>
      <div className="space-y-1.5">
        <Label htmlFor="login-phone">手机号</Label>
        <Input
          id="login-phone"
          type="text"
          inputMode="tel"
          autoComplete="username"
          placeholder="请输入智慧树绑定手机号"
          value={username}
          disabled={loading}
          onChange={(e) => onChangeUsername(e.target.value)}
        />
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="login-password">密码</Label>
        <div className="relative">
          <Input
            id="login-password"
            type={showPassword ? 'text' : 'password'}
            autoComplete="current-password"
            placeholder={passwordPlaceholder}
            value={password}
            disabled={loading}
            onChange={(e) => onChangePassword(e.target.value)}
            className="pr-11"
          />
          <button
            type="button"
            onClick={onToggleShowPassword}
            disabled={loading}
            aria-label={showPassword ? '隐藏密码' : '显示密码'}
            className="absolute inset-y-0 right-0 flex w-11 items-center justify-center rounded-r-xl text-on-surface-variant hover:text-on-surface"
          >
            {showPassword ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
          </button>
        </div>
      </div>
      <div className="flex flex-wrap items-center justify-between gap-2 pt-0.5">
        <label className="flex cursor-pointer items-center gap-2.5 text-xs text-on-surface-variant">
          <input
            id="login-remember-credentials"
            type="checkbox"
            checked={rememberCredentials}
            disabled={loading}
            onChange={(e) => onChangeRemember(e.target.checked)}
            className="h-4 w-4 rounded accent-primary"
          />
          <span>记住账号密码（下次自动填充，掉线时自动重登）</span>
        </label>
        {hasSavedPhone && (
          <button
            type="button"
            disabled={loading || clearingSaved}
            onClick={onClearRemembered}
            className="rounded-full px-2 py-0.5 text-[11px] font-medium text-on-surface-variant hover:bg-on-surface/[0.06] hover:text-error disabled:opacity-50"
          >
            清除已记住
          </button>
        )}
      </div>
    </>
  );
}
