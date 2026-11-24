import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import LoginView from './LoginView';
import { defaultConfig } from '../lib/configModel';
import { tauriApi } from '../lib/tauriApi';

afterEach(() => {
  vi.useRealTimers();
  cleanup();
  vi.restoreAllMocks();
});

const baseRuntime = {
  platform: 'windows',
  cookieExists: false,
  detectedBrowsers: [
    {
      id: 'edge',
      name: 'Microsoft Edge',
      driver: 'Edge',
      exePath: 'C:\\msedge.exe',
      recommended: true,
    },
  ],
  savedLogin: null,
};

describe('LoginView', () => {
  it('walks QR login state machine: waiting -> scanned -> confirmed -> triggers cookie login', async () => {
    vi.useFakeTimers();
    vi.spyOn(tauriApi, 'listenLoginStatus').mockResolvedValue(() => {});
    vi.spyOn(tauriApi, 'startQrLogin').mockResolvedValue({
      sessionId: 'qr-1',
      imageBase64: 'ZmFrZQ==',
    });
    const pollSpy = vi
      .spyOn(tauriApi, 'pollQrLogin')
      .mockResolvedValueOnce({ status: 'scanned' })
      .mockResolvedValueOnce({ status: 'confirmed' });
    const loginSpy = vi.spyOn(tauriApi, 'zhsLogin').mockResolvedValue({
      profile: { loggedIn: true, displayName: '扫码同学' },
      courses: [],
      cookieCount: 5,
    });
    const onLoginSuccess = vi.fn().mockResolvedValue(undefined);

    render(
      <LoginView
        runtime={baseRuntime}
        config={defaultConfig()}
        autoRestoreCookie={false}
        onLoginSuccess={onLoginSuccess}
      />
    );

    await act(async () => {
      fireEvent.click(screen.getByRole('tab', { name: /扫码登录/ }));
      await Promise.resolve();
    });
    expect(screen.getByText('请使用智慧树 / 知到 App 扫码')).toBeTruthy();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1500);
    });
    expect(pollSpy).toHaveBeenCalledTimes(1);
    expect(screen.getByText('已扫码，请在手机端确认登录')).toBeTruthy();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1500);
    });
    expect(pollSpy).toHaveBeenCalledTimes(2);
    expect(loginSpy).toHaveBeenCalledWith(
      expect.objectContaining({ mode: 'cookie', browserExe: 'C:\\msedge.exe' })
    );
    expect(onLoginSuccess).toHaveBeenCalledTimes(1);
  });

  it('submits encrypted saved password with useSavedPassword=true without plaintext and clears remembered login', async () => {
    vi.spyOn(tauriApi, 'listenLoginStatus').mockResolvedValue(() => {});
    const loginSpy = vi.spyOn(tauriApi, 'zhsLogin').mockResolvedValue({
      profile: { loggedIn: true, displayName: '张同学' },
      courses: [],
      cookieCount: 3,
    });
    const onLoginSuccess = vi.fn().mockResolvedValue(undefined);
    const onClearSavedLogin = vi.fn().mockResolvedValue({
      ...baseRuntime,
      savedLogin: null,
    });

    render(
      <LoginView
        runtime={{
          ...baseRuntime,
          savedLogin: {
            loginMode: 'phone',
            rememberPhone: true,
            phoneUsername: '13800138000',
            hasSavedPhonePassword: true,
          },
        }}
        config={defaultConfig()}
        autoRestoreCookie={false}
        onLoginSuccess={onLoginSuccess}
        onClearSavedLogin={onClearSavedLogin}
      />
    );

    const passwordInput = screen.getByLabelText('密码');
    expect(passwordInput.value).toBe('');
    expect(passwordInput.placeholder).toContain('已加密保存密码');

    // Submit directly without retyping password
    fireEvent.click(screen.getByRole('button', { name: /登录并自动识别课程/ }));
    await waitFor(() => expect(loginSpy).toHaveBeenCalledTimes(1));
    expect(loginSpy).toHaveBeenCalledWith(
      expect.objectContaining({
        mode: 'phone',
        username: '13800138000',
        password: '',
        useSavedPassword: true,
        rememberCredentials: true,
      })
    );

    // Clear remembered credentials resets inputs and saved flag
    fireEvent.click(screen.getByRole('button', { name: '清除已记住' }));
    await waitFor(() => expect(onClearSavedLogin).toHaveBeenCalledWith('phone'));
    expect(screen.getByLabelText('手机号').value).toBe('');
    expect(screen.getByLabelText('密码').placeholder).toBe('请输入密码');
  });
});
