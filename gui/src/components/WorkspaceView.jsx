import { useMemo, useState, useCallback, useEffect } from 'react';
import {
  BookOpen,
  SlidersHorizontal,
  Play,
  Loader2,
  Check,
  Search,
  Plus,
  Trash2,
  ArrowUp,
  ArrowDown,
  AlertCircle,
  Stethoscope,
  Link2,
  CheckCircle2,
  RefreshCw,
  User,
  GraduationCap,
} from 'lucide-react';
import Button from './ui/Button';
import Input from './ui/Input';
import Label from './ui/Label';
import NumberInput from './ui/NumberInput';
import Switch from './ui/Switch';
import {
  detectCourseType,
  extractCourseUrlsFromText,
  inferCourseTitle,
  isValidCourseUrl,
  isValidLlmApiBase,
  isValidProxyUrl,
} from '../lib/configModel';

const SPEED_PRESETS = [1.0, 1.2, 1.5, 1.8];
const MASTERY_PRESETS = [80, 90, 100];

const TIME_PRESETS = [
  { label: '不限', value: 0 },
  { label: '25分', value: 25 },
  { label: '30分', value: 30 },
  { label: '45分', value: 45 },
  { label: '60分', value: 60 },
];

const WorkspaceView = ({
  config,
  onChangeConfig,
  runtime,
  dirty,
  saving,
  saveStatus,
  refreshingCourses = false,
  courseSyncNotice = null,
  starting,
  startError,
  runState,
  onRefreshCourses,
  onRetrySave,
  onStartStudy,
  onReturnToProgress,
  onCheckCourse,
}) => {
  const [query, setQuery] = useState('');
  const [showManualAdd, setShowManualAdd] = useState(false);
  const [newUrl, setNewUrl] = useState('');
  const [newTitle, setNewTitle] = useState('');
  const [urlError, setUrlError] = useState('');
  const [manualFeedback, setManualFeedback] = useState('');
  const [invalidFields, setInvalidFields] = useState({});

  const setFieldValid = useCallback((field, valid) => {
    setInvalidFields((current) => {
      if (Boolean(current[field]) === !valid) return current;
      const next = { ...current };
      if (valid) delete next[field];
      else next[field] = true;
      return next;
    });
  }, []);

  const courses = config.courses || [];
  const enabledCount = useMemo(() => courses.filter((c) => c.enabled).length, [courses]);
  const taskRunning = runState?.status === 'running';
  const hasRunHistory = runState && runState.status !== 'idle';
  const proxyValid = isValidProxyUrl(config.proxy);
  const llmBaseValid = isValidLlmApiBase(config.llm?.apiBase);
  const showMasteryTarget = config.enableMastery !== false || config.enableExamApi !== false;

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

  const updateField = (patch) => {
    onChangeConfig((prev) => ({ ...prev, ...patch }));
  };

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

  const isMultiLineInput = /\r|\n/.test(newUrl.trim());

  const handleAddManual = (e) => {
    e.preventDefault();
    setManualFeedback('');
    const rawText = newUrl.trim();
    if (!rawText) {
      setUrlError('请输入智慧树课程播放页链接（支持单条或多行批量粘贴）');
      return;
    }

    if (isMultiLineInput) {
      const { valid, invalid } = extractCourseUrlsFromText(rawText);
      const existing = new Set(courses.map((c) => c.url));
      const newItems = [];
      let dupCount = 0;
      for (const url of valid) {
        if (existing.has(url)) {
          dupCount += 1;
          continue;
        }
        existing.add(url);
        const nextIndex = courses.length + newItems.length + 1;
        newItems.push({
          id: `manual-${Date.now()}-${newItems.length}-${Math.random().toString(36).slice(2, 6)}`,
          title: inferCourseTitle(url, nextIndex),
          url,
          enabled: true,
          teacher: '',
          school: '',
          progress: '',
          courseType: detectCourseType(url).label,
          autoDetected: false,
        });
      }
      if (newItems.length === 0) {
        const reasons = [];
        if (dupCount > 0) reasons.push(`${dupCount} 条重复`);
        if (invalid.length > 0) reasons.push(`${invalid.length} 条无效`);
        setUrlError(
          reasons.length > 0
            ? `未添加任何课程（${reasons.join('、')}）`
            : '未解析到有效的 https:// 课程链接'
        );
        return;
      }
      onChangeConfig((prev) => ({ ...prev, courses: [...prev.courses, ...newItems] }));
      const skips = [];
      if (dupCount > 0) skips.push(`${dupCount} 条重复`);
      if (invalid.length > 0) skips.push(`${invalid.length} 条无效`);
      setManualFeedback(
        `已添加 ${newItems.length} 门课程${skips.length > 0 ? `（跳过 ${skips.join('、')}）` : ''}`
      );
      setNewUrl('');
      setNewTitle('');
      setUrlError('');
      return;
    }

    const cleaned = rawText.replace(/^[A-Za-z0-9_]+\s*=\s*/, '').trim();
    if (!cleaned) {
      setUrlError('请输入智慧树课程播放页链接（以 https:// 开头）');
      return;
    }
    if (!isValidCourseUrl(cleaned)) {
      setUrlError('链接格式无效，请输入完整的 https:// 播放页地址');
      return;
    }
    if (courses.some((c) => c.url === cleaned)) {
      setUrlError('该课程已在队列中');
      return;
    }
    const nextItem = {
      id: `manual-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
      title: newTitle.trim() || inferCourseTitle(cleaned, courses.length + 1),
      url: cleaned,
      enabled: true,
      teacher: '',
      school: '',
      progress: '',
      courseType: detectCourseType(cleaned).label,
      autoDetected: false,
    };
    onChangeConfig((prev) => ({ ...prev, courses: [...prev.courses, nextItem] }));
    setNewUrl('');
    setNewTitle('');
    setUrlError('');
    setManualFeedback('已添加 1 门课程');
  };

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

  const canStart =
    enabledCount > 0 &&
    proxyValid &&
    llmBaseValid &&
    Object.keys(invalidFields).length === 0 &&
    !starting &&
    !taskRunning &&
    !refreshingCourses;

  return (
    <div className="m3-workspace-grid grid grid-cols-1 gap-5 md:grid-cols-[minmax(0,1fr)_clamp(20.5rem,32vw,26rem)]">
      {/* 左栏：课程队列（M3 Tonal Container） */}
      <section
        aria-label="课程列表"
        className="flex min-h-0 flex-col overflow-hidden rounded-[24px] bg-surface-container-low animate-stagger-up"
      >
        {/* 队列头部工具栏 */}
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

        {/* 同步状态提醒条 */}
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

        {/* 课程列表主体 */}
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
              const realIndex = courses.findIndex((c) => c.id === course.id);
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
                    {/* M3 Checkbox */}
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

                    {/* 快捷行操作 */}
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

        {/* 底部辅助条：手动补充链接 */}
        <div className="bg-surface-container/75 px-5 py-3">
          <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-on-surface-variant">
            <span>按队列顺序自动学习已勾选课程</span>
            <button
              type="button"
              onClick={() => {
                setShowManualAdd((v) => !v);
                setUrlError('');
                setManualFeedback('');
              }}
              className="inline-flex items-center gap-1 rounded-full px-2.5 py-1 font-medium text-primary hover:bg-primary/10"
            >
              <Plus className="h-3.5 w-3.5" />
              {showManualAdd ? '收起手动补充' : '手动补充链接'}
            </button>
          </div>
          {showManualAdd && (
            <form onSubmit={handleAddManual} className="mt-3 space-y-2 animate-fade-in">
              <div className="relative">
                <Link2
                  className="pointer-events-none absolute left-3 top-2.5 h-3.5 w-3.5 text-on-surface-variant"
                  aria-hidden="true"
                />
                <textarea
                  rows={isMultiLineInput ? 3 : 2}
                  value={newUrl}
                  onChange={(e) => {
                    setNewUrl(e.target.value);
                    if (urlError) setUrlError('');
                    if (manualFeedback) setManualFeedback('');
                  }}
                  placeholder={'粘贴单条或多行课程播放页 https:// 链接\n支持直接粘贴 config.ini 的 URL1 = https://... 块'}
                  aria-label="补充课程链接"
                  className="w-full resize-y rounded-xl border border-outline/60 bg-surface-container-lowest py-2 pl-8 pr-3 font-mono text-xs text-on-surface placeholder:text-on-surface-variant/75 focus:border-primary focus:outline-none focus:ring-2 focus:ring-primary/30"
                />
              </div>
              <div className="flex flex-wrap items-center gap-2">
                {!isMultiLineInput && (
                  <Input
                    value={newTitle}
                    onChange={(e) => setNewTitle(e.target.value)}
                    placeholder="课程名（单条添加时可选）"
                    className="h-9 min-w-[140px] flex-1 text-xs"
                    aria-label="补充课程备注名"
                  />
                )}
                <Button type="submit" size="sm" className="h-9 shrink-0">
                  {isMultiLineInput ? '批量添加' : '添加'}
                </Button>
                {isMultiLineInput && (
                  <span className="text-[11px] text-on-surface-variant">
                    检测到多行内容，将自动去重并忽略无效行
                  </span>
                )}
              </div>
              {urlError && (
                <p role="alert" className="text-xs text-error">
                  {urlError}
                </p>
              )}
              {manualFeedback && !urlError && (
                <p role="status" className="text-xs font-medium text-success">
                  {manualFeedback}
                </p>
              )}
            </form>
          )}
        </div>
      </section>

      {/* 右栏：高频学习参数 + 启动操作台 */}
      <aside
        aria-label="任务配置"
        className="flex min-h-0 flex-col gap-3 animate-stagger-up"
        style={{ animationDelay: '60ms' }}
      >
        {/* 高频刷课参数卡片 */}
        <div className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-[24px] bg-surface-container-low">
          <div className="flex shrink-0 items-center justify-between gap-2 px-5 pt-4 pb-2.5">
            <div className="flex items-center gap-2">
              <SlidersHorizontal className="h-4.5 w-4.5 text-primary" aria-hidden="true" />
              <h2 className="text-[15px] font-semibold text-on-surface">学习参数</h2>
            </div>
            {runtime?.cookieExists && (
              <span className="inline-flex items-center gap-1 rounded-full bg-success-container px-2.5 py-0.5 text-[11px] font-medium text-on-success-container">
                <CheckCircle2 className="h-3 w-3" />
                已登录免密
              </span>
            )}
          </div>

          <div
            className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-5 pb-4"
            role="region"
            tabIndex={0}
            aria-label="任务参数"
          >
            <div className="space-y-3.5 pt-0.5">
              {/* 播放倍速 */}
              <div className="space-y-2 rounded-2xl bg-surface-container px-3.5 py-3">
                <div className="flex items-center justify-between">
                  <Label htmlFor="limitSpeed" className="text-on-surface">
                    播放倍速
                  </Label>
                  <span className="rounded-full bg-primary-container px-2.5 py-0.5 font-mono text-xs font-semibold text-on-primary-container tnum">
                    {Number(config.limitSpeed).toFixed(1)}x
                  </span>
                </div>
                <input
                  id="limitSpeed"
                  type="range"
                  min="0.5"
                  max="1.8"
                  step="0.1"
                  value={config.limitSpeed}
                  onChange={(e) => updateField({ limitSpeed: parseFloat(e.target.value) })}
                  className="m3-slider w-full cursor-pointer"
                />
                <div className="flex flex-wrap items-center justify-between gap-1.5">
                  <div className="flex flex-wrap gap-1.5">
                    {SPEED_PRESETS.map((spd) => {
                      const active = Math.abs(Number(config.limitSpeed) - spd) < 0.01;
                      return (
                        <button
                          key={spd}
                          type="button"
                          onClick={() => updateField({ limitSpeed: spd })}
                          className={`rounded-full px-2.5 py-0.5 font-mono text-[11px] font-medium transition-colors tnum ${
                            active
                              ? 'bg-secondary-container text-on-secondary-container'
                              : 'bg-surface-container-highest/70 text-on-surface-variant hover:bg-surface-container-highest'
                          }`}
                        >
                          {spd.toFixed(1)}x
                        </button>
                      );
                    })}
                  </div>
                  <span className="text-[11px] text-on-surface-variant tnum">上限 1.8x</span>
                </div>
              </div>

              {/* 单门课程时长限制 */}
              <div className="space-y-1.5">
                <div className="flex items-center justify-between">
                  <Label htmlFor="limitMaxTime">单门课程时长限制（分钟）</Label>
                  <span className="text-xs text-on-surface-variant">0 = 不限制</span>
                </div>
                <NumberInput
                  id="limitMaxTime"
                  min={0}
                  max={1440}
                  step="5"
                  value={config.limitMaxTime}
                  onValueChange={(limitMaxTime) => updateField({ limitMaxTime })}
                  onValidityChange={(valid) => setFieldValid('limitMaxTime', valid)}
                  className="h-9"
                />
                <div className="flex flex-wrap gap-1.5 pt-0.5">
                  {TIME_PRESETS.map((preset) => {
                    const active = Number(config.limitMaxTime) === preset.value;
                    return (
                      <button
                        key={preset.value}
                        type="button"
                        onClick={() => updateField({ limitMaxTime: preset.value })}
                        className={`inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-medium transition-colors ease-m3 ${
                          active
                            ? 'bg-secondary-container text-on-secondary-container'
                            : 'border border-outline/50 bg-transparent text-on-surface-variant hover:bg-on-surface/[0.06]'
                        }`}
                      >
                        {active && <Check className="h-3 w-3" strokeWidth={2.8} />}
                        {preset.label}
                      </button>
                    );
                  })}
                </div>
              </div>

              {/* 学习模式 */}
              <div className="space-y-0 border-t border-outline-variant/60 pt-2">
                <Switch
                  id="reviewMode"
                  checked={config.reviewMode === true}
                  onChange={(reviewMode) => updateField({ reviewMode })}
                  label="复习模式（刷习惯分）"
                  description="忽略已完成状态重播视频；设有时长上限时循环刷满每日习惯分"
                />
                {config.reviewMode === true && (
                  <div className="mb-2 mt-1 flex flex-wrap items-center justify-between gap-2 rounded-2xl bg-tertiary-container/60 px-3.5 py-2.5 text-xs text-on-tertiary-container animate-fade-in">
                    <span className="leading-relaxed">
                      {Number(config.limitMaxTime) > 0
                        ? `将循环重播视频直至每门课满 ${config.limitMaxTime} 分钟，并跳过文档与刷题阶段`
                        : '当前时长限制为「不限」，将完整重播一轮后结束；刷习惯分建议设为 25–30 分钟'}
                    </span>
                    {Number(config.limitMaxTime) === 0 && (
                      <button
                        type="button"
                        onClick={() => updateField({ limitMaxTime: 30 })}
                        className="shrink-0 rounded-full bg-tertiary px-2.5 py-1 text-[11px] font-medium text-on-tertiary transition-opacity hover:opacity-90"
                      >
                        设为 30 分钟
                      </button>
                    )}
                  </div>
                )}
              </div>

              {/* 挂机与运行保障 */}
              <div className="space-y-0 border-t border-outline-variant/60 pt-2">
                <Switch
                  id="enableAutoCaptcha"
                  checked={config.enableAutoCaptcha}
                  onChange={(enableAutoCaptcha) => updateField({ enableAutoCaptcha })}
                  label="自动跳过验证码"
                  description="自动处理课中滑块与点选验证"
                />
                <Switch
                  id="soundOff"
                  checked={config.soundOff}
                  onChange={(soundOff) => updateField({ soundOff })}
                  label="静音播放"
                  description="自动将视频音量设为 0，避免后台挂机出声"
                />
                <Switch
                  id="enableHideWindow"
                  checked={config.enableHideWindow !== false}
                  onChange={(enableHideWindow) => updateField({ enableHideWindow })}
                  label="隐藏浏览器窗口"
                  description="移出屏幕后台学习（遇安全验证会自动前置）"
                />
              </div>

              {/* 答题与 AI 智慧课 */}
              <div className="space-y-0 border-t border-outline-variant/60 pt-2">
                <Switch
                  id="enableZhidaoQuiz"
                  checked={config.enableZhidaoQuiz !== false}
                  onChange={(enableZhidaoQuiz) => updateField({ enableZhidaoQuiz })}
                  label="课中弹题优先用智慧树 AI"
                  description="自动答题时先试智慧树 AI，失败回退所配置的大模型"
                />
                <Switch
                  id="enableMastery"
                  checked={config.enableMastery !== false}
                  onChange={(enableMastery) => {
                    updateField({ enableMastery });
                    if (!enableMastery && config.enableExamApi === false) {
                      setFieldValid('masteryTarget', true);
                    }
                  }}
                  label="AI 智慧课自动刷掌握度"
                  description="视频刷完后自动刷知识点掌握度题目（仅对 AI 智慧课生效）"
                />
                <Switch
                  id="enableExamApi"
                  checked={config.enableExamApi !== false}
                  onChange={(enableExamApi) => {
                    updateField({ enableExamApi });
                    if (!enableExamApi && config.enableMastery === false) {
                      setFieldValid('masteryTarget', true);
                    }
                  }}
                  label="接口版自动完成计分考试"
                  description="视频刷完后通过接口作答并交卷，成绩达标即跳过该知识点（仅对 AI 智慧课生效）"
                />
                {showMasteryTarget && (
                  <div className="mb-1.5 mt-1.5 space-y-1.5 rounded-2xl bg-surface-container px-3.5 py-3 animate-fade-in">
                    <div className="flex items-center justify-between">
                      <Label htmlFor="masteryTarget" className="text-on-surface">
                        掌握度目标（%）
                      </Label>
                      <span className="rounded-full bg-tertiary-container px-2.5 py-0.5 font-mono text-xs font-semibold text-on-tertiary-container tnum">
                        {Math.round(Number(config.masteryTarget) || 100)}%
                      </span>
                    </div>
                    <p className="text-[11px] leading-relaxed text-on-surface-variant">
                      同时用于接口版计分考试与知识点掌握度刷题
                    </p>
                    <NumberInput
                      id="masteryTarget"
                      min={1}
                      max={100}
                      step="5"
                      value={config.masteryTarget ?? 100}
                      onValueChange={(masteryTarget) => updateField({ masteryTarget })}
                      onValidityChange={(valid) => setFieldValid('masteryTarget', valid)}
                      className="h-9"
                    />
                    <div className="flex flex-wrap gap-1.5 pt-0.5">
                      {MASTERY_PRESETS.map((pct) => {
                        const active = Math.round(Number(config.masteryTarget) || 100) === pct;
                        return (
                          <button
                            key={pct}
                            type="button"
                            onClick={() => {
                              updateField({ masteryTarget: pct });
                              setFieldValid('masteryTarget', true);
                            }}
                            className={`inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 font-mono text-xs font-medium transition-colors ease-m3 tnum ${
                              active
                                ? 'bg-secondary-container text-on-secondary-container'
                                : 'border border-outline/50 bg-transparent text-on-surface-variant hover:bg-on-surface/[0.06]'
                            }`}
                          >
                            {active && <Check className="h-3 w-3" strokeWidth={2.8} />}
                            {pct}%
                          </button>
                        );
                      })}
                    </div>
                  </div>
                )}
                <Switch
                  id="enableReviewExamApi"
                  checked={config.enableReviewExamApi === true}
                  onChange={(enableReviewExamApi) => updateField({ enableReviewExamApi })}
                  label="复习 / 模拟考试接口作答"
                  description="捕获新打开的复习、模拟考试页并直接作答交卷（默认关闭，正式考试请勿开启）"
                />
              </div>
            </div>
          </div>
        </div>

        {/* 底部主操作区（M3 Primary Tonal Surface） */}
        <div className="shrink-0 space-y-2 rounded-[24px] bg-primary-container/50 p-3.5">
          {taskRunning ? (
            <Button className="h-11 w-full" onClick={onReturnToProgress}>
              <Loader2 className="h-4.5 w-4.5 animate-spin" aria-hidden="true" />
              返回运行中的学习任务
            </Button>
          ) : (
            <Button className="h-11 w-full" onClick={onStartStudy} disabled={!canStart}>
              {starting ? (
                <>
                  <Loader2 className="h-4.5 w-4.5 animate-spin" aria-hidden="true" />
                  {config.reviewMode ? '正在启动复习任务' : '正在启动学习任务'}
                </>
              ) : (
                <>
                  <Play className="h-4.5 w-4.5" aria-hidden="true" />
                  {config.reviewMode ? '开始复习刷分' : '开始刷课'}
                </>
              )}
            </Button>
          )}

          <p className="text-center text-xs text-on-primary-container/85">
            {!proxyValid
              ? '代理服务器地址格式无效，请在「系统设置」中修正'
              : !llmBaseValid
                ? '大模型 API Base 地址格式无效，请在「系统设置」中修正'
                : taskRunning
                  ? '当前有任务在后台运行，可随时查看实时监控'
                  : enabledCount
                    ? `将按顺序${config.reviewMode ? '复习' : '学习'} ${enabledCount} 门课程 · ${Number(config.limitSpeed).toFixed(1)}x${config.limitMaxTime > 0 ? ` · 每课 ${config.limitMaxTime} 分钟` : ' · 不限时'}${config.shutdownOnComplete ? ' · 完成后自动关机' : ''}`
                    : '请至少勾选一门课程'}
          </p>

          {hasRunHistory && !taskRunning && (
            <Button variant="ghost" size="sm" className="w-full" onClick={onReturnToProgress}>
              查看上次运行记录
            </Button>
          )}

          {startError && (
            <div
              role="alert"
              className="flex items-start gap-2 rounded-2xl bg-error-container px-3.5 py-2.5 text-xs leading-relaxed text-on-error-container animate-fade-in"
            >
              <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
              <span>{startError}</span>
            </div>
          )}

          {saveStatus?.type === 'error' ? (
            <div
              role="alert"
              className="flex items-center justify-between gap-2 rounded-2xl bg-error-container px-3.5 py-2 text-xs text-on-error-container animate-fade-in"
            >
              <span className="flex min-w-0 items-center gap-1.5 font-medium">
                <AlertCircle className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                <span className="truncate">{saveStatus.message}</span>
              </span>
              {onRetrySave && (
                <button
                  type="button"
                  onClick={onRetrySave}
                  disabled={saving || !proxyValid || !llmBaseValid}
                  className="shrink-0 rounded-full bg-on-error-container/10 px-2.5 py-0.5 font-medium hover:bg-on-error-container/15 disabled:opacity-50"
                >
                  重试
                </button>
              )}
            </div>
          ) : saving || (dirty && proxyValid && llmBaseValid) ? (
            <p
              role="status"
              className="flex items-center justify-center gap-1.5 text-xs font-medium text-on-primary-container/80"
            >
              <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" />
              正在自动保存配置...
            </p>
          ) : saveStatus?.type === 'success' ? (
            <p
              role="status"
              className="flex items-center justify-center gap-1.5 text-xs font-medium text-success animate-fade-in"
            >
              <CheckCircle2 className="h-3.5 w-3.5" aria-hidden="true" />
              {saveStatus.message}
            </p>
          ) : null}
        </div>
      </aside>
    </div>
  );
};

export default WorkspaceView;
