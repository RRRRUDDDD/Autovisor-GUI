import { useState } from 'react';
import { Link2 } from 'lucide-react';
import Button from '../ui/Button';
import Input from '../ui/Input';
import {
  detectCourseType,
  extractCourseUrlsFromText,
  inferCourseTitle,
  isValidCourseUrl,
} from '../../lib/configModel';

export default function ManualCourseAddForm({ courses, onAppendCourses }) {
  const [newUrl, setNewUrl] = useState('');
  const [newTitle, setNewTitle] = useState('');
  const [urlError, setUrlError] = useState('');
  const [manualFeedback, setManualFeedback] = useState('');

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
      onAppendCourses(newItems);
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
    onAppendCourses([nextItem]);
    setNewUrl('');
    setNewTitle('');
    setUrlError('');
    setManualFeedback('已添加 1 门课程');
  };

  return (
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
          placeholder={'粘贴单条或多行 https:// 链接（课程 / 知到作业 / 正式考试 / AI 学习页均可）\n支持直接粘贴 config.ini 的 URL1 = https://... 块'}
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
      <p className="text-[11px] text-on-surface-variant">
        支持课程播放页、知到作业 / 正式考试直达链接，以及 learnPage / knowledgeStudy / testDetail 学习页
      </p>
      {manualFeedback && !urlError && (
        <p role="status" className="text-xs font-medium text-success">
          {manualFeedback}
        </p>
      )}
    </form>
  );
}
