import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import CloseChoiceDialog from './CloseChoiceDialog';
import ProgressView from './ProgressView';
import { ScheduledShutdownBanner } from './ShutdownBanner';
import App from '../App';
import { tauriApi } from '../lib/tauriApi';
import { defaultConfig } from '../lib/configModel';

describe('CloseChoiceDialog', () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it('acknowledges close prompt, supports remember checkbox, and passes choice to host', async () => {
    let triggerClose;
    vi.spyOn(tauriApi, 'listenCloseRequested').mockImplementation(async (handler) => {
      triggerClose = handler;
      return () => {};
    });
    const ackSpy = vi.spyOn(tauriApi, 'closePromptShown').mockResolvedValue(true);
    const updatedRuntime = { closeBehavior: 'tray' };
    const choiceSpy = vi.spyOn(tauriApi, 'closeChoice').mockResolvedValue(updatedRuntime);
    const onRuntimeUpdated = vi.fn();

    render(<CloseChoiceDialog onRuntimeUpdated={onRuntimeUpdated} />);
    await waitFor(() => expect(triggerClose).toBeTypeOf('function'));

    await act(async () => {
      await triggerClose(7);
    });

    expect(ackSpy).toHaveBeenCalledWith(7);
    expect(screen.getByRole('dialog')).toBeTruthy();
    expect(screen.getByText('关闭窗口')).toBeTruthy();

    fireEvent.click(screen.getByRole('checkbox', { name: '记住我的选择' }));
    fireEvent.click(screen.getByRole('button', { name: '最小化到托盘' }));

    await waitFor(() => {
      expect(choiceSpy).toHaveBeenCalledWith('tray', true);
    });
    expect(onRuntimeUpdated).toHaveBeenCalledWith(updatedRuntime);
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('cancels on Escape without remembering and ignores unacknowledged prompts', async () => {
    let triggerClose;
    vi.spyOn(tauriApi, 'listenCloseRequested').mockImplementation(async (handler) => {
      triggerClose = handler;
      return () => {};
    });
    vi.spyOn(tauriApi, 'closePromptShown').mockResolvedValueOnce(false).mockResolvedValueOnce(true);
    const choiceSpy = vi.spyOn(tauriApi, 'closeChoice').mockResolvedValue(null);

    render(<CloseChoiceDialog />);
    await waitFor(() => expect(triggerClose).toBeTypeOf('function'));

    await act(async () => {
      await triggerClose(1);
    });
    expect(screen.queryByRole('dialog')).toBeNull();

    await act(async () => {
      await triggerClose(2);
    });
    const dialog = screen.getByRole('dialog');
    fireEvent.click(screen.getByRole('checkbox', { name: '记住我的选择' }));
    fireEvent.keyDown(dialog, { key: 'Escape' });

    await waitFor(() => {
      expect(choiceSpy).toHaveBeenCalledWith('cancel', false);
    });
    expect(screen.queryByRole('dialog')).toBeNull();
  });
});

describe('ShutdownBanner & ProgressView shutdown states', () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it('shows armed banner while running and allows cancelling shutdown', () => {
    const onCancelShutdown = vi.fn();
    render(
      <ProgressView
        runState={{
          status: 'running',
          mode: 'study',
          shutdownOnComplete: true,
          shutdown: null,
          logs: [{ seq: 1, timestamp: 1700000000, level: 'info', message: 'running' }],
        }}
        config={defaultConfig()}
        onBack={() => {}}
        onStop={() => {}}
        onRestart={() => {}}
        onOpenPath={() => {}}
        onCancelShutdown={onCancelShutdown}
      />
    );

    expect(screen.getByText(/已开启完成后自动关机/)).toBeTruthy();
    expect(screen.getByText('已开启')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '取消关机' }));
    expect(onCancelShutdown).toHaveBeenCalledTimes(1);
  });

  it('shows scheduled countdown banner and failed / cancelled banners appropriately', () => {
    const onCancel = vi.fn();
    const { rerender } = render(
      <ScheduledShutdownBanner
        shutdownInfo={{
          state: 'scheduled',
          delaySeconds: 60,
          shutdownAt: Date.now() / 1000 + 45,
        }}
        onCancel={onCancel}
      />
    );
    expect(screen.getByRole('alert').textContent).toMatch(/系统将在 \d+ 秒后自动关机/);
    fireEvent.click(screen.getByRole('button', { name: '取消关机' }));
    expect(onCancel).toHaveBeenCalledTimes(1);

    rerender(
      <ProgressView
        runState={{
          status: 'completed',
          mode: 'study',
          shutdownOnComplete: false,
          shutdown: { state: 'failed', error: '权限不足' },
          logs: [{ seq: 1, timestamp: 1700000000, level: 'info', message: 'done' }],
        }}
        config={defaultConfig()}
        onBack={() => {}}
        onStop={() => {}}
        onRestart={() => {}}
        onOpenPath={() => {}}
      />
    );
    expect(screen.getByText(/自动关机启动失败：权限不足/)).toBeTruthy();
  });
});

