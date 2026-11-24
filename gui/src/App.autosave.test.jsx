import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import App from './App';
import { tauriApi } from './lib/tauriApi';
import { defaultConfig } from './lib/configModel';

function makeRuntime(overrides = {}) {
  return {
    backendPhase: 'ready',
    backendError: '',
    baseDir: 'C:\\Autovisor\\userdata',
    configPath: 'C:\\Autovisor\\userdata\\config.ini',
    cookieExists: true,
    cookieCount: 5,
    cookieModifiedAt: null,
    version: '1.2.0',
    backendVersion: '3.21.0(Autovisor GUI Edition)',
    closeBehavior: 'exit',
    detectedBrowsers: [
      {
        id: 'chrome',
        name: 'Google Chrome',
        driver: 'Chrome',
        exePath: 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
        source: 'registry',
        recommended: true,
      },
    ],
    session: {
      loggedIn: true,
      displayName: '测试学员',
      account: '13800138000',
      loginMode: 'phone',
      schoolName: '测试大学',
      schoolId: '1001',
    },
    ...overrides,
  };
}

function makeCourse(id, title, enabled = true) {
  return {
    id,
    title,
    url: `https://studyvideoh5.zhihuishu.com/stuStudy?recruitAndCourseId=${id}`,
    enabled,
    teacher: '张老师',
    school: '测试大学',
    progress: '20%',
    courseType: '智慧共享课',
    autoDetected: true,
  };
}

function makeConfig(overrides = {}) {
  return {
    ...defaultConfig(),
    driver: 'Chrome',
    exePath: 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
    courses: [makeCourse('c1', '高等数学', true), makeCourse('c2', '大学英语', false)],
    ...overrides,
  };
}

function makeIdleRunState(overrides = {}) {
  return {
    status: 'idle',
    mode: 'study',
    startedAt: null,
    finishedAt: null,
    exitCode: null,
    stage: '就绪',
    courseIndex: 0,
    courseTotal: 0,
    currentCourse: '',
    catalogType: '',
    currentLesson: '',
    lessonProgress: 0,
    progressDesc: '',
    remainingMinutes: null,
    courseElapsedMin: 0,
    completedLessons: [],
    needsAttention: '',
    errorMessage: '',
    logFilePath: '',
    logs: [],
    logsTruncated: false,
    ...overrides,
  };
}

async function flushMicrotasks(rounds = 8) {
  for (let i = 0; i < rounds; i += 1) {
    await Promise.resolve();
  }
}

