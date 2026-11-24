import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { tauriApi } from '../lib/tauriApi';
import {
  configSnapshot,
  defaultConfig,
  isValidLlmApiBase,
  isValidProxyUrl,
  normalizeConfig,
} from '../lib/configModel';

const AUTO_SAVE_DELAY_MS = 250;

export function useAutoSaveConfig({ mountedRef }) {
  const [config, setConfig] = useState(defaultConfig);
  const [savedSnapshot, setSavedSnapshot] = useState(null);
  const [saving, setSaving] = useState(false);
  const [saveStatus, setSaveStatus] = useState(null);
  const [settingsFieldsValid, setSettingsFieldsValid] = useState(true);

  const latestConfigRef = useRef(config);
  latestConfigRef.current = config;
  const savedSnapshotRef = useRef(savedSnapshot);
  savedSnapshotRef.current = savedSnapshot;
  const autoSaveTimerRef = useRef(null);
  const savePromiseRef = useRef(null);

  useEffect(() => {
    return () => {
      if (autoSaveTimerRef.current) {
        clearTimeout(autoSaveTimerRef.current);
        autoSaveTimerRef.current = null;
      }
    };
  }, []);

  const applyBackendConfig = useCallback((nextConfig) => {
    if (autoSaveTimerRef.current) {
      clearTimeout(autoSaveTimerRef.current);
      autoSaveTimerRef.current = null;
    }
    const normalized = normalizeConfig(nextConfig);
    const snap = configSnapshot(normalized);
    latestConfigRef.current = normalized;
    savedSnapshotRef.current = snap;
    setConfig(normalized);
    setSavedSnapshot(snap);
    return normalized;
  }, []);

  const currentSnapshot = useMemo(() => configSnapshot(config), [config]);
  const dirty = savedSnapshot !== null && currentSnapshot !== savedSnapshot;
  const visibleSaveStatus =
    saveStatus && saveStatus.snapshot === currentSnapshot ? saveStatus : null;
  const configValidForSave = useMemo(
    () =>
      settingsFieldsValid &&
      isValidProxyUrl(config.proxy) &&
      isValidLlmApiBase(config.llm?.apiBase),
    [settingsFieldsValid, config.proxy, config.llm?.apiBase]
  );

  const persistConfigNow = useCallback(
    async ({ force = false } = {}) => {
      if (autoSaveTimerRef.current) {
        clearTimeout(autoSaveTimerRef.current);
        autoSaveTimerRef.current = null;
      }

      if (savePromiseRef.current) {
        try {
          await savePromiseRef.current;
        } catch {
          // Error state is handled by the active save run.
        }
      }

      const runLoop = async () => {
        let forceFirst = force;
        while (mountedRef.current) {
          const target = latestConfigRef.current;
          const normalized = normalizeConfig(target);
          const snap = configSnapshot(normalized);

          if (!forceFirst && savedSnapshotRef.current === snap) {
            return true;
          }
          if (!isValidProxyUrl(target.proxy) || !isValidLlmApiBase(target.llm?.apiBase)) {
            return false;
          }

          forceFirst = false;
          setSaving(true);
          setSaveStatus((prev) => (prev?.type === 'error' ? null : prev));
          try {
            await tauriApi.saveConfig(normalized);
            if (!mountedRef.current) return true;
            savedSnapshotRef.current = snap;
            setSavedSnapshot(snap);
            setSaveStatus({ type: 'success', message: '配置已自动保存', snapshot: snap });
            if (configSnapshot(latestConfigRef.current) === snap) {
              return true;
            }
          } catch (err) {
            if (!mountedRef.current) return false;
            setSaveStatus({
              type: 'error',
              message: err?.message || String(err) || '自动保存配置失败，请检查文件权限',
              snapshot: snap,
            });
            return false;
          } finally {
            if (mountedRef.current) {
              setSaving(false);
            }
          }
        }
        return true;
      };

      const promise = runLoop();
      savePromiseRef.current = promise;
      try {
        return await promise;
      } finally {
        if (savePromiseRef.current === promise) {
          savePromiseRef.current = null;
        }
      }
    },
    [mountedRef]
  );

  const handleRetrySave = useCallback(
    () => persistConfigNow({ force: true }),
    [persistConfigNow]
  );

  useEffect(() => {
    if (autoSaveTimerRef.current) {
      clearTimeout(autoSaveTimerRef.current);
      autoSaveTimerRef.current = null;
    }
    if (savedSnapshot === null || currentSnapshot === savedSnapshot || !configValidForSave) {
      return undefined;
    }
    if (saveStatus?.type === 'error' && saveStatus.snapshot === currentSnapshot) {
      return undefined;
    }
    autoSaveTimerRef.current = setTimeout(() => {
      autoSaveTimerRef.current = null;
      void persistConfigNow();
    }, AUTO_SAVE_DELAY_MS);
    return () => {
      if (autoSaveTimerRef.current) {
        clearTimeout(autoSaveTimerRef.current);
        autoSaveTimerRef.current = null;
      }
    };
  }, [currentSnapshot, savedSnapshot, configValidForSave, saveStatus, persistConfigNow]);

  return {
    config,
    setConfig,
    latestConfigRef,
    saving,
    saveStatus,
    setSaveStatus,
    dirty,
    visibleSaveStatus,
    configValidForSave,
    setSettingsFieldsValid,
    applyBackendConfig,
    persistConfigNow,
    handleRetrySave,
  };
}
