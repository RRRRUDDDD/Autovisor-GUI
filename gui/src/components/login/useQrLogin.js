import { useCallback, useEffect, useRef, useState } from 'react';
import { tauriApi } from '../../lib/tauriApi';

const INITIAL_QR_STATE = {
  loading: false,
  sessionId: '',
  imageBase64: '',
  status: 'idle', // idle | waiting | scanned | confirmed | expired | cancelled | error
  error: '',
  savedPath: '',
  saving: false,
};

export function useQrLogin({
  mode,
  modeRef,
  mountedRef,
  restoringCookie,
  loading,
  effectiveProxy,
  browserExe,
  onClearError,
  onConfirmed,
}) {
  const [qrState, setQrState] = useState(INITIAL_QR_STATE);
  const qrSessionIdRef = useRef('');
  const qrPollTimerRef = useRef(null);
  const qrAttemptIdRef = useRef(0);

  const stopQrPolling = useCallback(() => {
    if (qrPollTimerRef.current) {
      clearTimeout(qrPollTimerRef.current);
      qrPollTimerRef.current = null;
    }
  }, []);

  const cancelActiveQrSession = useCallback(() => {
    qrAttemptIdRef.current += 1;
    stopQrPolling();
    const sid = qrSessionIdRef.current;
    qrSessionIdRef.current = '';
    if (sid) {
      void tauriApi.cancelQrLogin(sid).catch(() => {});
    }
  }, [stopQrPolling]);

  const resetQrState = useCallback(() => {
    setQrState((prev) => ({
      ...prev,
      loading: false,
      status: 'idle',
      sessionId: '',
      imageBase64: '',
      error: '',
    }));
  }, []);

  const startInAppQrLogin = useCallback(async () => {
    cancelActiveQrSession();
    const attempt = ++qrAttemptIdRef.current;
    onClearError?.();
    setQrState({
      ...INITIAL_QR_STATE,
      loading: true,
    });
    try {
      const res = await tauriApi.startQrLogin(effectiveProxy, '');
      if (
        !mountedRef.current ||
        qrAttemptIdRef.current !== attempt ||
        modeRef.current !== 'qrcode'
      ) {
        if (res?.sessionId) void tauriApi.cancelQrLogin(res.sessionId).catch(() => {});
        return;
      }
      const sid = res?.sessionId || '';
      const img = res?.imageBase64 || '';
      if (!sid || !img) {
        throw new Error('未获取到有效二维码图片');
      }
      qrSessionIdRef.current = sid;
      setQrState({
        loading: false,
        sessionId: sid,
        imageBase64: img,
        status: 'waiting',
        error: '',
        savedPath: res?.savedPath || '',
        saving: false,
      });

      const pollOnce = async () => {
        if (
          !mountedRef.current ||
          qrAttemptIdRef.current !== attempt ||
          qrSessionIdRef.current !== sid ||
          modeRef.current !== 'qrcode'
        ) {
          return;
        }
        try {
          const pollRes = await tauriApi.pollQrLogin(sid);
          if (
            !mountedRef.current ||
            qrAttemptIdRef.current !== attempt ||
            qrSessionIdRef.current !== sid ||
            modeRef.current !== 'qrcode'
          ) {
            return;
          }
          const st = pollRes?.status || 'waiting';
          if (st === 'confirmed') {
            qrSessionIdRef.current = '';
            stopQrPolling();
            setQrState((prev) => ({ ...prev, status: 'confirmed', error: '' }));
            await onConfirmed?.({
              mode: 'cookie',
              browserExe,
              proxy: effectiveProxy,
            });
            return;
          }
          if (st === 'expired' || st === 'cancelled') {
            qrSessionIdRef.current = '';
            stopQrPolling();
            setQrState((prev) => ({ ...prev, status: st }));
            return;
          }
          setQrState((prev) => ({ ...prev, status: st }));
          qrPollTimerRef.current = setTimeout(pollOnce, 1500);
        } catch (err) {
          if (
            !mountedRef.current ||
            qrAttemptIdRef.current !== attempt ||
            qrSessionIdRef.current !== sid ||
            modeRef.current !== 'qrcode'
          ) {
            return;
          }
          qrSessionIdRef.current = '';
          stopQrPolling();
          setQrState((prev) => ({
            ...prev,
            status: 'error',
            error: err?.message || String(err) || '轮询二维码状态失败',
          }));
        }
      };

      qrPollTimerRef.current = setTimeout(pollOnce, 1500);
    } catch (err) {
      if (
        !mountedRef.current ||
        qrAttemptIdRef.current !== attempt ||
        modeRef.current !== 'qrcode'
      ) {
        return;
      }
      setQrState({
        ...INITIAL_QR_STATE,
        status: 'error',
        error: err?.message || String(err) || '获取二维码失败，请检查网络或改用浏览器扫码',
      });
    }
  }, [
    cancelActiveQrSession,
    effectiveProxy,
    stopQrPolling,
    onConfirmed,
    onClearError,
    browserExe,
    mountedRef,
    modeRef,
  ]);

  useEffect(() => {
    if (mode !== 'qrcode' || restoringCookie || loading) {
      if (mode !== 'qrcode') {
        cancelActiveQrSession();
      }
      return;
    }
    if (!qrSessionIdRef.current && !qrState.loading && qrState.status === 'idle') {
      void startInAppQrLogin();
    }
  }, [
    mode,
    restoringCookie,
    loading,
    qrState.loading,
    qrState.status,
    cancelActiveQrSession,
    startInAppQrLogin,
  ]);

  const handleSaveQrPng = useCallback(async () => {
    if (!qrState.sessionId || qrState.saving) return;
    setQrState((prev) => ({ ...prev, saving: true, error: '' }));
    try {
      const dir = await tauriApi.pickDirectory();
      if (!dir) {
        if (mountedRef.current) setQrState((prev) => ({ ...prev, saving: false }));
        return;
      }
      const res = await tauriApi.saveQrImage(qrState.sessionId, dir);
      if (mountedRef.current) {
        setQrState((prev) => ({
          ...prev,
          saving: false,
          savedPath: res?.savedPath || dir,
        }));
      }
    } catch (err) {
      if (mountedRef.current) {
        setQrState((prev) => ({
          ...prev,
          saving: false,
          error: err?.message || String(err) || '保存二维码图片失败',
        }));
      }
    }
  }, [qrState.sessionId, qrState.saving, mountedRef]);

  return {
    qrState,
    startInAppQrLogin,
    cancelActiveQrSession,
    resetQrState,
    handleSaveQrPng,
  };
}
