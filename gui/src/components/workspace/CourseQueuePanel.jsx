import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  AlertCircle,
  ArrowDown,
  ArrowUp,
  BookOpen,
  Check,
  CheckCircle2,
  GraduationCap,
  Plus,
  RefreshCw,
  Search,
  Stethoscope,
  Trash2,
  User,
} from 'lucide-react';
import Button from '../ui/Button';
import ManualCourseAddForm from './ManualCourseAddForm';
import { detectCourseType } from '../../lib/configModel';

export default function CourseQueuePanel({
  courses,
  enabledCount,
  refreshingCourses,
  courseSyncNotice,
  taskRunning,
  onChangeConfig,
  onRefreshCourses,
  onCheckCourse,
}) {
  const [query, setQuery] = useState('');
  const [showManualAdd, setShowManualAdd] = useState(false);

  const courseIndexMap = useMemo(() => {
    const map = new Map();
    courses.forEach((c, idx) => map.set(c.id, idx));
    return map;
  }, [courses]);

  const filteredCourses = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return courses;
    return courses.filter(
      (c) =>
        c.title.toLowerCase().includes(q) ||
        (c.teacher || '').toLowerCase().includes(q) ||
        (c.school || '').toLowerCase().includes(q) ||
        detectCourseType(c.url).label.toLowerCase().includes(q)
    );
  }, [courses, query]);

  const toggleCourse = (id) => {
    onChangeConfig((prev) => ({
      ...prev,
      courses: prev.courses.map((c) => (c.id === id ? { ...c, enabled: !c.enabled } : c)),
    }));
  };

  const selectAllVisible = useCallback(() => {
    const visibleIds = new Set(filteredCourses.map((c) => c.id));
    onChangeConfig((prev) => ({
      ...prev,
      courses: prev.courses.map((c) => (visibleIds.has(c.id) ? { ...c, enabled: true } : c)),
    }));
  }, [filteredCourses, onChangeConfig]);

  const clearSelection = () => {
    onChangeConfig((prev) => ({
      ...prev,
      courses: prev.courses.map((c) => ({ ...c, enabled: false })),
    }));
  };

  useEffect(() => {
    if (!filteredCourses.length) return undefined;
    const onKeyDown = (event) => {
      if (
        event.defaultPrevented ||
        !(event.ctrlKey || event.metaKey) ||
        event.altKey ||
        event.shiftKey ||
        event.key.toLowerCase() !== 'a'
      ) {
        return;
      }
      if (
        event.target?.closest?.(
          'input, textarea, select, [contenteditable]:not([contenteditable="false"])'
        )
      ) {
        return;
      }
      event.preventDefault();
      selectAllVisible();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [filteredCourses.length, selectAllVisible]);

  const moveCourse = (index, direction) => {
    const target = index + direction;
    if (target < 0 || target >= courses.length) return;
    onChangeConfig((prev) => {
      const copy = [...prev.courses];
      const [item] = copy.splice(index, 1);
      copy.splice(target, 0, item);
      return { ...prev, courses: copy };
    });
  };

  const removeCourse = (id) => {
    onChangeConfig((prev) => ({
      ...prev,
      courses: prev.courses.filter((c) => c.id !== id),
    }));
  };

  const appendCourses = useCallback(
    (newItems) => {
      onChangeConfig((prev) => ({ ...prev, courses: [...prev.courses, ...newItems] }));
    },
    [onChangeConfig]
  );

  return (
    <section
      aria-label="课程列表"
      className="flex min-h-0 flex-col overflow-hidden rounded-[24px] bg-surface-container-low animate-stagger-up"
    >
      <div className="flex flex-wrap items-center justify-between gap-3 px-5 py-4">
        <div className="flex items-center gap-2.5">
          <h2 className="flex items-center gap-2 text-base font-semibold text-on-surface">
            <BookOpen className="h-4.5 w-4.5 text-primary" aria-hidden="true" />
            课程队列
          </h2>
          <span className="rounded-full bg-secondary-container px-2.5 py-0.5 text-xs font-medium text-on-secondary-container tnum">
            已选 {enabledCount} / 共 {courses.length}
          </span>
        </div>

        <div className="flex flex-wrap items-center gap-1.5">
          <div className="relative">
            <Search
              className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-on-surface-variant"
              aria-hidden="true"
            />
            <input
              type="search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="搜索课程或教师"
              aria-label="搜索课程"
              className="h-8 w-36 rounded-full bg-surface-container-highest pl-8 pr-3 text-xs text-on-surface placeholder:text-on-surface-variant/80 focus:outline-none focus:ring-2 focus:ring-primary sm:w-44"
            />
          </div>

          <Button
            variant="tonal"
            size="sm"
            onClick={onRefreshCourses}
            disabled={refreshingCourses || taskRunning}
            title="从智慧树重新自动拉取当前账号的全部课程"
          >
            <RefreshCw
              className={`h-3.5 w-3.5 ${refreshingCourses ? 'animate-spin' : ''}`}
              aria-hidden="true"
            />
            {refreshingCourses ? '识别中...' : '刷新课程'}
          </Button>

          <Button
            variant="ghost"
            size="sm"
            onClick={selectAllVisible}
            disabled={!filteredCourses.length}
            title="Ctrl+A 全选当前列表"
          >
            全选
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={clearSelection}
            disabled={!enabledCount}
          >
            清空
          </Button>
        </div>
      </div>

      {courseSyncNotice && (
        <div
          role={courseSyncNotice.type === 'error' ? 'alert' : 'status'}
          className={`mx-4 mb-2 flex items-center gap-2 rounded-2xl px-4 py-2 text-xs font-medium ${
            courseSyncNotice.type === 'error'
              ? 'bg-error-container text-on-error-container'
              : courseSyncNotice.type === 'success'
                ? 'bg-success-container text-on-success-container'
                : 'bg-primary-container text-on-primary-container'
          }`}
        >
          {courseSyncNotice.type === 'error' ? (
            <AlertCircle className="h-3.5 w-3.5 shrink-0" />
          ) : (
            <CheckCircle2 className="h-3.5 w-3.5 shrink-0" />
          )}
          <span>{courseSyncNotice.message}</span>
        </div>
      )}

      {courses.length === 0 ? (
        <div className="flex flex-1 flex-col items-center justify-center px-8 py-14 text-center">
          <div className="flex h-14 w-14 items-center justify-center rounded-[20px] bg-primary-container text-on-primary-container">
            <BookOpen className="h-6 w-6" aria-hidden="true" />
          </div>
          <p className="mt-4 text-base font-semibold text-on-surface">暂未识别到进行中的课程</p>
          <p className="mt-1.5 max-w-md text-[13px] leading-relaxed text-on-surface-variant">
            系统会在登录后自动读取你的智慧树在学课程（共享课、融合共享课、翻转课、AI 慕课、AI 智慧课），无需手动粘贴链接。
          </p>
          <div className="mt-6 flex flex-wrap items-center justify-center gap-2.5">
            <Button
              size="sm"
              onClick={onRefreshCourses}
              disabled={refreshingCourses || taskRunning}
            >
              <RefreshCw className={`h-3.5 w-3.5 ${refreshingCourses ? 'animate-spin' : ''}`} />
              {refreshingCourses ? '正在识别课程...' : '重新自动识别课程'}
            </Button>
            <Button size="sm" variant="tonal" onClick={() => setShowManualAdd((v) => !v)}>
              <Plus className="h-3.5 w-3.5" />
              手动补充链接
            </Button>
          </div>
        </div>
      ) : (
        <ul className="min-h-0 flex-1 space-y-1.5 overflow-y-auto overscroll-contain px-3 pb-3">
          {filteredCourses.map((course) => {
            const realIndex = courseIndexMap.get(course.id) ?? -1;
            const courseType = detectCourseType(course.url);
            const isDone = course.progress === '100%' || course.progress === '已完成';
            return (
              <li key={course.id}>
                <div
                  className={`group flex items-center gap-3.5 rounded-2xl px-3.5 py-3 transition-all duration-150 ease-m3 ${
                    course.enabled
                      ? 'bg-secondary-container/65 text-on-surface'
                      : 'bg-surface-container/60 text-on-surface hover:bg-surface-container-high/80'
                  }`}
                >
                  <button
                    type="button"
                    onClick={() => toggleCourse(course.id)}
                    aria-pressed={course.enabled}
                    aria-label={`选择课程 ${course.title}`}
                    className="flex shrink-0 items-center justify-center rounded-md p-0.5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
                  >
                    <span
                      className={`flex h-[20px] w-[20px] items-center justify-center rounded-[6px] border-2 transition-all duration-150 ease-m3 ${
                        course.enabled
                          ? 'border-primary bg-primary text-on-primary'
                          : 'border-outline bg-transparent text-transparent group-hover:border-on-surface'
                      }`}
                    >
                      {course.enabled && <Check className="h-3.5 w-3.5" strokeWidth={3} />}
                    </span>
                  </button>

                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <button
                        type="button"
                        onClick={() => toggleCourse(course.id)}
                        className="min-w-0 truncate text-left text-sm font-semibold text-on-surface"
                        title={course.title}
                      >
                        {course.title}
                      </button>
                      <span
                        className={`shrink-0 rounded-full px-2.5 py-0.5 text-[11px] font-medium ${courseType.cls}`}
                      >
                        {course.courseType || courseType.label}
                      </span>
                      {course.progress && (
                        <span
                          className={`shrink-0 rounded-full px-2.5 py-0.5 text-[11px] font-medium tnum ${
                            isDone
                              ? 'bg-success-container text-on-success-container'
                              : 'bg-surface-container-highest text-on-surface-variant'
                          }`}
                        >
                          {isDone ? '已完成' : `进度 ${course.progress}`}
                        </span>
                      )}
                      <span className="text-[11px] text-on-surface-variant/75 tnum">
                        #{realIndex + 1}
                      </span>
                    </div>

                    {course.teacher || course.school ? (
                      <div className="mt-1 flex flex-wrap items-center gap-3 text-xs text-on-surface-variant">
                        {course.teacher && (
                          <span className="inline-flex items-center gap-1">
                            <User className="h-3 w-3" aria-hidden="true" />
                            {course.teacher}
                          </span>
                        )}
                        {course.school && (
                          <span className="inline-flex items-center gap-1">
                            <GraduationCap className="h-3 w-3" aria-hidden="true" />
                            {course.school}
                          </span>
                        )}
                      </div>
                    ) : (
                      <p
                        className="mt-1 truncate font-mono text-[11px] text-on-surface-variant"
                        title={course.url}
                      >
                        {course.url}
                      </p>
                    )}
                  </div>

                  <div className="flex shrink-0 items-center gap-0.5 opacity-75 transition-opacity group-hover:opacity-100">
                    <button
                      type="button"
                      onClick={() => onCheckCourse(course.url)}
                      disabled={taskRunning}
                      title="只读检测此课程目录结构"
                      className="flex h-8 w-8 items-center justify-center rounded-full text-on-surface-variant transition-colors hover:bg-on-surface/10 hover:text-primary disabled:opacity-35"
                    >
                      <Stethoscope className="h-3.5 w-3.5" />
                    </button>
                    <button
                      type="button"
                      onClick={() => moveCourse(realIndex, -1)}
                      disabled={realIndex === 0}
                      title="上移顺序"
                      className="flex h-8 w-8 items-center justify-center rounded-full text-on-surface-variant transition-colors hover:bg-on-surface/10 hover:text-on-surface disabled:opacity-25"
                    >
                      <ArrowUp className="h-3.5 w-3.5" />
                    </button>
                    <button
                      type="button"
                      onClick={() => moveCourse(realIndex, 1)}
                      disabled={realIndex === courses.length - 1}
                      title="下移顺序"
                      className="flex h-8 w-8 items-center justify-center rounded-full text-on-surface-variant transition-colors hover:bg-on-surface/10 hover:text-on-surface disabled:opacity-25"
                    >
                      <ArrowDown className="h-3.5 w-3.5" />
                    </button>
                    <button
                      type="button"
                      onClick={() => removeCourse(course.id)}
                      title="从队列移除"
                      className="flex h-8 w-8 items-center justify-center rounded-full text-on-surface-variant transition-colors hover:bg-error-container hover:text-on-error-container"
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  </div>
                </div>
              </li>
            );
          })}
          {filteredCourses.length === 0 && (
            <li className="py-12 text-center text-[13px] text-on-surface-variant">
              未找到与「{query}」匹配的课程
            </li>
          )}
        </ul>
      )}

      <div className="bg-surface-container/75 px-5 py-3">
        <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-on-surface-variant">
          <span>按队列顺序自动学习已勾选课程</span>
          <button
            type="button"
            onClick={() => setShowManualAdd((v) => !v)}
            className="inline-flex items-center gap-1 rounded-full px-2.5 py-1 font-medium text-primary hover:bg-primary/10"
          >
            <Plus className="h-3.5 w-3.5" />
            {showManualAdd ? '收起手动补充' : '手动补充链接'}
          </button>
        </div>
        {showManualAdd && (
          <ManualCourseAddForm courses={courses} onAppendCourses={appendCourses} />
        )}
      </div>
    </section>
  );
}
