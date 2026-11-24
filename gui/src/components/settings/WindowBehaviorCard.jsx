import { useState } from 'react';
import { AppWindow } from 'lucide-react';
import Label from '../ui/Label';
import Select from '../ui/Select';

export default function WindowBehaviorCard({ closeBehavior, onChangeCloseBehavior }) {
  const [savingCloseBehavior, setSavingCloseBehavior] = useState(false);
  const [closeBehaviorError, setCloseBehaviorError] = useState('');

  const handleChangeCloseBehavior = async (nextBehavior) => {
    if (!onChangeCloseBehavior || savingCloseBehavior) return;
    setSavingCloseBehavior(true);
    setCloseBehaviorError('');
    try {
      await onChangeCloseBehavior(nextBehavior);
    } catch (err) {
      setCloseBehaviorError(err?.message || String(err) || '无法保存关闭方式，请重试');
    } finally {
      setSavingCloseBehavior(false);
    }
  };

  return (
    <section className="flex flex-col rounded-[24px] bg-surface-container-low p-6 lg:col-span-2">
      <div className="mb-5 flex items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl bg-primary-container text-on-primary-container">
            <AppWindow className="h-5 w-5" aria-hidden="true" />
          </div>
          <div>
            <h2 className="text-base font-semibold text-on-surface">桌面窗口</h2>
            <p className="text-xs text-on-surface-variant">点击窗口右上角关闭按钮时的操作</p>
          </div>
        </div>
      </div>

      <div className="space-y-3">
        <div className="space-y-1.5">
          <Label htmlFor="close-behavior">关闭窗口时</Label>
          <Select
            id="close-behavior"
            value={closeBehavior}
            disabled={savingCloseBehavior}
            onChange={(e) => void handleChangeCloseBehavior(e.target.value)}
          >
            <option value="ask">每次询问</option>
            <option value="minimize">最小化</option>
            <option value="tray">最小化到托盘</option>
            <option value="exit">直接退出</option>
          </Select>
          <p className="text-xs leading-relaxed text-on-surface-variant">
            选择「每次询问」时会在关闭窗口前弹出确认框，并支持勾选记住选择；最小化到托盘后可单击系统托盘图标随时恢复窗口。
          </p>
          {closeBehaviorError && (
            <p role="alert" className="text-xs font-medium text-error">
              {closeBehaviorError}
            </p>
          )}
        </div>
      </div>
    </section>
  );
}