describe('SettingsView close behavior & shutdown on complete integration', () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it('exposes close-behavior selector in System Settings and shutdown-on-complete switch in Study Parameters with global shutdown banner', async () => {
    let emitRunState;
    const runtime = {
      backendPhase: 'ready',
      backendError: '',
      baseDir: 'C:\\Autovisor\\userdata',
      configPath: 'C:\\Autovisor\\userdata\\config.ini',
      cookieExists: true,
      cookieCount: 5,
      cookieModifiedAt: null,
      version: '1.3.0',
      backendVersion: '3.22.0(Autovisor GUI Edition)',
      closeBehavior: 'ask',
      detectedBrowsers: [],
      session: {
        loggedIn: true,
        displayName: '测试学员',
        account: '13800138000',
        loginMode: 'phone',
        schoolName: '测试大学',
        schoolId: '1001',
      },
    };
    const cfg = {
      ...defaultConfig(),
      courses: [
        {
          id: 'c1',
          title: '高等数学',
          url: 'https://studyvideoh5.zhihuishu.com/stuStudy?recruitAndCourseId=c1',
          enabled: true,
          teacher: '李教授',
          school: '测试大学',
          progress: '20%',
          courseType: '智慧共享课',
          autoDetected: true,
        },
      ],
    };

    vi.spyOn(tauriApi, 'getRuntimeInfo').mockResolvedValue(runtime);
    vi.spyOn(tauriApi, 'loadConfig').mockResolvedValue(cfg);
    vi.spyOn(tauriApi, 'getRunState').mockResolvedValue({
      status: 'idle',
      mode: 'study',
      shutdownOnComplete: false,
      shutdown: null,
      logs: [],
    });
    vi.spyOn(tauriApi, 'listenRunState').mockImplementation(async (handler) => {
      emitRunState = handler;
      return () => {};
    });
    vi.spyOn(tauriApi, 'listenBackendStatus').mockResolvedValue(() => {});
    vi.spyOn(tauriApi, 'listenLoginStatus').mockResolvedValue(() => {});
    vi.spyOn(tauriApi, 'listenCloseRequested').mockResolvedValue(() => {});
    vi.spyOn(tauriApi, 'listLogFiles').mockResolvedValue([]);
    vi.spyOn(tauriApi, 'zhsLogin').mockResolvedValue({
      profile: runtime.session,
      courses: cfg.courses,
      cookieCount: 5,
    });
    const setCloseSpy = vi
      .spyOn(tauriApi, 'setCloseBehavior')
      .mockImplementation(async (behavior) => ({ ...runtime, closeBehavior: behavior }));
    const saveConfigSpy = vi
      .spyOn(tauriApi, 'saveConfig')
      .mockImplementation(async (nextCfg) => structuredClone(nextCfg));
    const cancelShutdownSpy = vi
      .spyOn(tauriApi, 'cancelShutdown')
      .mockResolvedValue({
        status: 'completed',
        mode: 'study',
        shutdownOnComplete: false,
        shutdown: { state: 'cancelled' },
        logs: [],
      });

    render(<App />);
    await waitFor(() => expect(screen.getByText('课程队列')).toBeTruthy());

    // Enable shutdownOnComplete in 学习参数 (WorkspaceView) and verify auto-save
    fireEvent.click(screen.getByRole('checkbox', { name: /刷课任务完成后自动关机/ }));
    await waitFor(() => {
      expect(saveConfigSpy).toHaveBeenCalled();
      expect(saveConfigSpy.mock.calls.at(-1)[0].shutdownOnComplete).toBe(true);
    });

    // Open 系统设置 and verify 桌面窗口 close-behavior selector
    fireEvent.click(screen.getByRole('button', { name: '系统设置' }));
    expect(screen.getByText('桌面窗口')).toBeTruthy();

    // Change close behavior
    const closeSelect = screen.getByLabelText('关闭窗口时');
    expect(closeSelect.value).toBe('ask');
    fireEvent.change(closeSelect, { target: { value: 'minimize' } });
    await waitFor(() => {
      expect(setCloseSpy).toHaveBeenCalledWith('minimize');
      expect(closeSelect.value).toBe('minimize');
    });

    // Global scheduled shutdown banner appears even on settings tab and can be cancelled
    await act(async () => {
      emitRunState({
        status: 'completed',
        mode: 'study',
        shutdownOnComplete: true,
        shutdown: {
          state: 'scheduled',
          delaySeconds: 60,
          shutdownAt: Date.now() / 1000 + 50,
        },
        logs: [],
      });
    });

    expect(screen.getByText(/系统将在 \d+ 秒后自动关机/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '取消关机' }));
    await waitFor(() => {
      expect(cancelShutdownSpy).toHaveBeenCalledTimes(1);
    });
  });
});
