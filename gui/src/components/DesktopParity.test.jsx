import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import WorkspaceView from './WorkspaceView';
import SettingsView from './SettingsView';
import ExecutionLog from './ExecutionLog';
import LoginView from './LoginView';
import { defaultConfig } from '../lib/configModel';
import { tauriApi } from '../lib/tauriApi';

describe('Desktop Parity features (Phases 1-6)', () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it('keeps masteryTarget visible when enableMastery=false and enableExamApi=true, and hides when both are false', () => {
    const baseCfg = {
      ...defaultConfig(),
      enableMastery: false,
      enableExamApi: true,
      courses: [],
    };
    const { rerender } = render(
      <WorkspaceView
        config={baseCfg}
        onChangeConfig={() => {}}
        runtime={{}}
        runState={{ status: 'idle' }}
        onStartStudy={() => {}}
        onReturnToProgress={() => {}}
      />
    );
    expect(screen.getByLabelText('掌握度目标（%）')).toBeTruthy();

    rerender(
      <WorkspaceView
        config={{ ...baseCfg, enableMastery: false, enableExamApi: false }}
        onChangeConfig={() => {}}
        runtime={{}}
        runState={{ status: 'idle' }}
        onStartStudy={() => {}}
        onReturnToProgress={() => {}}
      />
    );
    expect(screen.queryByLabelText('掌握度目标（%）')).toBeNull();
  });

  it('batch-pastes multiple course URLs including URL1 = ... with deduplication and feedback', () => {
    let currentConfig = {
      ...defaultConfig(),
      courses: [
        {
          id: 'existing-1',
          title: '已有课程',
          url: 'https://studyvideoh5.zhihuishu.com/stuStudy?recruitAndCourseId=dup1',
          enabled: true,
        },
      ],
    };
    const onChangeConfig = vi.fn((updater) => {
      currentConfig = typeof updater === 'function' ? updater(currentConfig) : updater;
    });

    render(
      <WorkspaceView
        config={currentConfig}
        onChangeConfig={onChangeConfig}
        runtime={{}}
        runState={{ status: 'idle' }}
        onStartStudy={() => {}}
        onReturnToProgress={() => {}}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: '手动补充链接' }));
    const textarea = screen.getByLabelText('补充课程链接');
    fireEvent.change(textarea, {
      target: {
        value: [
          'URL1 = https://studyvideoh5.zhihuishu.com/stuStudy?recruitAndCourseId=dup1',
          'URL2 = https://fusioncourseh5.zhihuishu.com/stuStudy?recruitAndCourseId=new2',
          'https://ai-smart-course-student-pro.zhihuishu.com/singleCourse/knowledgeStudy?courseId=new3',
          'not-a-valid-url',
        ].join('\n'),
      },
    });
    fireEvent.click(screen.getByRole('button', { name: '批量添加' }));

    expect(currentConfig.courses).toHaveLength(3);
    expect(screen.getByRole('status').textContent).toContain('已添加 2 门课程');
    expect(screen.getByRole('status').textContent).toContain('1 条重复');
    expect(screen.getByRole('status').textContent).toContain('1 条无效');
  });

  it('renders DEBUG logs, hides them under regular info filter, and toggles live debug', () => {
    const onToggleDebug = vi.fn();
    render(
      <ExecutionLog
        logs={[
          { seq: 1, timestamp: 1700000001, level: 'info', message: 'normal info line' },
          { seq: 2, timestamp: 1700000002, level: 'debug', message: 'internal debug detail' },
        ]}
        debugEnabled={true}
        onToggleDebug={onToggleDebug}
      />
    );

    expect(screen.getByText('internal debug detail')).toBeTruthy();
    fireEvent.change(screen.getByLabelText('日志级别'), { target: { value: 'info' } });
    expect(screen.queryByText('internal debug detail')).toBeNull();
    expect(screen.getByText('normal info line')).toBeTruthy();

    fireEvent.click(screen.getByRole('checkbox', { name: '调试日志' }));
    expect(onToggleDebug).toHaveBeenCalledWith(false);
  });

  it('supports driver selection, check_browser, env card, credential clearing, and non-Windows notification guard in SettingsView', () => {
    let cfg = {
      ...defaultConfig(),
      driver: 'Chromium',
      username: '13800138000',
      password: 'secret',
      notificationProvider: 'Windows',
    };
    const onChangeConfig = vi.fn((updater) => {
      cfg = typeof updater === 'function' ? updater(cfg) : updater;
    });
    const onCheckBrowser = vi.fn();
    const onCheckUpdate = vi.fn();
    const onRestartBackend = vi.fn();
    const onOpenPath = vi.fn();

    render(
      <SettingsView
        config={cfg}
        onChangeConfig={onChangeConfig}
        runtime={{
          backendPhase: 'ready',
          platform: 'macos',
          version: '1.1.0',
          backendVersion: '3.20.0(Autovisor GUI Edition)',
          baseDir: '/Users/demo/Library/Application Support/com.autovisor.gui/userdata',
          configPath: '/Users/demo/Library/Application Support/com.autovisor.gui/userdata/config.ini',
          logsDir: '/Users/demo/Library/Application Support/com.autovisor.gui/userdata/logs',
          cookieExists: true,
          cookieCount: 8,
          detectedBrowsers: [],
        }}
        onCheckBrowser={onCheckBrowser}
        onCheckUpdate={onCheckUpdate}
        onRestartBackend={onRestartBackend}
        onOpenPath={onOpenPath}
        onListLogFiles={vi.fn().mockResolvedValue([])}
      />
    );

    expect(screen.getByText(/使用 Playwright 内置 Chromium 时将忽略下方手动可执行路径/)).toBeTruthy();
    expect(screen.getByText(/Windows 系统通知仅 Windows 可用/)).toBeTruthy();
    expect(screen.getByText(/已保存账号 138\*\*\*\*8000/)).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: '检测浏览器与登录状态' }));
    expect(onCheckBrowser).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole('button', { name: '检查更新' }));
    expect(onCheckUpdate).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole('button', { name: '重启内置引擎' }));
    expect(onRestartBackend).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole('button', { name: '打开数据目录' }));
    expect(onOpenPath).toHaveBeenCalledWith(
      '/Users/demo/Library/Application Support/com.autovisor.gui/userdata'
    );

    fireEvent.click(screen.getByRole('button', { name: '清除已保存账密' }));
    expect(cfg.username).toBe('');
    expect(cfg.password).toBe('');
  });

  it('exposes phase 9 answer enhancements, question bank test, and cache export/import in SettingsView', async () => {
    let cfg = {
      ...defaultConfig(),
      proxy: 'http://127.0.0.1:7890',
      llm: { ...defaultConfig().llm, questionBankToken: 'bank-token' },
    };
    const onChangeConfig = vi.fn((updater) => {
      cfg = typeof updater === 'function' ? updater(cfg) : updater;
    });
    const onTestQuestionBank = vi.fn().mockResolvedValue('题库连接正常, 剩余 5 次');
    const onExportQuestionCache = vi.fn().mockResolvedValue('C:\\out.json');
    const onImportQuestionCache = vi.fn().mockResolvedValue({ questions: 3, answers: 4 });
    const onClearQuestionCache = vi
      .fn()
      .mockResolvedValueOnce({ zhidao_questions: 5, ai_questions: 2, json_files: 1 })
      .mockResolvedValueOnce({ zhidao_questions: 0, ai_questions: 0, json_files: 0 });

    render(
      <SettingsView
        config={cfg}
        onChangeConfig={onChangeConfig}
        runtime={{ platform: 'windows' }}
        onListLogFiles={vi.fn().mockResolvedValue([])}
        onTestQuestionBank={onTestQuestionBank}
        onExportQuestionCache={onExportQuestionCache}
        onImportQuestionCache={onImportQuestionCache}
        onClearQuestionCache={onClearQuestionCache}
      />
    );

    expect(screen.getByLabelText('作业达标得分率（%）').value).toBe('100');
    expect(screen.getByLabelText('作业错题重做轮数').value).toBe('3');
    expect(screen.getByLabelText('日志上限（MB）').value).toBe('10');
    const retentionInput = screen.getByLabelText('保留天数（0 表示关闭）');
    const compressInput = screen.getByLabelText('压缩归档天数（0 表示关闭）');
    expect(retentionInput.getAttribute('min')).toBe('0');
    expect(compressInput.getAttribute('min')).toBe('0');
    fireEvent.change(retentionInput, { target: { value: '0' } });
    expect(cfg.logRetentionDays).toBe(0);
    fireEvent.change(compressInput, { target: { value: '0' } });
    expect(cfg.logCompressAfterDays).toBe(0);
    expect(screen.getByLabelText('流式总超时（秒）').value).toBe('120');
    expect(screen.getByLabelText('第三方题库 Token（可选）').value).toBe('bank-token');

    fireEvent.click(screen.getByRole('checkbox', { name: /知到作业优先走加密 API/ }));
    expect(cfg.enableHomeworkApi).toBe(false);

    fireEvent.change(screen.getByLabelText('作业达标得分率（%）'), {
      target: { value: '85' },
    });
    expect(cfg.homeworkThreshold).toBe(85);

    fireEvent.click(screen.getByRole('button', { name: '测试题库连通性' }));
    await waitFor(() => expect(onTestQuestionBank).toHaveBeenCalledTimes(1));
    expect(onTestQuestionBank).toHaveBeenCalledWith({
      token: 'bank-token',
      url: 'https://tk.enncy.cn/query',
      proxy: 'http://127.0.0.1:7890',
    });
    expect(await screen.findByText('题库连接正常, 剩余 5 次')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: '导出题库缓存' }));
    expect(await screen.findByText(/题库缓存已导出到 C:\\out\.json/)).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: '导入题库缓存' }));
    expect(
      await screen.findByText('题库缓存导入完成：题目 3，答案 4')
    ).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: '清理题库缓存' }));
    fireEvent.click(screen.getByRole('button', { name: '取消' }));
    expect(onClearQuestionCache).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: '清理题库缓存' }));
    fireEvent.click(screen.getByRole('button', { name: '确认清理缓存' }));
    await waitFor(() => expect(onClearQuestionCache).toHaveBeenCalledTimes(1));
    expect(
      await screen.findByText('题库缓存已清理：知到题 5 条，AI 题 2 条，快照文件 1 个')
    ).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: '清理题库缓存' }));
    fireEvent.click(screen.getByRole('button', { name: '确认清理缓存' }));
    await waitFor(() => expect(onClearQuestionCache).toHaveBeenCalledTimes(2));
    expect(await screen.findByText('题库缓存已为空，无需清理')).toBeTruthy();
  });

  it('disables both import and clear cache buttons while taskRunning is true', () => {
    render(
      <SettingsView
        config={defaultConfig()}
        onChangeConfig={() => {}}
        taskRunning={true}
        onExportQuestionCache={vi.fn()}
        onImportQuestionCache={vi.fn()}
        onClearQuestionCache={vi.fn()}
        onListLogFiles={vi.fn().mockResolvedValue([])}
      />
    );

    expect(screen.getByRole('button', { name: '导入题库缓存' }).disabled).toBe(true);
    expect(screen.getByRole('button', { name: '清理题库缓存' }).disabled).toBe(true);
    expect(screen.getByRole('button', { name: '导出题库缓存' }).disabled).toBe(false);
  });

  it('syncs exePath when switching browser driver in SettingsView', () => {
    let cfg = {
      ...defaultConfig(),
      driver: 'Edge',
      exePath: 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
    };
    const onChangeConfig = vi.fn((updater) => {
      cfg = typeof updater === 'function' ? updater(cfg) : updater;
    });
    const detectedBrowsers = [
      {
        id: 'edge',
        name: 'Microsoft Edge',
        driver: 'Edge',
        exePath: 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
      },
      {
        id: 'chrome',
        name: 'Google Chrome',
        driver: 'Chrome',
        exePath: 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
      },
    ];

    const { rerender } = render(
      <SettingsView
        config={cfg}
        onChangeConfig={onChangeConfig}
        runtime={{ detectedBrowsers }}
        onListLogFiles={vi.fn().mockResolvedValue([])}
      />
    );

    fireEvent.change(screen.getByLabelText('浏览器内核 / 驱动（driver）'), {
      target: { value: 'Chrome' },
    });
    expect(cfg.driver).toBe('Chrome');
    expect(cfg.exePath).toBe('C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe');

    rerender(
      <SettingsView
        config={cfg}
        onChangeConfig={onChangeConfig}
        runtime={{ detectedBrowsers }}
        onListLogFiles={vi.fn().mockResolvedValue([])}
      />
    );

    fireEvent.change(screen.getByLabelText('浏览器内核 / 驱动（driver）'), {
      target: { value: 'Chromium' },
    });
    expect(cfg.driver).toBe('Chromium');
    expect(cfg.exePath).toBe('');
  });

  it('defaults rememberCredentials to false and renders in-app HTTP QR image on qrcode tab', async () => {
    vi.spyOn(tauriApi, 'listenLoginStatus').mockResolvedValue(() => {});
    const startQrSpy = vi.spyOn(tauriApi, 'startQrLogin').mockResolvedValue({
      ok: true,
      sessionId: 'qr-test-1',
      imageBase64:
        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+X2dIAAAAASUVORK5CYII=',
      savedPath: '',
    });
    const cancelQrSpy = vi.spyOn(tauriApi, 'cancelQrLogin').mockResolvedValue(null);

    const { unmount } = render(
      <LoginView
        runtime={{
          cookieExists: false,
          detectedBrowsers: [
            { id: 'chrome', name: 'Google Chrome', driver: 'Chrome', exePath: '/usr/bin/chrome' },
          ],
        }}
        config={defaultConfig()}
        onLoginSuccess={() => {}}
      />
    );

    const rememberBox = screen.getByRole('checkbox', {
      name: /记住账号密码/,
    });
    expect(rememberBox.checked).toBe(false);

    fireEvent.click(screen.getByRole('tab', { name: '扫码登录' }));
    await waitFor(() => {
      expect(startQrSpy).toHaveBeenCalledTimes(1);
    });
    const img = await screen.findByAltText('智慧树登录二维码');
    expect(img.getAttribute('src')).toContain('data:image/png;base64,');
    expect(screen.getByRole('button', { name: '保存二维码 PNG' })).toBeTruthy();
    expect(screen.getByRole('button', { name: '改用浏览器扫码（备用）' })).toBeTruthy();

    unmount();
    expect(cancelQrSpy).toHaveBeenCalledWith('qr-test-1');
  });
});