describe('App auto-save configuration', () => {
  let saveConfigSpy;
  let startAutovisorSpy;

  beforeEach(() => {
    vi.useFakeTimers();
    const initialCfg = makeConfig();
    const runtime = makeRuntime();

    vi.spyOn(tauriApi, 'getRuntimeInfo').mockResolvedValue(runtime);
    vi.spyOn(tauriApi, 'loadConfig').mockResolvedValue(initialCfg);
    vi.spyOn(tauriApi, 'getRunState').mockResolvedValue(makeIdleRunState());
    vi.spyOn(tauriApi, 'listenRunState').mockResolvedValue(() => {});
    vi.spyOn(tauriApi, 'listenBackendStatus').mockResolvedValue(() => {});
    vi.spyOn(tauriApi, 'listenLoginStatus').mockResolvedValue(() => {});
    vi.spyOn(tauriApi, 'zhsLogin').mockResolvedValue({
      profile: runtime.session,
      courses: initialCfg.courses,
      cookieCount: 5,
    });
    saveConfigSpy = vi
      .spyOn(tauriApi, 'saveConfig')
      .mockImplementation(async (cfg) => structuredClone(cfg));
    startAutovisorSpy = vi
      .spyOn(tauriApi, 'startAutovisor')
      .mockResolvedValue(makeIdleRunState({ status: 'running', stage: '启动中' }));
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  async function renderSignedInWorkspace() {
    render(<App />);
    await act(async () => {
      await flushMicrotasks(12);
    });
    expect(screen.getByText('课程队列')).toBeTruthy();
  }

  it('auto-saves after toggling a course and omits manual save buttons', async () => {
    await renderSignedInWorkspace();

    expect(saveConfigSpy).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: /仅保存配置|保存配置/ })).toBeNull();

    const englishCheckbox = screen.getByRole('button', { name: '选择课程 大学英语' });
    fireEvent.click(englishCheckbox);

    await act(async () => {
      vi.advanceTimersByTime(260);
      await flushMicrotasks();
    });

    expect(saveConfigSpy).toHaveBeenCalledTimes(1);
    const saved = saveConfigSpy.mock.calls[0][0];
    expect(saved.courses.find((c) => c.id === 'c2').enabled).toBe(true);
    expect(screen.getByText('配置已自动保存')).toBeTruthy();
  });

  it('coalesces rapid selections and serializes edits made during an in-flight save', async () => {
    let resolveFirstSave;
    saveConfigSpy.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveFirstSave = resolve;
        })
    );

    await renderSignedInWorkspace();

    fireEvent.click(screen.getByRole('button', { name: '选择课程 大学英语' }));
    fireEvent.click(screen.getByRole('button', { name: '1.5x' }));

    await act(async () => {
      vi.advanceTimersByTime(260);
      await flushMicrotasks();
    });

    // Rapid edits before debounce collapsed into a single write.
    expect(saveConfigSpy).toHaveBeenCalledTimes(1);
    expect(saveConfigSpy.mock.calls[0][0].limitSpeed).toBe(1.5);

    // Edit again while the first save is still in-flight.
    fireEvent.click(screen.getByRole('button', { name: '1.8x' }));

    await act(async () => {
      resolveFirstSave(saveConfigSpy.mock.calls[0][0]);
      await flushMicrotasks();
    });

    // Follow-up save runs immediately after the first save completes.
    expect(saveConfigSpy).toHaveBeenCalledTimes(2);
    expect(saveConfigSpy.mock.calls[1][0].limitSpeed).toBe(1.8);
  });

  it('flushes pending debounced changes before starting study', async () => {
    await renderSignedInWorkspace();

    fireEvent.click(screen.getByRole('button', { name: '1.8x' }));
    expect(saveConfigSpy).not.toHaveBeenCalled();

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: '开始刷课' }));
      await flushMicrotasks();
    });

    expect(saveConfigSpy).toHaveBeenCalledTimes(1);
    expect(saveConfigSpy.mock.calls[0][0].limitSpeed).toBe(1.8);
    expect(startAutovisorSpy).toHaveBeenCalledTimes(1);
    expect(saveConfigSpy.mock.invocationCallOrder[0]).toBeLessThan(
      startAutovisorSpy.mock.invocationCallOrder[0]
    );
  });

  it('pauses auto-save for invalid settings and resumes once fixed without manual save button', async () => {
    await renderSignedInWorkspace();

    fireEvent.click(screen.getByRole('button', { name: '系统设置' }));
    expect(screen.queryByRole('button', { name: /保存配置|立即保存设置/ })).toBeNull();

    const proxyInput = screen.getByLabelText('代理服务器（可选）');
    fireEvent.change(proxyInput, { target: { value: 'invalid-proxy' } });

    await act(async () => {
      vi.advanceTimersByTime(400);
      await flushMicrotasks();
    });
    expect(saveConfigSpy).not.toHaveBeenCalled();

    fireEvent.change(proxyInput, { target: { value: 'http://127.0.0.1:7890' } });
    await act(async () => {
      vi.advanceTimersByTime(260);
      await flushMicrotasks();
    });

    expect(saveConfigSpy).toHaveBeenCalledTimes(1);
    expect(saveConfigSpy.mock.calls[0][0].proxy).toBe('http://127.0.0.1:7890');
  });

  it('does not loop on save failure and allows retry', async () => {
    saveConfigSpy.mockRejectedValueOnce(new Error('磁盘只读'));

    await renderSignedInWorkspace();
    fireEvent.click(screen.getByRole('button', { name: '1.5x' }));

    await act(async () => {
      vi.advanceTimersByTime(260);
      await flushMicrotasks();
    });

    expect(saveConfigSpy).toHaveBeenCalledTimes(1);
    expect(screen.getByText('磁盘只读')).toBeTruthy();

    // Waiting longer must not trigger an infinite retry loop.
    await act(async () => {
      vi.advanceTimersByTime(1000);
      await flushMicrotasks();
    });
    expect(saveConfigSpy).toHaveBeenCalledTimes(1);

    // Clicking retry succeeds.
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: '重试' }));
      await flushMicrotasks();
    });

    expect(saveConfigSpy).toHaveBeenCalledTimes(2);
    expect(screen.getByText('配置已自动保存')).toBeTruthy();
  });
});
