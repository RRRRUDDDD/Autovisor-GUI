import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { Minimize2, PanelBottomClose, Power } from 'lucide-react';
import Button from './ui/Button';
import { tauriApi } from '../lib/tauriApi';

const CHOICES = [
  { action: 'minimize', label: '最小化', icon: Minimize2 },
  { action: 'tray', label: '最小化到托盘', icon: PanelBottomClose },
  { action: 'exit', label: '退出程序', icon: Power },
];

export function CloseChoiceDialog({ onRuntimeUpdated }) {
  const [promptId, setPromptId] = useState(null);
  const [remember, setRemember] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const dialogRef = useRef(null);
  const previousFocusRef = useRef(null);
  const titleId = useId();

  useEffect(() => {
    let disposed = false;
    let unlisten = null;
    tauriApi
      .listenCloseRequested(async (id) => {
        try {
          // 宿主若在超时内未收到前端确认，将自动退出以免窗口卡死。
          if (!disposed && (await tauriApi.closePromptShown(id)) && !disposed) {
            previousFocusRef.current =
              document.activeElement instanceof HTMLElement ? document.activeElement : null;
            setRemember(false);
            setError('');
            setPromptId(id);
          }
        } catch {
          // 宿主超时后自动退出。
        }
      })
      .then((stop) => {
        if (disposed) stop?.();
        else unlisten = stop;
      })
      .catch(() => {});
    return () => {
      disposed = true;
      unlisten?.();
    };
  }, []);

  useEffect(() => {
    if (promptId === null) {
      const target = previousFocusRef.current;
      previousFocusRef.current = null;
      if (target && target.isConnected) target.focus();
      return;
    }
    dialogRef.current?.querySelector('button')?.focus();
  }, [promptId]);

  const choose = useCallback(
    async (action) => {
      if (busy) return;
      setBusy(true);
      setError('');
      try {
        const updatedRuntime = await tauriApi.closeChoice(
          action,
          action === 'cancel' ? false : remember
        );
        if (updatedRuntime && onRuntimeUpdated) {
          onRuntimeUpdated(updatedRuntime);
        }
        setPromptId(null);
      } catch (reason) {
        setError(
          typeof reason === 'string'
            ? reason
            : reason?.message || '操作失败，请重试'
        );
        if (action === 'cancel') setPromptId(null);
      } finally {
        setBusy(false);
      }
    },
    [busy, remember, onRuntimeUpdated]
  );

  const onKeyDown = (event) => {
    if (event.key === 'Escape') {
      event.preventDefault();
      void choose('cancel');
      return;
    }
    if (event.key !== 'Tab' || !dialogRef.current) return;
    const focusable = [
      ...dialogRef.current.querySelectorAll(
        'button, input, a[href], select, textarea, [tabindex]:not([tabindex="-1"])'
      ),
    ].filter((node) => !node.disabled);
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (!dialogRef.current.contains(document.activeElement)) {
      event.preventDefault();
      first.focus();
    } else if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  if (promptId === null) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/45 px-4 backdrop-blur-[2px] animate-fade-in"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) void choose('cancel');
      }}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        onKeyDown={onKeyDown}
        className="w-full max-w-sm rounded-[28px] bg-surface-container-high p-6 text-on-surface shadow-elevation-3"
      >
        <h2 id={titleId} className="text-base font-semibold text-on-surface">
          关闭窗口
        </h2>
        <p className="mt-1 text-sm text-on-surface-variant">选择关闭窗口后的操作。</p>

        <div className="mt-5 space-y-2">
          {CHOICES.map(({ action, label, icon: Icon }) => (
            <div key={action}>
              <Button
                type="button"
                variant={action === 'minimize' ? 'default' : 'tonal'}
                disabled={busy}
                className="w-full justify-start"
                onClick={() => void choose(action)}
              >
                <Icon className="h-4 w-4" aria-hidden="true" />
                {label}
              </Button>
            </div>
          ))}
        </div>

        <label className="mt-4 flex cursor-pointer items-center gap-2 text-[13px] text-on-surface-variant">
          <input
            type="checkbox"
            className="h-4 w-4 accent-primary"
            checked={remember}
            disabled={busy}
            onChange={(event) => setRemember(event.target.checked)}
          />
          记住我的选择
        </label>

        {error && (
          <p role="alert" className="mt-3 text-xs font-medium text-error">
            {error}
          </p>
        )}

        <div className="mt-4 border-t border-outline-variant/60 pt-3">
          <Button
            type="button"
            variant="ghost"
            disabled={busy}
            className="w-full"
            onClick={() => void choose('cancel')}
            aria-label="取消关闭窗口"
          >
            取消，继续使用
          </Button>
        </div>
      </div>
    </div>
  );
}

export default CloseChoiceDialog;
