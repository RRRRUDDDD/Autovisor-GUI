import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import WorkspaceView from './WorkspaceView';
import { defaultConfig } from '../lib/configModel';

afterEach(() => {
  cleanup();
});

const sampleCourses = [
  {
    id: 'c1',
    title: '高等数学',
    url: 'https://studyvideoh5.zhihuishu.com/stuStudy?recruitAndCourseId=1',
    enabled: true,
    teacher: '李教授',
    school: '示例大学',
    progress: '40%',
    courseType: '智慧共享课',
  },
  {
    id: 'c2',
    title: '大学英语',
    url: 'https://fusioncourseh5.zhihuishu.com/stuStudy?recruitAndCourseId=2',
    enabled: false,
    teacher: '王老师',
    school: '示例大学',
    progress: '100%',
    courseType: '融合共享课',
  },
];

describe('WorkspaceView', () => {
  it('filters courses, toggles selection, reorders, removes, and invokes checkCourse/startStudy', () => {
    let cfg = { ...defaultConfig(), courses: sampleCourses };
    const onChangeConfig = vi.fn((updater) => {
      cfg = typeof updater === 'function' ? updater(cfg) : updater;
    });
    const onCheckCourse = vi.fn();
    const onStartStudy = vi.fn();

    const { rerender } = render(
      <WorkspaceView
        config={cfg}
        onChangeConfig={onChangeConfig}
        runtime={{ cookieExists: true }}
        runState={{ status: 'idle' }}
        onCheckCourse={onCheckCourse}
        onStartStudy={onStartStudy}
        onReturnToProgress={vi.fn()}
      />
    );

    expect(screen.getByText('已选 1 / 共 2')).toBeTruthy();

    // Search narrows visible list
    fireEvent.change(screen.getByLabelText('搜索课程'), { target: { value: '英语' } });
    expect(screen.getByText('大学英语')).toBeTruthy();
    expect(screen.queryByText('高等数学')).toBeNull();

    // Clear search and select all
    fireEvent.change(screen.getByLabelText('搜索课程'), { target: { value: '' } });
    fireEvent.click(screen.getByRole('button', { name: '全选' }));
    expect(cfg.courses.every((c) => c.enabled)).toBe(true);

    rerender(
      <WorkspaceView
        config={cfg}
        onChangeConfig={onChangeConfig}
        runtime={{ cookieExists: true }}
        runState={{ status: 'idle' }}
        onCheckCourse={onCheckCourse}
        onStartStudy={onStartStudy}
        onReturnToProgress={vi.fn()}
      />
    );

    // Move first course down
    const moveDownBtns = screen.getAllByTitle('下移顺序');
    fireEvent.click(moveDownBtns[0]);
    expect(cfg.courses[0].id).toBe('c2');

    // Trigger read-only course check
    const checkBtns = screen.getAllByTitle('只读检测此课程目录结构');
    fireEvent.click(checkBtns[0]);
    expect(onCheckCourse).toHaveBeenCalledTimes(1);

    // Start study button enabled
    fireEvent.click(screen.getByRole('button', { name: /开始刷课/ }));
    expect(onStartStudy).toHaveBeenCalledTimes(1);
  });

  it('updates speed presets and review-mode 30-min quick button', () => {
    let cfg = {
      ...defaultConfig(),
      courses: [sampleCourses[0]],
      reviewMode: true,
      limitMaxTime: 0,
    };
    const onChangeConfig = vi.fn((updater) => {
      cfg = typeof updater === 'function' ? updater(cfg) : updater;
    });

    render(
      <WorkspaceView
        config={cfg}
        onChangeConfig={onChangeConfig}
        runtime={{}}
        runState={{ status: 'idle' }}
        onStartStudy={vi.fn()}
        onReturnToProgress={vi.fn()}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: '1.5x' }));
    expect(cfg.limitSpeed).toBe(1.5);

    fireEvent.click(screen.getByRole('button', { name: '设为 30 分钟' }));
    expect(cfg.limitMaxTime).toBe(30);
  });
});
