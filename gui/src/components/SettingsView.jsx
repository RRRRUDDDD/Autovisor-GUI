import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Globe,
  Bell,
  RefreshCw,
  CheckCircle2,
  Eye,
  EyeOff,
  Send,
  Loader2,
  AlertCircle,
  Bot,
  PlugZap,
  Sparkles,
  KeyRound,
  ScrollText,
  FolderOpen,
  Trash2,
  ExternalLink,
  AppWindow,
  Power,
  Cpu,
  Download,
  Stethoscope,
  FileCode2,
} from 'lucide-react';
import Button from './ui/Button';
import Input from './ui/Input';
import Label from './ui/Label';
import Select from './ui/Select';
import Switch from './ui/Switch';
import {
  NOTIFICATION_PROVIDERS,
  defaultLlmConfig,
  isNotificationConfigured,
  isValidLlmApiBase,
  isValidProxyUrl,
} from '../lib/configModel';
import {
  LLM_API_FORMATS,
  LLM_PROVIDERS,
  LLM_PROVIDER_GROUPS,
  getLlmDisplayName,
  getLlmProvider,
  inferLlmProvider,
  isLlmConfigured,
  isLocalLlmEndpoint,
} from '../lib/llmProviders';

const SettingsView = ({
  config,
  onChangeConfig,
  runtime,
  dirty,
  saving,
  saveStatus,
  onRetrySave,
  taskRunning = false,
  onSelectBrowser,
  onRedetectBrowsers,
  onPickBrowserExe,
  onCheckBrowser,
  onRestartBackend,
  restartingBackend = false,
  onTestNotification,
  onTestLlmConnection,
  onFetchLlmModels,
  onImportCookies,
  onClearCookies,
  onOpenPath,
  onOpenUrl,
  onListLogFiles,
  onReadLogFile,
  onChangeCloseBehavior,
  onToggleDebugLog,
  onCheckUpdate,
  checkingUpdate = false,
  updateInfo = null,
  updateError = '',
}) => {
  const [showLlmApiKey, setShowLlmApiKey] = useState(false);
  const [savingCloseBehavior, setSavingCloseBehavior] = useState(false);
  const [closeBehaviorError, setCloseBehaviorError] = useState('');
  const [testingNotify, setTestingNotify] = useState(false);
  const [notifyResult, setNotifyResult] = useState(null);
  const [testingLlm, setTestingLlm] = useState(false);
  const [llmTestResult, setLlmTestResult] = useState(null);
  const [fetchingModels, setFetchingModels] = useState(false);
  const [fetchedModels, setFetchedModels] = useState([]);
  const [fetchModelsError, setFetchModelsError] = useState('');
  const [logFiles, setLogFiles] = useState([]);
  const [loadingLogs, setLoadingLogs] = useState(false);
  const [selectedLog, setSelectedLog] = useState(null);
  const [logContent, setLogContent] = useState('');
  const [showUpdateNotes, setShowUpdateNotes] = useState(false);
  const [editingCredentials, setEditingCredentials] = useState(false);
  const [showSavedPassword, setShowSavedPassword] = useState(false);

  const refreshLogs = useCallback(async () => {
    if (!onListLogFiles) return;
    setLoadingLogs(true);
    try {
      const files = await onListLogFiles();
      setLogFiles(Array.isArray(files) ? files : []);
    } finally {
      setLoadingLogs(false);
    }
  }, [onListLogFiles]);

  useEffect(() => {
    if (!onListLogFiles) return undefined;
    let active = true;
    setLoadingLogs(true);
    Promise.resolve(onListLogFiles())
      .then((files) => {
        if (active) setLogFiles(Array.isArray(files) ? files : []);
      })
      .finally(() => {
        if (active) setLoadingLogs(false);
      });
    return () => {
      active = false;
    };
  }, [onListLogFiles]);

  const handleOpenLog = async (file) => {
    if (!onReadLogFile) return;
    setSelectedLog(file);
    try {
      const text = await onReadLogFile(file.path);
      setLogContent(text || '（日志文件为空）');
    } catch (err) {
      setLogContent(`读取日志失败：${err?.message || String(err)}`);
    }
  };

  const detectedBrowsers = runtime?.detectedBrowsers || [];
  const proxyValid = isValidProxyUrl(config.proxy);
  const llm = config.llm || defaultLlmConfig();
  const llmBaseValid = isValidLlmApiBase(llm.apiBase);
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
  const hasInvalidFields = !proxyValid || !llmBaseValid;
  const isAutoSaving = saving || (dirty && !hasInvalidFields && saveStatus?.type !== 'error');

  const isWindows = !runtime?.platform || runtime.platform === 'windows';
  const notifyProvider = config.notificationProvider || '';
  const notifyProviderMeta = useMemo(
    () => NOTIFICATION_PROVIDERS.find((item) => item.value === notifyProvider) || NOTIFICATION_PROVIDERS[0],
    [notifyProvider]
  );
  const notifyConfigured = useMemo(() => isNotificationConfigured(config), [config]);

  const updateField = (patch) => {
    onChangeConfig((prev) => ({ ...prev, ...patch }));
  };

  const updateLlmField = (patch) => {
    setLlmTestResult(null);
    setFetchModelsError('');
    onChangeConfig((prev) => ({
      ...prev,
      llm: { ...(prev.llm || defaultLlmConfig()), ...patch },
    }));
  };

  const handleSelectLlmProvider = (providerId) => {
    const preset = getLlmProvider(providerId);
    setLlmTestResult(null);
    setFetchedModels([]);
    setFetchModelsError('');
    if (!preset || preset.id === 'custom') {
      updateLlmField({ provider: 'custom' });
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
    updateLlmField(patch);
  };

  const handleTestLlmConnection = async () => {
    if (!onTestLlmConnection) return;
    setTestingLlm(true);
    setLlmTestResult(null);
    try {
      const message = await onTestLlmConnection({
        llm,
        proxy: config.proxy || '',
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
        proxy: config.proxy || '',
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

  const handleTestNotification = async () => {
    if (!onTestNotification) return;
    setTestingNotify(true);
    setNotifyResult(null);
    try {
      const message = await onTestNotification({
        provider: config.notificationProvider || '',
        url: config.notificationUrl || '',
        tgChatId: config.notificationTgChatId || '',
        proxy: config.proxy || '',
      });
      setNotifyResult({ type: 'success', message: message || '测试推送发送成功' });
    } catch (err) {
      setNotifyResult({
        type: 'error',
        message: err?.message || String(err) || '测试推送发送失败',
      });
    } finally {
      setTestingNotify(false);
    }
  };

  const handleChangeCloseBehavior = async (nextBehavior) => {
    if (!onChangeCloseBehavior || savingCloseBehavior) return;
    setSavingCloseBehavior(true);
    setCloseBehaviorError('');
    try {
      await onChangeCloseBehavior(nextBehavior);
    } catch (err) {
      setCloseBehaviorError(err?.message || String(err) || '无法保存关闭方式，请重试');
    } finally {
      setSavingCloseBehavior(false);
    }
  };

  const closeBehavior = runtime?.closeBehavior || 'ask';
  const shutdownEnabled = config.shutdownOnComplete === true;

  return (
    <div className="space-y-5 pb-6 animate-stagger-up">
      {/* 顶部异常或校验提示条（正常修改会自动保存，无需手动确认） */}
      {(saveStatus?.type === 'error' || hasInvalidFields) && (
        <div
          role="alert"
          className={`flex flex-wrap items-center justify-between gap-3 rounded-2xl px-5 py-3.5 text-sm animate-fade-in ${
            saveStatus?.type === 'error'
              ? 'bg-error-container text-on-error-container'
              : 'bg-warning-container text-on-warning-container'
          }`}
        >
          <span className="flex items-center gap-2 font-medium">
            <AlertCircle className="h-4 w-4 shrink-0" />
            {saveStatus?.type === 'error'
              ? saveStatus.message
              : '当前存在格式无效的配置项，修正后将自动保存'}
          </span>
          {saveStatus?.type === 'error' && onRetrySave && (
            <Button
              size="sm"
              onClick={onRetrySave}
              disabled={saving || hasInvalidFields}
            >
              {saving ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
              ) : (
                <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
              )}
              {saving ? '重试中...' : '重试保存'}
            </Button>
          )}
        </div>
      )}

      <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
        {/* 卡片 1：大模型智能答题（data/llm.json） */}
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
                    onChange={(e) => updateLlmField({ apiKey: e.target.value })}
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
                onChange={(e) => updateLlmField({ model: e.target.value })}
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
                        onClick={() => updateLlmField({ model: modelName })}
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
                onChange={(e) => updateLlmField({ visionModel: e.target.value })}
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
                onChange={(autoSubmit) => updateLlmField({ autoSubmit })}
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
                    onChange={(e) => updateLlmField({ apiBase: e.target.value })}
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
                      updateLlmField({
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
                    onChange={(e) => updateLlmField({ apiVersion: e.target.value })}
                    placeholder={llm.apiFormat === 'azure' ? '2024-10-21' : '留空使用协议默认值'}
                    className="font-mono text-xs sm:max-w-xs"
                  />
                </div>
              )}

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

        {/* 卡片 2：完成后关机 */}
        <section className="flex flex-col rounded-[24px] bg-surface-container-low p-6">
          <div className="mb-5 flex items-center justify-between gap-3">
            <div className="flex items-center gap-3">
              <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl bg-warning-container text-on-warning-container">
                <Power className="h-5 w-5" aria-hidden="true" />
              </div>
              <div>
                <h2 className="text-base font-semibold text-on-surface">完成后关机</h2>
                <p className="text-xs text-on-surface-variant">刷课任务结束后自动关闭计算机</p>
              </div>
            </div>
            <span
              className={`shrink-0 rounded-full px-3 py-1 text-xs font-medium ${
                shutdownEnabled
                  ? 'bg-warning-container text-on-warning-container'
                  : 'bg-surface-container-high text-on-surface-variant'
              }`}
            >
              {shutdownEnabled ? '已开启' : '未开启'}
            </span>
          </div>

          <div className="space-y-3">
            <Switch
              id="shutdownOnComplete"
              checked={shutdownEnabled}
              onChange={(shutdownOnComplete) => updateField({ shutdownOnComplete })}
              label="刷课任务完成后自动关机"
              description={
                shutdownEnabled
                  ? '刷课任务正常完成或达到单课时长上限后将倒计时 60 秒自动关机，期间可在运行监控页随时取消；手动停止、任务出错或诊断检测不会关机。'
                  : '仅对自动学习与复习刷分任务生效；手动停止、异常中断或诊断检测不会触发关机。'
              }
            />
          </div>
        </section>

        {/* 卡片 3：桌面窗口关闭行为 */}
        <section className="flex flex-col rounded-[24px] bg-surface-container-low p-6">
          <div className="mb-5 flex items-center justify-between gap-3">
            <div className="flex items-center gap-3">
              <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl bg-primary-container text-on-primary-container">
                <AppWindow className="h-5 w-5" aria-hidden="true" />
              </div>
              <div>
                <h2 className="text-base font-semibold text-on-surface">桌面窗口</h2>
                <p className="text-xs text-on-surface-variant">点击窗口右上角关闭按钮时的操作</p>
              </div>
            </div>
          </div>

          <div className="space-y-3">
            <div className="space-y-1.5">
              <Label htmlFor="close-behavior">关闭窗口时</Label>
              <Select
                id="close-behavior"
                value={closeBehavior}
                disabled={savingCloseBehavior}
                onChange={(e) => void handleChangeCloseBehavior(e.target.value)}
              >
                <option value="ask">每次询问</option>
                <option value="minimize">最小化</option>
                <option value="tray">最小化到托盘</option>
                <option value="exit">直接退出</option>
              </Select>
              <p className="text-xs leading-relaxed text-on-surface-variant">
                选择「每次询问」时会在关闭窗口前弹出确认框，并支持勾选记住选择；最小化到托盘后可单击系统托盘图标随时恢复窗口。
              </p>
              {closeBehaviorError && (
                <p role="alert" className="text-xs font-medium text-error">
                  {closeBehaviorError}
                </p>
              )}
            </div>
          </div>
        </section>

        {/* 卡片 4：浏览器与网络代理 */}
        <section className="flex flex-col rounded-[24px] bg-surface-container-low p-6">
          <div className="mb-5 flex items-center justify-between gap-3">
            <div className="flex items-center gap-3">
              <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl bg-primary-container text-on-primary-container">
                <Globe className="h-5 w-5" aria-hidden="true" />
              </div>
              <div>
                <h2 className="text-base font-semibold text-on-surface">浏览器与网络</h2>
                <p className="text-xs text-on-surface-variant">自动识别本机浏览器、代理与 CDP 调试</p>
              </div>
            </div>
            <Button variant="tonal" size="sm" onClick={onRedetectBrowsers}>
              <RefreshCw className="h-3.5 w-3.5" />
              重新扫描
            </Button>
          </div>

          <div className="space-y-4">
            <div className="space-y-2">
              <Label>本机已识别浏览器</Label>
              {detectedBrowsers.length > 0 ? (
                <div className="space-y-2">
                  {detectedBrowsers.map((browser) => {
                    const selected =
                      config.exePath &&
                      config.exePath.toLowerCase() === browser.exePath.toLowerCase();
                    return (
                      <button
                        key={browser.exePath}
                        type="button"
                        onClick={() => onSelectBrowser?.(browser)}
                        className={`flex w-full items-start justify-between gap-3 rounded-2xl px-4 py-3 text-left transition-all duration-150 ease-m3 ${
                          selected
                            ? 'bg-secondary-container text-on-secondary-container'
                            : 'bg-surface-container hover:bg-surface-container-high text-on-surface'
                        }`}
                      >
                        <div className="min-w-0">
                          <div className="flex items-center gap-2">
                            <span className="text-sm font-medium">{browser.name}</span>
                            {browser.recommended && (
                              <span className="rounded-full bg-primary-container px-2 py-0.5 text-[11px] font-medium text-on-primary-container">
                                推荐
                              </span>
                            )}
                          </div>
                          <p
                            className="mt-1 truncate font-mono text-[11px] opacity-75"
                            title={browser.exePath}
                          >
                            {browser.exePath}
                          </p>
                        </div>
                        {selected && (
                          <CheckCircle2 className="mt-0.5 h-4.5 w-4.5 shrink-0 text-primary" />
                        )}
                      </button>
                    );
                  })}
                </div>
              ) : (
                <div className="rounded-2xl bg-warning-container px-4 py-3 text-xs text-on-warning-container">
                  未自动检测到 Edge / Chrome，请点击下方手动指定浏览器可执行文件路径。
                </div>
              )}
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="proxy">代理服务器（可选）</Label>
              <Input
                id="proxy"
                value={config.proxy || ''}
                onChange={(e) => updateField({ proxy: e.target.value })}
                placeholder="如 http://127.0.0.1:7890 或 socks5://127.0.0.1:1080"
                className="font-mono text-xs"
              />
              {!proxyValid ? (
                <p role="alert" className="text-xs text-error">
                  代理格式无效，请以 http://、https:// 或 socks5:// 开头
                </p>
              ) : (
                <p className="text-xs text-on-surface-variant">
                  留空则直连；同时作用于账号登录、拉取课程、浏览器刷课、更新检查与推送通知。
                </p>
              )}
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="browserDriver">浏览器内核 / 驱动（driver）</Label>
              <Select
                id="browserDriver"
                value={config.driver || 'Edge'}
                onChange={(e) => {
                  const nextDriver = e.target.value;
                  const patch = { driver: nextDriver };
                  if (nextDriver === 'Chromium') {
                    const matched = detectedBrowsers.find(
                      (b) => (b.driver || '').toLowerCase() === 'chromium'
                    );
                    patch.exePath = matched ? matched.exePath : '';
                  } else {
                    const curExe = (config.exePath || '').trim();
                    const curLower = curExe.toLowerCase();
                    const curMatched = detectedBrowsers.find(
                      (b) => (b.exePath || '').toLowerCase() === curLower
                    );
                    const curInferred = curMatched
                      ? curMatched.driver
                      : curLower.includes('msedge')
                        ? 'Edge'
                        : curLower.includes('chromium') || curLower.includes('ms-playwright')
                          ? 'Chromium'
                          : curExe
                            ? 'Chrome'
                            : '';
                    if (!curExe || (curInferred || '').toLowerCase() !== nextDriver.toLowerCase()) {
                      const matched = detectedBrowsers.find(
                        (b) => (b.driver || '').toLowerCase() === nextDriver.toLowerCase()
                      );
                      patch.exePath = matched ? matched.exePath : '';
                    }
                  }
                  updateField(patch);
                }}
              >
                <option value="Edge">Microsoft Edge</option>
                <option value="Chrome">Google Chrome</option>
                <option value="Chromium">Playwright 内置 Chromium</option>
              </Select>
              {config.driver === 'Chromium' && (
                <p className="text-xs text-on-surface-variant">
                  使用 Playwright 内置 Chromium 时将忽略下方手动可执行路径
                </p>
              )}
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="exePath">手动覆盖路径（一般无需填写）</Label>
              <div className="flex gap-2">
                <Input
                  id="exePath"
                  value={config.exePath}
                  onChange={(e) => {
                    const nextPath = e.target.value;
                    const patch = { exePath: nextPath };
                    if (config.driver !== 'Chromium') {
                      const lower = nextPath.toLowerCase();
                      if (lower.includes('msedge')) {
                        patch.driver = 'Edge';
                      } else if (lower.includes('chrome')) {
                        patch.driver = 'Chrome';
                      }
                    }
                    updateField(patch);
                  }}
                  placeholder="已由设备自动识别填充"
                  className="font-mono text-xs"
                />
                <Button variant="tonal" className="h-11 shrink-0" onClick={onPickBrowserExe}>
                  浏览
                </Button>
              </div>
            </div>

            <div className="border-t border-outline-variant/60 pt-3">
              <Switch
                id="attachExistingChrome"
                checked={config.attachExistingChrome}
                onChange={(attachExistingChrome) => updateField({ attachExistingChrome })}
                label="连接已打开的 Chrome（CDP 调试）"
                description="进阶选项：直接复用已开启远程调试端口的本地 Chrome"
              />
              {config.attachExistingChrome && (
                <div className="mt-3 space-y-1.5 animate-fade-in">
                  <Label htmlFor="cdpUrl">CDP 端点地址</Label>
                  <Input
                    id="cdpUrl"
                    value={config.cdpUrl}
                    onChange={(e) => updateField({ cdpUrl: e.target.value })}
                    placeholder="http://127.0.0.1:9222"
                    className="font-mono text-xs"
                  />
                </div>
              )}
            </div>

            {onCheckBrowser && (
              <div className="flex flex-wrap items-center gap-3 border-t border-outline-variant/60 pt-3">
                <Button
                  type="button"
                  variant="tonal"
                  size="sm"
                  onClick={onCheckBrowser}
                  disabled={
                    taskRunning ||
                    !proxyValid ||
                    (config.driver !== 'Chromium' &&
                      !config.exePath?.trim() &&
                      detectedBrowsers.length === 0)
                  }
                >
                  <Stethoscope className="h-3.5 w-3.5" aria-hidden="true" />
                  检测浏览器与登录状态
                </Button>
                <span className="text-xs text-on-surface-variant">
                  只读验证浏览器启动、隐身配置与当前 Cookie 登录态
                </span>
              </div>
            )}
          </div>
        </section>

        {/* 卡片 5：消息推送通知 */}
        <section className="flex flex-col rounded-[24px] bg-surface-container-low p-6">
          <div className="mb-5 flex items-center gap-3">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl bg-secondary-container text-on-secondary-container">
              <Bell className="h-5 w-5" aria-hidden="true" />
            </div>
            <div>
              <h2 className="text-base font-semibold text-on-surface">消息推送通知</h2>
              <p className="text-xs text-on-surface-variant">
                支持 Windows 系统通知、Server酱、Qmsg酱、Bark 与 Telegram
              </p>
            </div>
          </div>

          <div className="space-y-4">
            <div className="space-y-1.5">
              <Label htmlFor="notification-provider">通知服务</Label>
              <Select
                id="notification-provider"
                value={notifyProvider}
                onChange={(e) => {
                  updateField({ notificationProvider: e.target.value });
                  setNotifyResult(null);
                }}
              >
                {NOTIFICATION_PROVIDERS.map((item) => (
                  <option
                    key={item.value || 'none'}
                    value={item.value}
                    disabled={!isWindows && item.value === 'Windows'}
                  >
                    {!isWindows && item.value === 'Windows'
                      ? `${item.label}（仅 Windows 可用）`
                      : item.label}
                  </option>
                ))}
              </Select>
              {!isWindows && notifyProvider === 'Windows' && (
                <p role="alert" className="text-xs text-warning">
                  Windows 系统通知仅 Windows 可用，请选择其他推送渠道或设为「不使用通知」
                </p>
              )}
            </div>

            {notifyProvider && notifyProvider !== 'Windows' && (
              <>
                <div className="space-y-1.5">
                  <Label htmlFor="notification-url">通知 URL</Label>
                  <Input
                    id="notification-url"
                    type="text"
                    placeholder="https://..."
                    value={config.notificationUrl || ''}
                    onChange={(e) => {
                      updateField({ notificationUrl: e.target.value });
                      setNotifyResult(null);
                    }}
                    className="font-mono text-xs"
                  />
                  {notifyProviderMeta.hint && (
                    <p className="break-all font-mono text-xs text-on-surface-variant">
                      {notifyProviderMeta.hint}
                    </p>
                  )}
                </div>

                {notifyProvider === 'Telegram' && (
                  <div className="space-y-1.5">
                    <Label htmlFor="tg-chat-id">Chat ID</Label>
                    <Input
                      id="tg-chat-id"
                      type="text"
                      placeholder="123456789"
                      value={config.notificationTgChatId || ''}
                      onChange={(e) => {
                        updateField({ notificationTgChatId: e.target.value });
                        setNotifyResult(null);
                      }}
                      className="font-mono text-xs"
                    />
                  </div>
                )}
              </>
            )}

            <div className="space-y-0.5 border-t border-outline-variant/60 pt-3">
              <Switch
                id="notifyOnVerify"
                checked={config.notifyOnVerify !== false}
                onChange={(notifyOnVerify) => updateField({ notifyOnVerify })}
                label="遇安全验证 / 需人工登录时提醒"
                description="自动识别失败或登录态失效时立即发送提醒"
              />
              <Switch
                id="notifyOnError"
                checked={config.notifyOnError !== false}
                onChange={(notifyOnError) => updateField({ notifyOnError })}
                label="任务异常中断时提醒"
                description="课程进度未确认或发生未处理异常时通知"
              />
              <Switch
                id="notifyOnFinish"
                checked={config.notifyOnFinish !== false}
                onChange={(notifyOnFinish) => updateField({ notifyOnFinish })}
                label="学习完成 / 达到时限时提醒"
                description="全部课程完成或达到每课时长限制时发送汇总"
              />
            </div>

            <div className="flex flex-wrap items-center gap-3 pt-1">
              <Button
                type="button"
                variant="tonal"
                size="sm"
                onClick={handleTestNotification}
                disabled={
                  testingNotify ||
                  !notifyConfigured ||
                  !proxyValid ||
                  (!isWindows && notifyProvider === 'Windows')
                }
              >
                {testingNotify ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
                ) : (
                  <Send className="h-3.5 w-3.5" aria-hidden="true" />
                )}
                {testingNotify ? '发送中...' : '发送测试推送'}
              </Button>
              {notifyResult && (
                <p
                  role={notifyResult.type === 'error' ? 'alert' : 'status'}
                  className={`text-xs font-medium ${
                    notifyResult.type === 'error' ? 'text-error' : 'text-success'
                  }`}
                >
                  {notifyResult.message}
                </p>
              )}
            </div>
          </div>
        </section>

        {/* 卡片 6：智慧树登录 Cookie 管理 */}
        <section className="rounded-[24px] bg-surface-container-low p-6 lg:col-span-2">
          <div className="flex flex-wrap items-center justify-between gap-4">
            <div className="flex min-w-0 items-center gap-3">
              <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl bg-primary-container text-on-primary-container">
                <KeyRound className="h-5 w-5" aria-hidden="true" />
              </div>
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <h2 className="text-base font-semibold text-on-surface">智慧树登录 Cookie 管理</h2>
                  <span
                    className={`inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-medium ${
                      runtime?.cookieExists
                        ? 'bg-success-container text-on-success-container'
                        : 'bg-surface-container-highest text-on-surface-variant'
                    }`}
                  >
                    {runtime?.cookieExists && <CheckCircle2 className="h-3 w-3" aria-hidden="true" />}
                    {runtime?.cookieExists ? '已保存' : '未缓存'}
                  </span>
                </div>
                <p className="mt-0.5 text-xs text-on-surface-variant">
                  {runtime?.cookieExists
                    ? `当前本地已缓存 ${runtime.cookieCount} 条有效凭证（data/cookies.json）`
                    : '本地暂无缓存的 Cookie，首次登录完成后会自动保存'}
                </p>
              </div>
            </div>

            <div className="flex flex-wrap items-center gap-2">
              <Button
                size="sm"
                variant="tonal"
                onClick={onImportCookies}
                disabled={taskRunning}
              >
                从 JSON 文件导入 Cookie
              </Button>
              <Button
                size="sm"
                variant="destructive-tonal"
                onClick={onClearCookies}
                disabled={!runtime?.cookieExists || taskRunning}
              >
                <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
                清除本地 Cookie
              </Button>
            </div>
          </div>

          {/* 自动重登凭证（可选） */}
          <div className="mt-5 border-t border-outline-variant/60 pt-4">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <div className="flex flex-wrap items-center gap-2">
                  <h3 className="text-sm font-semibold text-on-surface">自动重登凭证（可选）</h3>
                  <span
                    className={`rounded-full px-2.5 py-0.5 text-xs font-medium ${
                      config.username && config.password
                        ? 'bg-primary-container text-on-primary-container'
                        : 'bg-surface-container-highest text-on-surface-variant'
                    }`}
                  >
                    {config.username && config.password
                      ? `已保存账号 ${
                          config.username.length >= 7
                            ? `${config.username.slice(0, 3)}****${config.username.slice(-4)}`
                            : `${config.username.slice(0, 1)}***`
                        }`
                      : config.username
                        ? '仅保存账号（未存密码）'
                        : '未保存'}
                  </span>
                </div>
                <p className="mt-0.5 text-xs text-on-surface-variant">
                  默认不保存密码；仅用于长时挂机中途 Cookie 失效时自动重登，明文保存在本机 config.ini，随时可清除
                </p>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <Button
                  type="button"
                  size="sm"
                  variant="tonal"
                  onClick={() => setEditingCredentials((v) => !v)}
                  disabled={taskRunning}
                >
                  {editingCredentials ? '收起凭证编辑' : '编辑自动重登账密'}
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant="destructive-tonal"
                  onClick={() => {
                    updateField({ username: '', password: '' });
                    setEditingCredentials(false);
                  }}
                  disabled={(!config.username && !config.password) || taskRunning}
                >
                  <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
                  清除已保存账密
                </Button>
              </div>
            </div>

            {editingCredentials && (
              <div className="mt-3 grid grid-cols-1 gap-3 rounded-2xl bg-surface-container p-4 sm:grid-cols-2 animate-fade-in">
                <div className="space-y-1.5">
                  <Label htmlFor="saved-username">手机号 / 账号</Label>
                  <Input
                    id="saved-username"
                    value={config.username || ''}
                    onChange={(e) => updateField({ username: e.target.value })}
                    placeholder="用于 Cookie 失效时自动重登的手机号"
                    className="font-mono text-xs"
                  />
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="saved-password">登录密码</Label>
                  <div className="relative">
                    <Input
                      id="saved-password"
                      type={showSavedPassword ? 'text' : 'password'}
                      value={config.password || ''}
                      onChange={(e) => updateField({ password: e.target.value })}
                      placeholder="留空则不自动填写密码"
                      className="pr-11 font-mono text-xs"
                    />
                    <button
                      type="button"
                      onClick={() => setShowSavedPassword((s) => !s)}
                      aria-label={showSavedPassword ? '隐藏已保存密码' : '显示已保存密码'}
                      className="absolute inset-y-0 right-0 flex w-11 items-center justify-center rounded-r-xl text-on-surface-variant hover:text-on-surface"
                    >
                      {showSavedPassword ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                    </button>
                  </div>
                </div>
              </div>
            )}
          </div>
        </section>

        {/* 卡片 7：历史运行日志 */}
        <section className="flex flex-col rounded-[24px] bg-surface-container-low p-6 lg:col-span-2">
          <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
            <div className="flex items-center gap-3">
              <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl bg-tertiary-container text-on-tertiary-container">
                <ScrollText className="h-5 w-5" aria-hidden="true" />
              </div>
              <div>
                <h2 className="text-base font-semibold text-on-surface">历史运行日志</h2>
                <p className="text-xs text-on-surface-variant">点击任意记录在线预览末尾输出</p>
              </div>
            </div>
            <div className="flex items-center gap-1.5">
              <Button variant="ghost" size="sm" onClick={refreshLogs} disabled={loadingLogs}>
                <RefreshCw className={`h-3.5 w-3.5 ${loadingLogs ? 'animate-spin' : ''}`} aria-hidden="true" />
                刷新
              </Button>
              {runtime?.logsDir && onOpenPath && (
                <Button
                  variant="tonal"
                  size="sm"
                  onClick={() => onOpenPath(runtime.logsDir)}
                >
                  <FolderOpen className="h-3.5 w-3.5" aria-hidden="true" />
                  打开日志文件夹
                </Button>
              )}
            </div>
          </div>

          <div className="mb-4 rounded-2xl bg-surface-container px-4 py-2">
            <Switch
              id="enableDebugLog"
              checked={Boolean(config.enableDebugLog)}
              onChange={(enableDebugLog) => {
                updateField({ enableDebugLog });
                onToggleDebugLog?.(enableDebugLog);
              }}
              label="实时输出 DEBUG 调试日志"
              description="开启后运行监控页将实时显示底层 [DEBUG] 调试信息（支持任务运行中动态切换，默认关闭）"
            />
          </div>

          {logFiles.length === 0 ? (
            <div className="flex flex-col items-center justify-center rounded-2xl bg-surface-container py-12 text-center">
              <ScrollText className="h-8 w-8 text-on-surface-variant/60" aria-hidden="true" />
              <p className="mt-3 text-sm font-semibold text-on-surface">暂无历史日志文件</p>
              <p className="mt-1 text-xs text-on-surface-variant">
                运行 Autovisor GUI 后，日志会自动保存在数据目录的 logs/ 下。
              </p>
            </div>
          ) : (
            <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
              <div className="max-h-64 space-y-1 overflow-y-auto rounded-2xl bg-surface-container p-2 lg:col-span-4 lg:max-h-[24rem]">
                {logFiles.map((file) => {
                  const active = selectedLog?.path === file.path;
                  return (
                    <button
                      key={file.path}
                      type="button"
                      onClick={() => handleOpenLog(file)}
                      className={`flex w-full items-center justify-between gap-2 rounded-xl px-3 py-2.5 text-left text-xs transition-colors ease-m3 ${
                        active
                          ? 'bg-secondary-container font-semibold text-on-secondary-container'
                          : 'text-on-surface hover:bg-on-surface/[0.06]'
                      }`}
                    >
                      <span className="truncate font-mono">{file.name}</span>
                      <span className="shrink-0 opacity-75 tnum">
                        {file.sizeLabel} · {file.modifiedAt}
                      </span>
                    </button>
                  );
                })}
              </div>

              <div className="flex min-h-0 flex-col lg:col-span-8">
                <div className="mb-2 flex items-center justify-between gap-2">
                  <Label className="truncate text-xs">
                    {selectedLog ? `预览末尾内容：${selectedLog.name}` : '点击左侧任意日志文件查看详情'}
                  </Label>
                  {selectedLog && onOpenPath && (
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => onOpenPath(selectedLog.path)}
                    >
                      <ExternalLink className="h-3.5 w-3.5" aria-hidden="true" />
                      外部编辑器打开
                    </Button>
                  )}
                </div>
                <pre className="min-h-[16rem] max-h-[24rem] flex-1 overflow-auto rounded-2xl bg-[#12151C] p-4 font-mono text-xs leading-relaxed text-slate-200 whitespace-pre-wrap [overflow-wrap:anywhere]">
                  {logContent || '选择日志文件以查看运行细节...'}
                </pre>
              </div>
            </div>
          )}
        </section>

        {/* 卡片 8：运行环境与版本 */}
        <section className="flex flex-col rounded-[24px] bg-surface-container-low p-6 lg:col-span-2">
          <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
            <div className="flex items-center gap-3">
              <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl bg-primary-container text-on-primary-container">
                <Cpu className="h-5 w-5" aria-hidden="true" />
              </div>
              <div>
                <div className="flex flex-wrap items-center gap-2">
                  <h2 className="text-base font-semibold text-on-surface">运行环境与版本</h2>
                  <span className="rounded-full bg-surface-container-highest px-2.5 py-0.5 font-mono text-xs font-medium text-on-surface-variant">
                    v{runtime?.version || '1.0.0'}
                  </span>
                  {runtime?.backendVersion && (
                    <span className="rounded-full bg-surface-container-highest px-2.5 py-0.5 font-mono text-xs font-medium text-on-surface-variant">
                      Autovisor {runtime.backendVersion}
                    </span>
                  )}
                  <span
                    className={`rounded-full px-2.5 py-0.5 text-xs font-medium ${
                      runtime?.backendPhase === 'ready'
                        ? 'bg-success-container text-on-success-container'
                        : runtime?.backendPhase === 'failed'
                          ? 'bg-error-container text-on-error-container'
                          : 'bg-warning-container text-on-warning-container'
                    }`}
                  >
                    引擎：
                    {runtime?.backendPhase === 'ready'
                      ? '就绪'
                      : runtime?.backendPhase === 'failed'
                        ? '异常'
                        : runtime?.backendPhase === 'starting'
                          ? '启动中'
                          : runtime?.backendPhase === 'stopping'
                            ? '停止中'
                            : '未就绪'}
                  </span>
                </div>
                <p className="mt-0.5 text-xs text-on-surface-variant">
                  检查版本更新、重启内置学习引擎或快速打开数据目录与配置文件
                </p>
              </div>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              {onCheckUpdate && (
                <Button
                  type="button"
                  variant="tonal"
                  size="sm"
                  onClick={onCheckUpdate}
                  disabled={checkingUpdate}
                >
                  {checkingUpdate ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
                  ) : (
                    <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
                  )}
                  {checkingUpdate ? '检查中...' : '检查更新'}
                </Button>
              )}
              {onRestartBackend && (
                <Button
                  type="button"
                  variant="tonal"
                  size="sm"
                  onClick={onRestartBackend}
                  disabled={restartingBackend || taskRunning}
                  title={taskRunning ? '任务运行中，请先停止任务后再重启引擎' : '重启内置学习引擎'}
                >
                  {restartingBackend ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
                  ) : (
                    <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
                  )}
                  {restartingBackend ? '重启中...' : '重启内置引擎'}
                </Button>
              )}
            </div>
          </div>

          <div className="space-y-3">
            {runtime?.backendPhase === 'failed' && runtime?.backendError && (
              <p role="alert" className="text-xs font-medium text-error">
                {runtime.backendError}
              </p>
            )}
            {updateError && (
              <p role="alert" className="text-xs font-medium text-error">
                {updateError}
              </p>
            )}
            {updateInfo && !updateError && (
              <div
                className={`rounded-2xl px-4 py-3 text-xs ${
                  updateInfo.hasUpdate
                    ? 'bg-primary-container/60 text-on-primary-container'
                    : 'bg-surface-container text-on-surface-variant'
                }`}
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="font-medium">
                    {updateInfo.hasUpdate
                      ? `发现新版本 v${updateInfo.latestVersion}（当前 v${updateInfo.currentVersion}）`
                      : `当前已是最新版本（v${updateInfo.currentVersion}）`}
                  </span>
                  {updateInfo.hasUpdate && (
                    <div className="flex flex-wrap items-center gap-2">
                      {updateInfo.notes && (
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          onClick={() => setShowUpdateNotes((v) => !v)}
                        >
                          {showUpdateNotes ? '收起更新日志' : '查看更新日志'}
                        </Button>
                      )}
                      {updateInfo.downloadUrl && onOpenUrl && (
                        <Button
                          type="button"
                          size="sm"
                          onClick={() => onOpenUrl(updateInfo.downloadUrl)}
                        >
                          <Download className="h-3.5 w-3.5" aria-hidden="true" />
                          直接下载
                        </Button>
                      )}
                      {updateInfo.releaseUrl && onOpenUrl && (
                        <Button
                          type="button"
                          variant="tonal"
                          size="sm"
                          onClick={() => onOpenUrl(updateInfo.releaseUrl)}
                        >
                          <ExternalLink className="h-3.5 w-3.5" aria-hidden="true" />
                          打开下载页
                        </Button>
                      )}
                    </div>
                  )}
                </div>
                {updateInfo.hasUpdate && showUpdateNotes && updateInfo.notes && (
                  <pre className="mt-2.5 max-h-48 overflow-auto rounded-xl bg-surface/80 p-3 font-mono text-[11px] leading-relaxed text-on-surface whitespace-pre-wrap">
                    {updateInfo.notes}
                  </pre>
                )}
              </div>
            )}

            <div className="flex flex-wrap items-center gap-2 pt-1">
              {runtime?.baseDir && onOpenPath && (
                <Button
                  type="button"
                  variant="tonal"
                  size="sm"
                  onClick={() => onOpenPath(runtime.baseDir)}
                >
                  <FolderOpen className="h-3.5 w-3.5" aria-hidden="true" />
                  打开数据目录
                </Button>
              )}
              {runtime?.configPath && onOpenPath && (
                <Button
                  type="button"
                  variant="tonal"
                  size="sm"
                  onClick={() => onOpenPath(runtime.configPath)}
                >
                  <FileCode2 className="h-3.5 w-3.5" aria-hidden="true" />
                  打开 config.ini
                </Button>
              )}
              {taskRunning && (
                <span className="text-xs text-on-surface-variant">
                  当前任务运行中，重启引擎前请先停止任务
                </span>
              )}
            </div>
          </div>
        </section>
      </div>
    </div>
  );
};

export default SettingsView;
