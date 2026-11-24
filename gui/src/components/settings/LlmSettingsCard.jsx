import { useMemo, useState } from 'react';
import { Bot, Eye, EyeOff, Loader2, PlugZap, Sparkles } from 'lucide-react';
import Button from '../ui/Button';
import Input from '../ui/Input';
import Label from '../ui/Label';
import NumberInput from '../ui/NumberInput';
import Select from '../ui/Select';
import Switch from '../ui/Switch';
import {
  LLM_API_FORMATS,
  LLM_PROVIDERS,
  LLM_PROVIDER_GROUPS,
  getLlmDisplayName,
  getLlmProvider,
  inferLlmProvider,
  isLlmConfigured,
  isLocalLlmEndpoint,
} from '../../lib/llmProviders';

export default function LlmSettingsCard({
  llm,
  proxy,
  proxyValid,
  llmBaseValid,
  updateLlmField,
  onNumberValidity,
  onTestLlmConnection,
  onFetchLlmModels,
}) {
  const [showLlmApiKey, setShowLlmApiKey] = useState(false);
  const [testingLlm, setTestingLlm] = useState(false);
  const [llmTestResult, setLlmTestResult] = useState(null);
  const [fetchingModels, setFetchingModels] = useState(false);
  const [fetchedModels, setFetchedModels] = useState([]);
  const [fetchModelsError, setFetchModelsError] = useState('');

  const llmProviderId = llm.provider || inferLlmProvider(llm.apiBase, llm.apiFormat);
  const llmProviderMeta = useMemo(() => getLlmProvider(llmProviderId), [llmProviderId]);
  const llmFormatMeta = useMemo(
    () => LLM_API_FORMATS.find((f) => f.id === (llm.apiFormat || 'openai')) || LLM_API_FORMATS[0],
    [llm.apiFormat]
  );
  const llmIsLocal = isLocalLlmEndpoint(llm);
  const llmConfigured = isLlmConfigured(llm);
  const visionReady = Boolean(String(llm.visionModel || '').trim());

  const llmModelChoices = useMemo(() => {
    const seen = new Set();
    const list = [];
    for (const m of [...fetchedModels, ...(llmProviderMeta?.models || [])]) {
      const cleaned = String(m || '').trim();
      if (cleaned && !seen.has(cleaned)) {
        seen.add(cleaned);
        list.push(cleaned);
      }
    }
    return list.slice(0, 24);
  }, [fetchedModels, llmProviderMeta]);

  const handleLlmPatch = (patch) => {
    setLlmTestResult(null);
    setFetchModelsError('');
    updateLlmField(patch);
  };

  const handleSelectLlmProvider = (providerId) => {
    const preset = getLlmProvider(providerId);
    setLlmTestResult(null);
    setFetchedModels([]);
    setFetchModelsError('');
    if (!preset || preset.id === 'custom') {
      handleLlmPatch({ provider: 'custom' });
      return;
    }
    const patch = {
      provider: preset.id,
      apiFormat: preset.apiFormat,
      apiVersion: preset.apiVersion || '',
    };
    if (preset.apiBase) {
      patch.apiBase = preset.apiBase;
    }
    if (preset.models?.length) {
      patch.model = preset.models[0];
    }
    handleLlmPatch(patch);
  };

  const handleTestLlmConnection = async () => {
    if (!onTestLlmConnection) return;
    setTestingLlm(true);
    setLlmTestResult(null);
    try {
      const message = await onTestLlmConnection({
        llm,
        proxy: proxy || '',
      });
      setLlmTestResult({ type: 'success', message: message || '大模型连接测试成功' });
    } catch (err) {
      setLlmTestResult({
        type: 'error',
        message: err?.message || String(err) || '大模型连接测试失败',
      });
    } finally {
      setTestingLlm(false);
    }
  };

  const handleFetchLlmModels = async () => {
    if (!onFetchLlmModels) return;
    setFetchingModels(true);
    setFetchModelsError('');
    try {
      const models = await onFetchLlmModels({
        llm,
        proxy: proxy || '',
      });
      if (Array.isArray(models) && models.length > 0) {
        setFetchedModels(models);
      } else {
        setFetchModelsError('接口未返回模型列表，请手动输入模型名称');
      }
    } catch (err) {
      setFetchModelsError(err?.message || String(err) || '拉取模型列表失败');
    } finally {
      setFetchingModels(false);
    }
  };

  return (
    <section className="flex flex-col rounded-[24px] bg-surface-container-low p-6 lg:col-span-2">
      <div className="mb-5 flex items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl bg-primary-container text-on-primary-container">
            <Bot className="h-5 w-5" aria-hidden="true" />
          </div>
          <div>
            <h2 className="text-base font-semibold text-on-surface">大模型智能答题</h2>
            <p className="text-xs text-on-surface-variant">
              支持 DeepSeek / OpenAI / Claude / Gemini / 国产大模型 / 本地 Ollama 与任意中转接口（data/llm.json）
            </p>
          </div>
        </div>
        <span
          className={`shrink-0 rounded-full px-3 py-1 text-xs font-medium ${
            llmConfigured
              ? 'bg-primary-container text-on-primary-container'
              : 'bg-surface-container-high text-on-surface-variant'
          }`}
        >
          {llmConfigured
            ? `${getLlmDisplayName(llm)} · ${llm.autoSubmit ? '自动提交' : '仅填答'}${visionReady ? ' · 图片题' : ''}`
            : '未配置'}
        </span>
      </div>

      <div className="space-y-4">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label htmlFor="llmProvider">模型服务商</Label>
            <Select
              id="llmProvider"
              value={llmProviderId}
              onChange={(e) => handleSelectLlmProvider(e.target.value)}
            >
              {LLM_PROVIDER_GROUPS.map((group) => (
                <optgroup key={group.id} label={group.label}>
                  {LLM_PROVIDERS.filter((p) => p.group === group.id).map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name}
                    </option>
                  ))}
                </optgroup>
              ))}
            </Select>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="llmApiKey">
              {llmIsLocal ? 'API Key（本地模型可留空）' : 'API Key'}
            </Label>
            <div className="relative">
              <Input
                id="llmApiKey"
                type={showLlmApiKey ? 'text' : 'password'}
                value={llm.apiKey || ''}
                onChange={(e) => handleLlmPatch({ apiKey: e.target.value })}
                placeholder={
                  llmIsLocal
                    ? '本机 Ollama / LM Studio 无需填写，若网关需鉴权可填'
                    : `留空则回退为默认跳过弹题逻辑（${llmProviderMeta?.keyPlaceholder || 'sk-...'}）`
                }
                className="pr-11 font-mono text-xs"
              />
              <button
                type="button"
                onClick={() => setShowLlmApiKey((s) => !s)}
                aria-label={showLlmApiKey ? '隐藏大模型 API Key' : '显示大模型 API Key'}
                className="absolute inset-y-0 right-0 flex w-11 items-center justify-center rounded-r-xl text-on-surface-variant hover:text-on-surface"
              >
                {showLlmApiKey ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
              </button>
            </div>
          </div>
        </div>

        <div className="space-y-1.5">
          <div className="flex items-center justify-between gap-2">
            <Label htmlFor="llmModel">
              {llm.apiFormat === 'azure' ? '模型 / 部署名称 (Deployment)' : '模型名称'}
            </Label>
            {onFetchLlmModels && llm.apiFormat !== 'azure' && (
              <button
                type="button"
                onClick={handleFetchLlmModels}
                disabled={fetchingModels || !llmBaseValid || !proxyValid || (!llm.apiKey?.trim() && !llmIsLocal)}
                className="inline-flex items-center gap-1 text-xs font-medium text-primary hover:underline disabled:pointer-events-none disabled:opacity-40"
              >
                {fetchingModels ? (
                  <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" />
                ) : (
                  <Sparkles className="h-3 w-3" aria-hidden="true" />
                )}
                {fetchingModels ? '拉取中...' : '获取模型'}
              </button>
            )}
          </div>
          <Input
            id="llmModel"
            value={llm.model || ''}
            onChange={(e) => handleLlmPatch({ model: e.target.value })}
            placeholder={llmProviderMeta?.models?.[0] || '输入任意模型 ID'}
            className="font-mono text-xs"
          />
          {fetchModelsError && (
            <p role="alert" className="text-xs text-error">
              {fetchModelsError}
            </p>
          )}
          {llmModelChoices.length > 0 && (
            <div className="flex max-h-24 flex-wrap gap-1.5 overflow-y-auto pt-0.5">
              {llmModelChoices.map((modelName) => {
                const active = (llm.model || '').trim() === modelName;
                return (
                  <button
                    key={modelName}
                    type="button"
                    onClick={() => handleLlmPatch({ model: modelName })}
                    className={`rounded-full px-2.5 py-0.5 font-mono text-[11px] font-medium transition-colors ${
                      active
                        ? 'bg-secondary-container text-on-secondary-container'
                        : 'bg-surface-container-highest/70 text-on-surface-variant hover:bg-surface-container-highest'
                    }`}
                  >
                    {modelName}
                  </button>
                );
              })}
            </div>
          )}
        </div>

        <div className="space-y-1.5">
          <Label htmlFor="llmVisionModel">视觉模型（图片题，可选）</Label>
          <Input
            id="llmVisionModel"
            value={llm.visionModel || ''}
            onChange={(e) => handleLlmPatch({ visionModel: e.target.value })}
            placeholder="如 gpt-4o、Qwen/Qwen2.5-VL-72B-Instruct、glm-4v-plus"
            className="font-mono text-xs"
          />
          <p className="text-[11px] text-on-surface-variant">
            作业/考试的题干与选项常为图片；填写支持读图的模型 ID 后会逐题截图作答，留空则跳过图片题
          </p>
        </div>

        <div className="border-t border-outline-variant/60 pt-2">
          <Switch
            id="llmAutoSubmit"
            checked={Boolean(llm.autoSubmit)}
            onChange={(autoSubmit) => handleLlmPatch({ autoSubmit })}
            label="测验 / 作业自动提交"
            description="关闭时仅自动填写答案，方便你核对后手动提交"
          />
        </div>

        <div className="space-y-3 border-t border-outline-variant/60 pt-3">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label htmlFor="llmApiBase">API Base URL</Label>
              <Input
                id="llmApiBase"
                value={llm.apiBase || ''}
                onChange={(e) => handleLlmPatch({ apiBase: e.target.value })}
                placeholder={llmProviderMeta?.apiBase || 'https://api.openai.com/v1'}
                className="font-mono text-xs"
              />
              {!llmBaseValid ? (
                <p role="alert" className="text-xs text-error">
                  API Base 必须以 http:// 或 https:// 开头
                </p>
              ) : (
                <p className="text-[11px] text-on-surface-variant">
                  支持填写根地址或完整接口地址，支持任意 OneAPI / NewAPI 中转站
                </p>
              )}
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="llmApiFormat">接口协议</Label>
              <Select
                id="llmApiFormat"
                value={llm.apiFormat || 'openai'}
                onChange={(e) => {
                  const nextFormat = e.target.value;
                  handleLlmPatch({
                    apiFormat: nextFormat,
                    ...(nextFormat === 'azure' && !llm.apiVersion ? { apiVersion: '2024-10-21' } : {}),
                  });
                }}
              >
                {LLM_API_FORMATS.map((fmt) => (
                  <option key={fmt.id} value={fmt.id}>
                    {fmt.label}
                  </option>
                ))}
              </Select>
              <p className="text-[11px] text-on-surface-variant">{llmFormatMeta.hint}</p>
            </div>
          </div>

          {(llm.apiFormat === 'azure' || Boolean(llm.apiVersion)) && (
            <div className="space-y-1.5">
              <Label htmlFor="llmApiVersion">
                API Version{llm.apiFormat === 'azure' ? '（Azure 必填，如 2024-10-21）' : '（可选）'}
              </Label>
              <Input
                id="llmApiVersion"
                value={llm.apiVersion || ''}
                onChange={(e) => handleLlmPatch({ apiVersion: e.target.value })}
                placeholder={llm.apiFormat === 'azure' ? '2024-10-21' : '留空使用协议默认值'}
                className="font-mono text-xs sm:max-w-xs"
              />
            </div>
          )}

          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label htmlFor="llmStreamTimeout">流式总超时（秒）</Label>
              <NumberInput
                id="llmStreamTimeout"
                min={1}
                integer
                value={llm.streamTimeout ?? 120}
                onValueChange={(streamTimeout) => handleLlmPatch({ streamTimeout })}
                onValidityChange={(valid) => onNumberValidity('llmStreamTimeout', valid)}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="llmStreamIdleTimeout">流式空闲超时（秒）</Label>
              <NumberInput
                id="llmStreamIdleTimeout"
                min={1}
                integer
                value={llm.streamIdleTimeout ?? 30}
                onValueChange={(streamIdleTimeout) => handleLlmPatch({ streamIdleTimeout })}
                onValidityChange={(valid) => onNumberValidity('llmStreamIdleTimeout', valid)}
              />
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-3 pt-1">
            <Button
              type="button"
              variant="tonal"
              size="sm"
              onClick={handleTestLlmConnection}
              disabled={testingLlm || !llmConfigured || !llmBaseValid || !proxyValid}
            >
              {testingLlm ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
              ) : (
                <PlugZap className="h-3.5 w-3.5" aria-hidden="true" />
              )}
              {testingLlm ? '测试中...' : '测试模型连接'}
            </Button>
            {llmTestResult && (
              <p
                role={llmTestResult.type === 'error' ? 'alert' : 'status'}
                className={`text-xs font-medium ${
                  llmTestResult.type === 'error' ? 'text-error' : 'text-success'
                }`}
              >
                {llmTestResult.message}
              </p>
            )}
          </div>
        </div>
      </div>
    </section>
  );
}
