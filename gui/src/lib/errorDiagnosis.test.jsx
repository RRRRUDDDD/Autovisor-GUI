import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { diagnoseAttention, diagnoseRunError } from './errorDiagnosis';
import ProgressView from '../components/ProgressView';
import { defaultConfig } from './configModel';

describe('errorDiagnosis classification and recovery guidance', () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it('returns null when runState has no error', () => {
    expect(diagnoseRunError({ status: 'completed', errorMessage: '', logs: [] })).toBeNull();
    expect(diagnoseRunError({ status: 'time_limit', errorMessage: '', logs: [] })).toBeNull();
  });

  it('classifies auth, captcha, browser, network, llm, config, course_progress, and unknown errors accurately', () => {
    expect(
      diagnoseRunError({
        status: 'error',
        errorMessage: 'SessionExpiredError: CAS 未签发考试会话票据, 请重新登录智慧树.',
        logs: [],
      })?.category
    ).toBe('auth');

    expect(
      diagnoseRunError({
        status: 'error',
        errorMessage: 'CaptchaRequiredError: 接口要求验证码(code=-12)',
        logs: [],
      })?.category
    ).toBe('captcha');

    expect(
      diagnoseRunError({
        status: 'error',
        errorMessage: 'TargetClosedError: Target page, context or browser has been closed',
        logs: [],
      })?.category
    ).toBe('browser');

    expect(
      diagnoseRunError({
        status: 'error',
        errorMessage: 'requests.exceptions.ProxyError: Cannot connect to proxy http://127.0.0.1:7890',
        logs: [],
      })?.category
    ).toBe('network');

    expect(
      diagnoseRunError({
        status: 'error',
        errorMessage: 'NonRetryableLLMError: HTTP 401 invalid_api_key sk-secretkey12345678',
        logs: [],
      })
    ).toMatchObject({
      category: 'llm',
      rawDetail: expect.stringContaining('sk-***REDACTED***'),
    });

    expect(
      diagnoseRunError({
        status: 'error',
        exitCode: 2,
        errorMessage: '未检测到有效课程链接，请先勾选或添加课程！',
        logs: [],
      })?.category
    ).toBe('config');

    expect(
      diagnoseRunError({
        status: 'error',
        errorMessage: '任务异常结束（退出码 1），请查看日志',
        logs: [
          { seq: 1, timestamp: 1700000000, level: 'warning', message: '本轮因课时进度未确认而停止.' },
        ],
      })
    ).toMatchObject({
      category: 'course_progress',
      rawDetail: '本轮因课时进度未确认而停止.',
    });

    expect(
      diagnoseRunError({
        status: 'error',
        errorMessage: 'RuntimeError: something unexpected happened',
        logs: [],
      })?.category
    ).toBe('unknown');
  });

  it('provides clear guidance for active needsAttention states', () => {
    expect(diagnoseAttention('检测到安全验证,请手动完成验证...')?.title).toBe('需要处理安全验证');
    expect(diagnoseAttention('配置文件未填写账号密码,请在浏览器中手动登录.')?.title).toBe(
      '需要在浏览器中完成登录'
    );
    expect(diagnoseAttention('课时未选中,保留页面等待手动切课.')?.title).toBe(
      '需要手动点选一下目标课时'
    );
  });

  it('renders categorized error card in ProgressView with actionable buttons and collapsible technical details', () => {
    const onRelogin = vi.fn();
    const onOpenSettings = vi.fn();
    const onOpenPath = vi.fn();

    render(
      <ProgressView
        runState={{
          status: 'error',
          mode: 'study',
          exitCode: 1,
          errorMessage: 'SessionExpiredError: Cookies 已失效，请重新登录',
          logFilePath: 'C:\\Autovisor\\logs\\Log1.txt',
          logs: [
            {
              seq: 1,
              timestamp: 1700000000,
              level: 'error',
              message: 'SessionExpiredError: Cookies 已失效，请重新登录',
            },
          ],
        }}
        config={defaultConfig()}
        onBack={() => {}}
        onStop={() => {}}
        onRestart={() => {}}
        onOpenPath={onOpenPath}
        onOpenSettings={onOpenSettings}
        onRelogin={onRelogin}
      />
    );

    const card = screen.getByRole('alert', { name: '任务异常诊断' });
    expect(within(card).getByText('智慧树登录状态已失效或被挤下线')).toBeTruthy();
    expect(within(card).getByText('建议处理步骤')).toBeTruthy();
    expect(within(card).queryByText(/SessionExpiredError:/)).toBeNull();

    fireEvent.click(within(card).getByRole('button', { name: /查看技术详情/ }));
    expect(within(card).getByText(/SessionExpiredError: Cookies 已失效/)).toBeTruthy();

    fireEvent.click(within(card).getByRole('button', { name: '重新登录' }));
    expect(onRelogin).toHaveBeenCalledTimes(1);

    fireEvent.click(within(card).getByRole('button', { name: '前往系统设置' }));
    expect(onOpenSettings).toHaveBeenCalledTimes(1);

    fireEvent.click(within(card).getByRole('button', { name: '打开完整日志' }));
    expect(onOpenPath).toHaveBeenCalledWith('C:\\Autovisor\\logs\\Log1.txt');
  });
});
