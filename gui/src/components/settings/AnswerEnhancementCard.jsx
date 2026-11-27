import { useState } from 'react';
import { Check, Download, Loader2, PlugZap, RefreshCw, Sparkles, Trash2 } from 'lucide-react';
import Button from '../ui/Button';
import Input from '../ui/Input';
import Label from '../ui/Label';
import NumberInput from '../ui/NumberInput';
import Switch from '../ui/Switch';
import {
  DEFAULT_QUESTION_BANK_SCOPES,
  QUESTION_BANK_SCOPES,
  normalizeQuestionBankScopes,
} from '../../lib/configModel';

const MASTERY_PRESETS = [80, 90, 100];

export default function AnswerEnhancementCard({
  config,
  llm,
  taskRunning,
  updateField,
  updateLlmField,
  onNumberValidity,
  onTestQuestionBank,
  onExportQuestionCache,
  onImportQuestionCache,
  onClearQuestionCache,
}) {
  const [testingBank, setTestingBank] = useState(false);
  const [bankResult, setBankResult] = useState(null);
  const [cacheBusy, setCacheBusy] = useState(false);
  const [confirmClearCache, setConfirmClearCache] = useState(false);

  const showMasteryTarget = config.enableMastery !== false || config.enableExamApi !== false;
  const activeBankScopes = normalizeQuestionBankScopes(
    llm.questionBankScopes ?? DEFAULT_QUESTION_BANK_SCOPES
  );

  const toggleBankScope = (scopeValue) => {
    const next = activeBankScopes.includes(scopeValue)
      ? activeBankScopes.filter((item) => item !== scopeValue)
      : normalizeQuestionBankScopes([...activeBankScopes, scopeValue]);
    updateLlmField({ questionBankScopes: next });
  };

  const handleTestQuestionBank = async () => {
    if (!onTestQuestionBank) return;
    setTestingBank(true);
    setConfirmClearCache(false);
    setBankResult(null);
    try {
      const message = await onTestQuestionBank({
        token: llm.questionBankToken || '',
        url: llm.questionBankUrl || '',
        proxy: config.proxy || '',
      });
      setBankResult({ type: 'success', message: message || '题库连接正常' });
    } catch (err) {
      setBankResult({
        type: 'error',
        message: err?.message || String(err) || '题库连通性测试失败',
      });
    } finally {
      setTestingBank(false);
    }
  };

  const handleExportCache = async () => {
    if (!onExportQuestionCache) return;
    setCacheBusy(true);
    setConfirmClearCache(false);
    setBankResult(null);
    try {
      const path = await onExportQuestionCache();
      setBankResult(
        path
          ? { type: 'success', message: `题库缓存已导出到 ${path}` }
          : { type: 'info', message: '已取消导出' }
      );
    } catch (err) {
      setBankResult({
        type: 'error',
        message: err?.message || String(err) || '导出题库缓存失败',
      });
    } finally {
      setCacheBusy(false);
    }
  };

  const handleImportCache = async () => {
    if (!onImportQuestionCache) return;
    setCacheBusy(true);
    setConfirmClearCache(false);
    setBankResult(null);
    try {
      const result = await onImportQuestionCache();
      if (!result) {
        setBankResult({ type: 'info', message: '已取消导入' });
      } else {
        const zhidao = result.zhidao_questions ?? result.zhidaoQuestions;
        const ai = result.ai_questions ?? result.aiQuestions;
        const parts = [
          zhidao != null ? `知到题 ${zhidao} 条` : '',
          ai != null ? `AI 题 ${ai} 条` : '',
        ].filter(Boolean);
        setBankResult({
          type: 'success',
          message: parts.length ? `题库缓存导入完成：${parts.join('，')}` : '题库缓存导入完成',
        });
      }
    } catch (err) {
      setBankResult({
        type: 'error',
        message: err?.message || String(err) || '导入题库缓存失败',
      });
    } finally {
      setCacheBusy(false);
    }
  };

  const handleClearCache = async () => {
    if (!onClearQuestionCache) return;
    setCacheBusy(true);
    setConfirmClearCache(false);
    setBankResult(null);
    try {
      const result = await onClearQuestionCache();
      const zhidao = Number(result?.zhidao_questions || 0);
      const ai = Number(result?.ai_questions || 0);
      const jsonFiles = Number(result?.json_files || 0);
      const parts = [
        zhidao > 0 ? `知到题 ${zhidao} 条` : '',
        ai > 0 ? `AI 题 ${ai} 条` : '',
        jsonFiles > 0 ? `快照文件 ${jsonFiles} 个` : '',
      ].filter(Boolean);
      setBankResult(
        parts.length
          ? { type: 'success', message: `题库缓存已清理：${parts.join('，')}` }
          : { type: 'info', message: '题库缓存已为空，无需清理' }
      );
    } catch (err) {
      setBankResult({
        type: 'error',
        message: err?.message || String(err) || '清理题库缓存失败',
      });
    } finally {
      setCacheBusy(false);
    }
  };

  return (
    <section className="flex flex-col rounded-[24px] bg-surface-container-low p-6 lg:col-span-2">
      <div className="mb-5 flex items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl bg-tertiary-container text-on-tertiary-container">
            <Sparkles className="h-5 w-5" aria-hidden="true" />
          </div>
          <div>
            <h2 className="text-base font-semibold text-on-surface">答题增强与题库</h2>
            <p className="text-xs text-on-surface-variant">
              知到作业/考试接口、AI 智慧课答题策略、课中弹题、PPT 参考与第三方题库（写入 config.ini 与 data/llm.json）
            </p>
          </div>
        </div>
      </div>

      <div className="space-y-5">
        <div className="space-y-3">
          <h3 className="text-xs font-semibold uppercase tracking-wide text-on-surface-variant">
            知到作业 / 考试与作答策略
          </h3>
          <div className="space-y-1">
            <Switch
              id="enableHomeworkApi"
              checked={config.enableHomeworkApi !== false}
              onChange={(enableHomeworkApi) => updateField({ enableHomeworkApi })}
              label="知到作业优先走加密 API"
              description="关闭后仅使用视觉模型作答；开启时图片题仍会自动回退视觉模型"
            />
            <Switch
              id="enableFormalExamApi"
              checked={config.enableFormalExamApi !== false}
              onChange={(enableFormalExamApi) => updateField({ enableFormalExamApi })}
              label="接管知到正式考试接口"
              description="交卷仍受大模型设置中的「测验 / 作业自动提交」控制"
            />
            <Switch
              id="forceRedoAnswered"
              checked={config.forceRedoAnswered === true}
              onChange={(forceRedoAnswered) => updateField({ forceRedoAnswered })}
              label="已作答保存的题目仍重新作答"
              description="开启后，答题时即使题目此前已作答并保存（暂存），也会重新识别并覆盖作答"
            />
          </div>

          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label htmlFor="homeworkThreshold">作业达标得分率（%）</Label>
              <NumberInput
                id="homeworkThreshold"
                min={0}
                max={100}
                integer
                value={config.homeworkThreshold ?? 100}
                onValueChange={(homeworkThreshold) => updateField({ homeworkThreshold })}
                onValidityChange={(valid) => onNumberValidity('homeworkThreshold', valid)}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="homeworkMaxRedo">作业错题重做轮数</Label>
              <NumberInput
                id="homeworkMaxRedo"
                min={0}
                integer
                value={config.homeworkMaxRedo ?? 3}
                onValueChange={(homeworkMaxRedo) => updateField({ homeworkMaxRedo })}
                onValidityChange={(valid) => onNumberValidity('homeworkMaxRedo', valid)}
              />
            </div>
          </div>
        </div>

        <div className="space-y-3 border-t border-outline-variant/60 pt-4">
          <h3 className="text-xs font-semibold uppercase tracking-wide text-on-surface-variant">
            课中弹题与 AI 智慧课
          </h3>
          <div className="space-y-1">
            <Switch
              id="enablePopupIntercept"
              checked={config.enablePopupIntercept !== false}
              onChange={(enablePopupIntercept) => updateField({ enablePopupIntercept })}
              label="课中弹题接口拦截"
              description="只读监听官方 AI 弹题响应，未命中时静默回退大模型"
            />
            <Switch
              id="enableZhidaoQuiz"
              checked={config.enableZhidaoQuiz !== false}
              onChange={(enableZhidaoQuiz) => updateField({ enableZhidaoQuiz })}
              label="课中弹题优先用智慧树 AI"
              description="自动答题时先试智慧树 AI，失败回退所配置的大模型"
            />
            <Switch
              id="enablePptReference"
              checked={config.enablePptReference !== false}
              onChange={(enablePptReference) => updateField({ enablePptReference })}
              label="PPT 课件辅助答题"
              description="自动提取 AI 智慧课知识点的 PPT 文本并注入答题参考"
            />
            <Switch
              id="enableMastery"
              checked={config.enableMastery !== false}
              onChange={(enableMastery) => updateField({ enableMastery })}
              label="AI 智慧课自动刷掌握度"
              description="视频刷完后自动刷知识点掌握度题目（仅对 AI 智慧课生效）"
            />
            <Switch
              id="skipAiFreeExam"
              checked={config.skipAiFreeExam !== false}
              onChange={(skipAiFreeExam) => updateField({ skipAiFreeExam })}
              label="AI 智慧课跳过免考知识点考核"
              description="仅在掌握度与接口考核阶段跳过免考节点，不影响必学视频与文档资源补齐"
            />
            <Switch
              id="enableExamApi"
              checked={config.enableExamApi !== false}
              onChange={(enableExamApi) => updateField({ enableExamApi })}
              label="接口版自动完成计分考试"
              description="视频刷完后通过接口作答并交卷，成绩达标即跳过该知识点（仅对 AI 智慧课生效）"
            />
            {showMasteryTarget && (
              <div className="my-2 space-y-2 rounded-2xl bg-surface-container px-4 py-3.5 animate-fade-in">
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
                <div className="flex flex-wrap items-center gap-3">
                  <NumberInput
                    id="masteryTarget"
                    min={1}
                    max={100}
                    step="5"
                    value={config.masteryTarget ?? 100}
                    onValueChange={(masteryTarget) => updateField({ masteryTarget })}
                    onValidityChange={(valid) => onNumberValidity('masteryTarget', valid)}
                    className="h-9 w-36"
                  />
                  <div className="flex flex-wrap gap-1.5">
                    {MASTERY_PRESETS.map((pct) => {
                      const active = Math.round(Number(config.masteryTarget) || 100) === pct;
                      return (
                        <button
                          key={pct}
                          type="button"
                          onClick={() => updateField({ masteryTarget: pct })}
                          className={`inline-flex items-center gap-1 rounded-full px-2.5 py-1 font-mono text-xs font-medium transition-colors ease-m3 tnum ${
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

        <div className="space-y-3 border-t border-outline-variant/60 pt-4">
          <h3 className="text-xs font-semibold uppercase tracking-wide text-on-surface-variant">
            第三方题库与本地缓存
          </h3>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label htmlFor="llmQuestionBankToken">第三方题库 Token（可选）</Label>
              <Input
                id="llmQuestionBankToken"
                value={llm.questionBankToken || ''}
                onChange={(e) => updateLlmField({ questionBankToken: e.target.value })}
                placeholder="留空则不启用第三方题库"
                className="font-mono text-xs"
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="llmQuestionBankUrl">题库查询地址</Label>
              <Input
                id="llmQuestionBankUrl"
                value={llm.questionBankUrl || ''}
                onChange={(e) => updateLlmField({ questionBankUrl: e.target.value })}
                placeholder="https://tk.enncy.cn/query"
                className="font-mono text-xs"
              />
            </div>
          </div>

          <div className="space-y-1.5">
            <Label>外部题库生效范围</Label>
            <p className="text-[11px] leading-relaxed text-on-surface-variant">
              默认仅在知到考试与 AI 考试启用，避免可重做的平时作业消耗付费题库次数
            </p>
            <div className="flex flex-wrap gap-1.5 pt-0.5">
              {QUESTION_BANK_SCOPES.map((scope) => {
                const active = activeBankScopes.includes(scope.value);
                return (
                  <button
                    key={scope.value}
                    type="button"
                    aria-pressed={active}
                    onClick={() => toggleBankScope(scope.value)}
                    className={`inline-flex items-center gap-1 rounded-full px-3 py-1 text-xs font-medium transition-colors ease-m3 ${
                      active
                        ? 'bg-secondary-container text-on-secondary-container'
                        : 'border border-outline/50 bg-transparent text-on-surface-variant hover:bg-on-surface/[0.06]'
                    }`}
                  >
                    {active && <Check className="h-3 w-3" strokeWidth={2.8} />}
                    {scope.label}
                  </button>
                );
              })}
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-3 pt-1">
            <Button
              type="button"
              variant="tonal"
              size="sm"
              onClick={handleTestQuestionBank}
              disabled={testingBank || !String(llm.questionBankToken || '').trim()}
            >
              {testingBank ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
              ) : (
                <PlugZap className="h-3.5 w-3.5" aria-hidden="true" />
              )}
              {testingBank ? '测试中...' : '测试题库连通性'}
            </Button>
            <Button
              type="button"
              variant="tonal"
              size="sm"
              onClick={handleExportCache}
              disabled={cacheBusy}
            >
              <Download className="h-3.5 w-3.5" aria-hidden="true" />
              导出题库缓存
            </Button>
            <Button
              type="button"
              variant="tonal"
              size="sm"
              onClick={handleImportCache}
              disabled={cacheBusy || taskRunning}
            >
              <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
              导入题库缓存
            </Button>
            {confirmClearCache ? (
              <>
                <Button
                  type="button"
                  variant="destructive"
                  size="sm"
                  onClick={handleClearCache}
                  disabled={cacheBusy || taskRunning}
                >
                  <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
                  确认清理缓存
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={() => setConfirmClearCache(false)}
                  disabled={cacheBusy}
                >
                  取消
                </Button>
              </>
            ) : (
              <Button
                type="button"
                variant="destructive-tonal"
                size="sm"
                onClick={() => {
                  setBankResult(null);
                  setConfirmClearCache(true);
                }}
                disabled={cacheBusy || taskRunning}
              >
                <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
                清理题库缓存
              </Button>
            )}
            {bankResult && (
              <p
                role={bankResult.type === 'error' ? 'alert' : 'status'}
                className={`text-xs font-medium ${
                  bankResult.type === 'error'
                    ? 'text-error'
                    : bankResult.type === 'info'
                      ? 'text-on-surface-variant'
                      : 'text-success'
                }`}
              >
                {bankResult.message}
              </p>
            )}
          </div>
        </div>
      </div>
    </section>
  );
}
