import { useCallback, useEffect, useRef, useState } from 'react';
import { tauriApi } from '../lib/tauriApi';
import { isValidProxyUrl } from '../lib/configModel';

export function useUpdateChecker({ loading, backendPhase, latestConfigRef, mountedRef }) {
  const [updateInfo, setUpdateInfo] = useState(null);
  const [checkingUpdate, setCheckingUpdate] = useState(false);
  const [updateError, setUpdateError] = useState('');
  const [updateDismissed, setUpdateDismissed] = useState(false);
  const autoUpdateCheckedRef = useRef(false);

  const handleCheckUpdate = useCallback(
    async ({ silent = false } = {}) => {
      setCheckingUpdate(true);
      if (!silent) {
        setUpdateError('');
      }
      try {
        const proxy = isValidProxyUrl(latestConfigRef.current?.proxy)
          ? latestConfigRef.current.proxy || ''
          : '';
        const info = await tauriApi.checkForUpdate(proxy);
        if (!mountedRef.current) return info;
        if (info && typeof info === 'object') {
          setUpdateInfo(info);
          if (!silent) {
            setUpdateError('');
            if (info.hasUpdate) {
              setUpdateDismissed(false);
            }
          }
        }
        return info;
      } catch (err) {
        if (!mountedRef.current) return null;
        if (!silent) {
          setUpdateError(err?.message || String(err) || '检查更新失败，请稍后重试');
        }
        return null;
      } finally {
        if (mountedRef.current) {
          setCheckingUpdate(false);
        }
      }
    },
    [latestConfigRef, mountedRef]
  );

  useEffect(() => {
    if (loading || backendPhase !== 'ready' || autoUpdateCheckedRef.current) {
      return;
    }
    autoUpdateCheckedRef.current = true;
    void handleCheckUpdate({ silent: true });
  }, [loading, backendPhase, handleCheckUpdate]);

  const handleManualCheckUpdate = useCallback(
    () => handleCheckUpdate({ silent: false }),
    [handleCheckUpdate]
  );

  return {
    updateInfo,
    checkingUpdate,
    updateError,
    updateDismissed,
    setUpdateDismissed,
    handleManualCheckUpdate,
  };
}
