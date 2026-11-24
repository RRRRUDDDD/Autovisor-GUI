import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import ExecutionLog from './ExecutionLog';

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const sampleLogs = [
  { seq: 1, timestamp: 1700000001, level: 'info', message: '初始化浏览器成功' },
  { seq: 2, timestamp: 1700000002, level: 'warning', message: '网络波动重试中' },
  { seq: 3, timestamp: 1700000003, level: 'error', message: '提交失败 token=sk-secret1234567890' },
  { seq: 4, timestamp: 1700000004, level: 'debug', message: 'raw payload detail' },
];

describe('ExecutionLog', () => {
  it('filters by level and search query, and opens log file', () => {
    const onOpenLogFile = vi.fn();
    render(
      <ExecutionLog
        logs={sampleLogs}
        truncated={true}
        logFilePath={'C:\\logs\\Log1.txt'}
        onOpenLogFile={onOpenLogFile}
      />
    );

    expect(screen.getByText('匹配 4 / 4 条')).toBeTruthy();
    expect(screen.getByText(/较早的日志已省略/)).toBeTruthy();

    fireEvent.change(screen.getByLabelText('日志级别'), { target: { value: 'warnings' } });
    expect(screen.getByText('匹配 2 / 4 条')).toBeTruthy();
    expect(screen.queryByText('初始化浏览器成功')).toBeNull();

    fireEvent.change(screen.getByLabelText('搜索日志'), { target: { value: '波动' } });
    expect(screen.getByText('匹配 1 / 4 条')).toBeTruthy();
    expect(screen.getByText('网络波动重试中')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: /打开日志文件/ }));
    expect(onOpenLogFile).toHaveBeenCalledWith('C:\\logs\\Log1.txt');
  });

  it('copies redacted logs via clipboard and falls back to manual copy when clipboard is unavailable', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    });

    const { rerender } = render(<ExecutionLog logs={sampleLogs} />);
    fireEvent.click(screen.getByRole('button', { name: /复制当前结果/ }));
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    expect(writeText.mock.calls[0][0]).not.toContain('sk-secret1234567890');
    expect(await screen.findByText(/已复制当前匹配日志/)).toBeTruthy();

    // Clipboard failure shows manual textarea fallback
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: vi.fn().mockRejectedValue(new Error('denied')) },
    });
    rerender(<ExecutionLog logs={sampleLogs} />);
    fireEvent.click(screen.getByRole('button', { name: /复制当前结果/ }));
    expect(await screen.findByText(/请手动复制下方文本/)).toBeTruthy();
  });
});
