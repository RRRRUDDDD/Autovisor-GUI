import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import SettingsView from './SettingsView';
import LoginView from './LoginView';
import {
  defaultConfig,
  isValidLlmApiBase,
  isValidProxyUrl,
} from '../lib/configModel';
import { tauriApi } from '../lib/tauriApi';

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('Phase 1 (T1.8) frontend fixes', () => {
  it('validates proxy URLs and LLM API bases strictly with URL parsing', () => {
    expect(isValidProxyUrl('')).toBe(true);
    expect(isValidProxyUrl('http://127.0.0.1:7890')).toBe(true);
    expect(isValidProxyUrl('https://proxy.example.com:8443')).toBe(true);
    expect(isValidProxyUrl('socks5://127.0.0.1:1080')).toBe(true);
    expect(isValidProxyUrl('socks5h://user:pass@127.0.0.1:1080')).toBe(true);
    expect(isValidProxyUrl('http://[::1]:7890')).toBe(true);

    expect(isValidProxyUrl('http://127.0.0.1:7890 extra')).toBe(false);
    expect(isValidProxyUrl('http://127.0.0.1:99999')).toBe(false);
    expect(isValidProxyUrl('socks5://127.0.0.1:99999')).toBe(false);
    expect(isValidProxyUrl('ftp://127.0.0.1:21')).toBe(false);
    expect(isValidProxyUrl('http://')).toBe(false);

    expect(isValidLlmApiBase('')).toBe(true);
    expect(isValidLlmApiBase('https://api.deepseek.com/v1')).toBe(true);
    expect(isValidLlmApiBase('http://127.0.0.1:11434/v1')).toBe(true);
    expect(isValidLlmApiBase('https://api.openai.com/v1 bad')).toBe(false);
    expect(isValidLlmApiBase('http://127.0.0.1:70000/v1')).toBe(false);
    expect(isValidLlmApiBase('socks5://127.0.0.1:1080')).toBe(false);
    expect(isValidLlmApiBase('javascript:alert(1)')).toBe(false);
  });

  it('prevents clearing numeric inputs from writing 0 and reports validity changes', async () => {
    let cfg = defaultConfig();
    const onChangeConfig = vi.fn((updater) => {
      cfg = typeof updater === 'function' ? updater(cfg) : updater;
    });
    const onValidityChange = vi.fn();

    render(
      <SettingsView
        config={cfg}
        onChangeConfig={onChangeConfig}
        onValidityChange={onValidityChange}
        runtime={{ platform: 'windows' }}
        onListLogFiles={vi.fn().mockResolvedValue([])}
      />
    );

    const thresholdInput = screen.getByLabelText('作业达标得分率（%）');
    fireEvent.change(thresholdInput, { target: { value: '' } });

    expect(cfg.homeworkThreshold).toBe(100);
    expect(onChangeConfig).not.toHaveBeenCalled();
    await waitFor(() => expect(onValidityChange).toHaveBeenLastCalledWith(false));

    fireEvent.change(thresholdInput, { target: { value: '88' } });
    expect(cfg.homeworkThreshold).toBe(88);
    await waitFor(() => expect(onValidityChange).toHaveBeenLastCalledWith(true));
  });

  it('ignores stale slow log read responses when user selects a newer log file', async () => {
    let resolveSlow;
    const slowPromise = new Promise((res) => {
      resolveSlow = res;
    });
    const onReadLogFile = vi
      .fn()
      .mockImplementationOnce(() => slowPromise)
      .mockResolvedValueOnce('NEW_LOG_CONTENT');

    const files = [
      { name: 'Log1.txt', path: 'C:\\logs\\Log1.txt', sizeLabel: '1 KB', modifiedAt: '10:00' },
      { name: 'Log2.txt', path: 'C:\\logs\\Log2.txt', sizeLabel: '2 KB', modifiedAt: '10:05' },
    ];

    render(
      <SettingsView
        config={defaultConfig()}
        onChangeConfig={() => {}}
        runtime={{ platform: 'windows' }}
        onListLogFiles={vi.fn().mockResolvedValue(files)}
        onReadLogFile={onReadLogFile}
      />
    );

    const log1Btn = await screen.findByText('Log1.txt');
    const log2Btn = screen.getByText('Log2.txt');

    fireEvent.click(log1Btn);
    fireEvent.click(log2Btn);

    expect(await screen.findByText('NEW_LOG_CONTENT')).toBeTruthy();

    resolveSlow('STALE_LOG_CONTENT');
    await Promise.resolve();
    await Promise.resolve();

    expect(screen.getByText('NEW_LOG_CONTENT')).toBeTruthy();
    expect(screen.queryByText('STALE_LOG_CONTENT')).toBeNull();
  });

  it('cancels late-resolving QR session when user switches away from QR tab', async () => {
    let resolveQr;
    const qrPromise = new Promise((res) => {
      resolveQr = res;
    });

    vi.spyOn(tauriApi, 'listenLoginStatus').mockResolvedValue(() => {});
    const startSpy = vi.spyOn(tauriApi, 'startQrLogin').mockReturnValue(qrPromise);
    const cancelSpy = vi.spyOn(tauriApi, 'cancelQrLogin').mockResolvedValue(undefined);
    const pollSpy = vi.spyOn(tauriApi, 'pollQrLogin').mockResolvedValue({ status: 'waiting' });

    render(
      <LoginView
        runtime={{
          platform: 'windows',
          cookieExists: false,
          detectedBrowsers: [],
          savedLogin: null,
        }}
        config={defaultConfig()}
        autoRestoreCookie={false}
        onLoginSuccess={vi.fn()}
      />
    );

    fireEvent.click(screen.getByRole('tab', { name: /扫码登录/ }));
    await waitFor(() => expect(startSpy).toHaveBeenCalledTimes(1));

    // Switch back to phone tab while startQrLogin is still in flight
    fireEvent.click(screen.getByRole('tab', { name: /手机号登录/ }));

    // Late resolution arrives after tab switch
    resolveQr({ sessionId: 'late-qr-sid', imageBase64: 'ZmFrZQ==' });
    await waitFor(() => expect(cancelSpy).toHaveBeenCalledWith('late-qr-sid'));
    expect(pollSpy).not.toHaveBeenCalled();
  });

  it('surfaces error and preserves inputs when clearing saved login fails', async () => {
    vi.spyOn(tauriApi, 'listenLoginStatus').mockResolvedValue(() => {});
    const onClearSavedLogin = vi.fn().mockRejectedValue(new Error('磁盘只读'));

    render(
      <LoginView
        runtime={{
          platform: 'windows',
          cookieExists: false,
          detectedBrowsers: [],
          savedLogin: {
            loginMode: 'phone',
            rememberPhone: true,
            phoneUsername: '13800138000',
            phonePassword: 'secret',
          },
        }}
        config={defaultConfig()}
        autoRestoreCookie={false}
        onLoginSuccess={vi.fn()}
        onClearSavedLogin={onClearSavedLogin}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: '清除已记住' }));
    await waitFor(() => expect(onClearSavedLogin).toHaveBeenCalledWith('phone'));
    expect(await screen.findByText('磁盘只读')).toBeTruthy();
    expect(screen.getByDisplayValue('13800138000')).toBeTruthy();
  });
});
