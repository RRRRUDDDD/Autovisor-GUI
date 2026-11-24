import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import ProgressView from './ProgressView';
import { defaultConfig } from '../lib/configModel';

afterEach(() => {
  cleanup();
});

describe('ProgressView', () => {
  it('renders idle empty state and navigates back to course queue', () => {
    const onBack = vi.fn();
    render(
      <ProgressView
        runState={{ status: 'idle', logs: [] }}
        config={defaultConfig()}
        onBack={onBack}
      />
    );

    expect(screen.getByText('当前暂无运行中的学习任务')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: /前往课程队列/ }));
    expect(onBack).toHaveBeenCalledTimes(1);
  });

  it('requires two-click confirmation before invoking onStop while running', () => {
    const onStop = vi.fn();
    render(
      <ProgressView
        runState={{
          status: 'running',
          mode: 'study',
          stage: '播放视频',
          courseIndex: 1,
          courseTotal: 2,
          currentCourse: '高等数学',
          currentLesson: '1.1 极限',
          lessonProgress: 60,
          startedAt: Math.floor(Date.now() / 1000) - 90,
          logs: [{ seq: 1, timestamp: 1700000000, level: 'info', message: '开始播放视频' }],
        }}
        config={defaultConfig()}
        onStop={onStop}
        onBack={vi.fn()}
      />
    );

    expect(screen.getByText('学习中')).toBeTruthy();
    expect(screen.getByText('高等数学')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: /停止任务/ }));
    expect(onStop).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: /确认停止/ }));
    expect(onStop).toHaveBeenCalledTimes(1);
  });

  it('shows terminal restart button and completed status when finished', () => {
    const onRestart = vi.fn();
    render(
      <ProgressView
        runState={{
          status: 'completed',
          mode: 'study',
          stage: '已结束',
          courseIndex: 1,
          courseTotal: 1,
          currentCourse: '大学物理',
          lessonProgress: 100,
          startedAt: 1700000000,
          finishedAt: 1700000600,
          logs: [{ seq: 1, timestamp: 1700000600, level: 'success', message: '全部课程完成' }],
        }}
        config={defaultConfig()}
        onRestart={onRestart}
        onBack={vi.fn()}
      />
    );

    expect(screen.getByText('已完成')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: /再次运行/ }));
    expect(onRestart).toHaveBeenCalledTimes(1);
  });
});
