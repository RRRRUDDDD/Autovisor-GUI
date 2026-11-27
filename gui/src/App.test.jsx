import { describe, it, expect, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import App from './App';

describe('App UI flows', () => {
  beforeEach(() => {
    window.localStorage.clear();
    document.documentElement.classList.remove('dark');
  });

  it('shows pre-login screen with auto-detected browser, supports student ID login, auto-populates courses, navigates M3 rail, toggles dark theme, and logs out cleanly', async () => {
    render(<App />);

    // 1. Starts on pre-login view and displays auto-detected device browser
    await waitFor(() => {
      expect(screen.getByText('知到 · 智慧树自动化学习助手')).toBeTruthy();
    });
    expect(screen.queryByText(/v1\.0\.0/i)).toBeNull();
    expect(screen.getByText('Google Chrome')).toBeTruthy();

    // 2. Switch to 学号登录 tab, search & select school, fill student ID + password
    fireEvent.click(screen.getByRole('tab', { name: '学号登录' }));
    const schoolInput = screen.getByLabelText('学校');
    fireEvent.focus(schoolInput);
    fireEvent.change(schoolInput, { target: { value: '清华' } });

    await waitFor(() => {
      expect(screen.getByText('清华大学')).toBeTruthy();
    });
    fireEvent.click(screen.getByText('清华大学'));

    fireEvent.change(screen.getByLabelText('大学学号'), { target: { value: '20230001' } });
    fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'secret123' } });

    fireEvent.click(screen.getByRole('button', { name: /登录并自动识别课程/ }));

    // 3. Enters workspace with auto-discovered courses and AI mastery controls
    await waitFor(() => {
      expect(screen.getByText('选择课程并配置刷课参数')).toBeTruthy();
    });
    expect(screen.queryByText(/Autovisor v/i)).toBeNull();
    expect(screen.getByText('高等数学（上）')).toBeTruthy();
    expect(screen.getByText('人工智能导论')).toBeTruthy();
    expect(screen.getByText(/已选 0 \/ 共 2/)).toBeTruthy();
    expect(screen.getByText('复习模式（刷习惯分）')).toBeTruthy();
    expect(screen.getByText('刷课任务完成后自动关机')).toBeTruthy();
    expect(screen.queryByText('AI 智慧课自动刷掌握度')).toBeNull();
    expect(screen.queryByText('运行环境与进阶配置')).toBeNull();
    const studyRegion = screen.getByRole('region', { name: '任务参数' });
    const studySwitches = Array.from(studyRegion.querySelectorAll('input[type="checkbox"]')).map((el) => el.id);
    expect(studySwitches).toEqual([
      'reviewMode',
      'enableAutoCaptcha',
      'soundOff',
      'enableHideWindow',
      'shutdownOnComplete',
    ]);
    fireEvent.click(screen.getByRole('checkbox', { name: /复习模式（刷习惯分）/ }));
    expect(screen.getByRole('button', { name: '开始复习刷分' })).toBeTruthy();
    fireEvent.click(screen.getByRole('checkbox', { name: /复习模式（刷习惯分）/ }));
    expect(screen.getByRole('button', { name: '开始刷课' })).toBeTruthy();

    // 4. Navigates to 系统设置, verifies swapped order (答题增强与题库 before 大模型智能答题), moved AI mastery controls, notification, Cookie & history log cards, and toggles dark theme from rail
    fireEvent.click(screen.getByText('系统设置'));
    expect(screen.getByText('AI 答题、浏览器、推送通知与凭证日志')).toBeTruthy();
    const settingsHeadings = screen.getAllByRole('heading', { level: 2 }).map((el) => el.textContent);
    expect(settingsHeadings).toEqual([
      '答题增强与题库',
      '大模型智能答题',
      '桌面窗口',
      '浏览器与网络',
      '消息推送通知',
      '智慧树登录 Cookie 管理',
      '历史运行日志',
      '运行环境与版本',
    ]);
    expect(screen.getByText('AI 智慧课自动刷掌握度')).toBeTruthy();
    expect(screen.getByLabelText('掌握度目标（%）')).toBeTruthy();
    expect(screen.getByText('已作答保存的题目仍重新作答')).toBeTruthy();
    expect(screen.getByText('消息推送通知')).toBeTruthy();
    expect(screen.getByLabelText('通知服务').value).toBe('Windows');
    expect(screen.queryByLabelText('通知 URL')).toBeNull();
    fireEvent.change(screen.getByLabelText('通知服务'), { target: { value: 'Telegram' } });
    expect(screen.getByLabelText('通知 URL')).toBeTruthy();
    expect(screen.getByLabelText('Chat ID')).toBeTruthy();
    fireEvent.change(screen.getByLabelText('通知服务'), { target: { value: 'Windows' } });
    expect(screen.queryByLabelText('通知 URL')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '发送测试推送' }));
    await waitFor(() => {
      expect(screen.getByText(/Windows 系统通知发送成功/)).toBeTruthy();
    });
    expect(screen.getByText('大模型智能答题')).toBeTruthy();
    expect(screen.getByLabelText('模型服务商')).toBeTruthy();
    expect(screen.getByLabelText('接口协议')).toBeTruthy();
    expect(screen.getByLabelText('API Base URL')).toBeTruthy();
    fireEvent.change(screen.getByLabelText('模型服务商'), { target: { value: 'anthropic' } });
    expect(screen.getByLabelText('接口协议').value).toBe('anthropic');
    expect(screen.getByLabelText('API Base URL').value).toBe('https://api.anthropic.com');
    fireEvent.change(screen.getByLabelText('模型服务商'), { target: { value: 'ollama' } });
    expect(screen.getByLabelText('接口协议').value).toBe('ollama');
    fireEvent.click(screen.getByRole('button', { name: '测试模型连接' }));
    await waitFor(() => {
      expect(screen.getByText(/连接成功/)).toBeTruthy();
    });
    expect(screen.getByText('智慧树登录 Cookie 管理')).toBeTruthy();
    expect(screen.getByRole('button', { name: '从 JSON 文件导入 Cookie' })).toBeTruthy();
    expect(screen.getByRole('button', { name: '清除本地 Cookie' })).toBeTruthy();
    expect(screen.getByText('历史运行日志')).toBeTruthy();
    expect(screen.getByText('暂无历史日志文件')).toBeTruthy();
    expect(screen.queryByText('环境与诊断')).toBeNull();
    expect(screen.queryByText('依赖下载镜像源')).toBeNull();
    expect(screen.queryByText('应用外观与运行环境')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: '切换到深色模式' }));
    expect(document.documentElement.classList.contains('dark')).toBe(true);

    // 5. Navigates to 运行监控 (idle empty state)
    fireEvent.click(screen.getByText('运行监控'));
    expect(screen.getByText('当前暂无运行中的学习任务')).toBeTruthy();

    // 6. Logs out back to pre-login screen
    fireEvent.click(screen.getByRole('button', { name: /退出登录/ }));
    await waitFor(() => {
      expect(screen.getByRole('tab', { name: '手机号登录' })).toBeTruthy();
    });
  });
});
