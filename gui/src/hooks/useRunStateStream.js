import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { tauriApi } from '../lib/tauriApi';
import { createRunStateStore } from '../lib/runStateStore';

export const isActiveRunStatus = (status) => status === 'running' || status === 'stopping';
const selectTaskRunning = (s) => isActiveRunStatus(s?.status);
const selectHasRunHistory = (s) => Boolean(s && s.status !== 'idle');
const selectShutdownScheduled = (s) => s?.shutdown?.state === 'scheduled';
const selectShutdownInfo = (s) => s?.shutdown ?? null;

export function useRunStateStream({
  setRuntime,
  setAutoRestoreCookie,
  setStopping,
  setCancelShutdownError,
}) {
  const [cookieImportState, setCookieImportState] = useState({
    importing: false,
    error: '',
    nonce: 0,
  });

  const runStateStoreRef = useRef(null);
  if (!runStateStoreRef.current) {
    runStateStoreRef.current = createRunStateStore(null);
  }
  const runStateStore = runStateStoreRef.current;

  const taskRunning = useSyncExternalStore(runStateStore.subscribe, () =>
    selectTaskRunning(runStateStore.getSnapshot())
  );
  const hasRunHistory = useSyncExternalStore(runStateStore.subscribe, () =>
    selectHasRunHistory(runStateStore.getSnapshot())
  );
  const shutdownScheduled = useSyncExternalStore(runStateStore.subscribe, () =>
    selectShutdownScheduled(runStateStore.getSnapshot())
  );
  const shutdownInfo = useSyncExternalStore(runStateStore.subscribe, () =>
    selectShutdownInfo(runStateStore.getSnapshot())
  );

  const prevRunStatusRef = useRef('idle');
  const cookieImportAttemptRef = useRef(0);
  const cookieImportHandledKeyRef = useRef('');
  const cookieImportInFlightKeyRef = useRef('');

  const getCookieImportResultKey = useCallback((nextState) => {
    if (!nextState || nextState.mode !== 'import_cookies' || isActiveRunStatus(nextState.status)) {
      return '';
    }
    return nextState.startedAt || nextState.finishedAt
      ? `${nextState.startedAt ?? ''}:${nextState.finishedAt ?? ''}:${nextState.status}`
      : `attempt:${cookieImportAttemptRef.current}:${nextState.status}`;
  }, []);

  const finalizeCookieImport = useCallback(
    (nextState) => {
      const key = getCookieImportResultKey(nextState);
      if (!key || cookieImportHandledKeyRef.current === key) {
        return false;
      }
      cookieImportHandledKeyRef.current = key;
      if (nextState.status === 'completed') {
        setAutoRestoreCookie(true);
        setCookieImportState((prev) => ({
          importing: false,
          error: '',
          nonce: prev.nonce + 1,
        }));
      } else if (nextState.status === 'error') {
        setCookieImportState((prev) => ({
          ...prev,
          importing: false,
          error: nextState.errorMessage || '导入 Cookie 失败，请检查文件格式',
        }));
      } else {
        setCookieImportState((prev) => ({ ...prev, importing: false }));
      }
      return true;
    },
    [getCookieImportResultKey, setAutoRestoreCookie]
  );

  useEffect(() => {
    let unlistenState = () => {};
    let unlistenBackend = () => {};
    let active = true;
    tauriApi
      .listenRunState((payload) => {
        if (!active) return;
        const prevStatus = prevRunStatusRef.current;
        const nextState = runStateStore.applyPayload(payload);
        const nextStatus = nextState?.status || 'idle';
        prevRunStatusRef.current = nextStatus;

        if (nextState?.shutdown?.state !== 'scheduled' && !nextState?.shutdownOnComplete) {
          setCancelShutdownError('');
        }
        if (!isActiveRunStatus(nextStatus)) {
          setStopping(false);
        }
        const transitionedToTerminal =
          isActiveRunStatus(prevStatus) && !isActiveRunStatus(nextStatus);
        const cookieResultKey = getCookieImportResultKey(nextState);
        const isPendingCookieImportResult = Boolean(
          cookieResultKey &&
            cookieImportHandledKeyRef.current !== cookieResultKey &&
            cookieImportInFlightKeyRef.current !== cookieResultKey
        );

        if (transitionedToTerminal || isPendingCookieImportResult) {
          if (isPendingCookieImportResult) {
            cookieImportInFlightKeyRef.current = cookieResultKey;
          }
          tauriApi
            .getRuntimeInfo()
            .then((rt) => {
              if (!active) return;
              if (rt) setRuntime(rt);
              if (isPendingCookieImportResult) {
                finalizeCookieImport(nextState);
              }
            })
            .catch(() => {
              if (active && isPendingCookieImportResult) {
                finalizeCookieImport(nextState);
              }
            })
            .finally(() => {
              if (cookieImportInFlightKeyRef.current === cookieResultKey) {
                cookieImportInFlightKeyRef.current = '';
              }
            });
        }
      })
      .then((fn) => {
        if (active) unlistenState = fn;
        else fn();
      });
    tauriApi
      .listenBackendStatus((status) => {
        if (!active || !status) return;
        setRuntime((prev) =>
          prev
            ? {
                ...prev,
                backendPhase: status.phase || prev.backendPhase,
                backendError: status.error || '',
                version: status.version || prev.version,
                backendVersion: status.backendVersion || prev.backendVersion,
              }
            : prev
        );
      })
      .then((fn) => {
        if (active) unlistenBackend = fn;
        else fn();
      });
    return () => {
      active = false;
      unlistenState();
      unlistenBackend();
    };
  }, [
    runStateStore,
    getCookieImportResultKey,
    finalizeCookieImport,
    setRuntime,
    setStopping,
    setCancelShutdownError,
  ]);

  return {
    runStateStore,
    taskRunning,
    hasRunHistory,
    shutdownScheduled,
    shutdownInfo,
    prevRunStatusRef,
    cookieImportState,
    setCookieImportState,
    cookieImportAttemptRef,
    finalizeCookieImport,
  };
}
