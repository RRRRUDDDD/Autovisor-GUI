import { useCallback, useEffect, useState } from 'react';

const STORAGE_KEY = 'autovisor-m3-theme';
const VALID_THEMES = new Set(['system', 'light', 'dark']);

export function getStoredTheme() {
  if (typeof window === 'undefined') return 'system';
  try {
    const saved = window.localStorage.getItem(STORAGE_KEY);
    return VALID_THEMES.has(saved) ? saved : 'system';
  } catch {
    return 'system';
  }
}

export function resolveDark(preference) {
  if (preference === 'dark') return true;
  if (preference === 'light') return false;
  if (typeof window !== 'undefined' && typeof window.matchMedia === 'function') {
    return window.matchMedia('(prefers-color-scheme: dark)').matches;
  }
  return false;
}

export function applyTheme(preference) {
  if (typeof document === 'undefined') return;
  const isDark = resolveDark(preference);
  document.documentElement.classList.toggle('dark', isDark);
  document.documentElement.dataset.theme = preference;
}

export function initTheme() {
  applyTheme(getStoredTheme());
}

export function useTheme() {
  const [preference, setPreferenceState] = useState(() => getStoredTheme());
  const [isDark, setIsDark] = useState(() => resolveDark(getStoredTheme()));

  const setTheme = useCallback((next) => {
    const safe = VALID_THEMES.has(next) ? next : 'system';
    setPreferenceState(safe);
    try {
      window.localStorage.setItem(STORAGE_KEY, safe);
    } catch {
      // ignore storage errors
    }
    applyTheme(safe);
    setIsDark(resolveDark(safe));
  }, []);

  const toggleLightDark = useCallback(() => {
    setTheme(isDark ? 'light' : 'dark');
  }, [isDark, setTheme]);

  useEffect(() => {
    applyTheme(preference);
    setIsDark(resolveDark(preference));

    if (preference !== 'system' || typeof window === 'undefined' || typeof window.matchMedia !== 'function') {
      return undefined;
    }
    const media = window.matchMedia('(prefers-color-scheme: dark)');
    const onChange = () => {
      applyTheme('system');
      setIsDark(media.matches);
    };
    media.addEventListener?.('change', onChange);
    return () => media.removeEventListener?.('change', onChange);
  }, [preference]);

  return { preference, isDark, setTheme, toggleLightDark };
}
