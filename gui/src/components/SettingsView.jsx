import { memo, useCallback, useEffect, useState } from 'react';
import { AlertCircle, Loader2, RefreshCw } from 'lucide-react';
import Button from './ui/Button';
import AnswerEnhancementCard from './settings/AnswerEnhancementCard';
import LlmSettingsCard from './settings/LlmSettingsCard';
import WindowBehaviorCard from './settings/WindowBehaviorCard';
import BrowserNetworkCard from './settings/BrowserNetworkCard';
import NotificationCard from './settings/NotificationCard';
import CookieCredentialCard from './settings/CookieCredentialCard';
import LogHistoryCard from './settings/LogHistoryCard';
import RuntimeEnvCard from './settings/RuntimeEnvCard';
import {
  defaultLlmConfig,
  isValidLlmApiBase,
  isValidProxyUrl,
} from '../lib/configModel';

const SettingsView = ({
  config,
  onChangeConfig,
  runtime,
  saving,
  saveStatus,
  onRetrySave,
  onValidityChange,
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
  onTestQuestionBank,
  onExportQuestionCache,
  onImportQuestionCache,
  onClearQuestionCache,
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
  const [invalidNumberFields, setInvalidNumberFields] = useState({});

  const handleNumberValidity = useCallback((field, valid) => {
    setInvalidNumberFields((prev) => {
      const nextInvalid = !valid;
      if (Boolean(prev[field]) === nextInvalid) return prev;
      const next = { ...prev };
      if (nextInvalid) next[field] = true;
      else delete next[field];
      return next;
    });
  }, []);

  const detectedBrowsers = runtime?.detectedBrowsers || [];
  const proxyValid = isValidProxyUrl(config.proxy);
  const llm = config.llm || defaultLlmConfig();
  const llmBaseValid = isValidLlmApiBase(llm.apiBase);
  const hasInvalidNumbers = Object.keys(invalidNumberFields).length > 0;
  const hasInvalidFields = !proxyValid || !llmBaseValid || hasInvalidNumbers;

  useEffect(() => {
    onValidityChange?.(!hasInvalidNumbers);
    return () => onValidityChange?.(true);
  }, [hasInvalidNumbers, onValidityChange]);

  const isWindows = !runtime?.platform || runtime.platform === 'windows';
  const closeBehavior = runtime?.closeBehavior || 'ask';

  const updateField = useCallback(
    (patch) => {
      onChangeConfig((prev) => ({ ...prev, ...patch }));
    },
    [onChangeConfig]
  );

  const updateLlmField = useCallback(
    (patch) => {
      onChangeConfig((prev) => ({
        ...prev,
        llm: { ...(prev.llm || defaultLlmConfig()), ...patch },
      }));
    },
    [onChangeConfig]
  );

  return (
    <div className="space-y-5 pb-6 animate-stagger-up">
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
        <AnswerEnhancementCard
          config={config}
          llm={llm}
          taskRunning={taskRunning}
          updateField={updateField}
          updateLlmField={updateLlmField}
          onNumberValidity={handleNumberValidity}
          onTestQuestionBank={onTestQuestionBank}
          onExportQuestionCache={onExportQuestionCache}
          onImportQuestionCache={onImportQuestionCache}
          onClearQuestionCache={onClearQuestionCache}
        />

        <LlmSettingsCard
          llm={llm}
          proxy={config.proxy}
          proxyValid={proxyValid}
          llmBaseValid={llmBaseValid}
          updateLlmField={updateLlmField}
          onNumberValidity={handleNumberValidity}
          onTestLlmConnection={onTestLlmConnection}
          onFetchLlmModels={onFetchLlmModels}
        />

        <WindowBehaviorCard
          closeBehavior={closeBehavior}
          onChangeCloseBehavior={onChangeCloseBehavior}
        />

        <BrowserNetworkCard
          config={config}
          detectedBrowsers={detectedBrowsers}
          proxyValid={proxyValid}
          taskRunning={taskRunning}
          updateField={updateField}
          onSelectBrowser={onSelectBrowser}
          onRedetectBrowsers={onRedetectBrowsers}
          onPickBrowserExe={onPickBrowserExe}
          onCheckBrowser={onCheckBrowser}
        />

        <NotificationCard
          config={config}
          isWindows={isWindows}
          proxyValid={proxyValid}
          updateField={updateField}
          onTestNotification={onTestNotification}
        />

        <CookieCredentialCard
          config={config}
          runtime={runtime}
          taskRunning={taskRunning}
          updateField={updateField}
          onImportCookies={onImportCookies}
          onClearCookies={onClearCookies}
        />

        <LogHistoryCard
          config={config}
          logsDir={runtime?.logsDir}
          updateField={updateField}
          onNumberValidity={handleNumberValidity}
          onToggleDebugLog={onToggleDebugLog}
          onListLogFiles={onListLogFiles}
          onReadLogFile={onReadLogFile}
          onOpenPath={onOpenPath}
        />

        <RuntimeEnvCard
          runtime={runtime}
          taskRunning={taskRunning}
          restartingBackend={restartingBackend}
          checkingUpdate={checkingUpdate}
          updateInfo={updateInfo}
          updateError={updateError}
          onCheckUpdate={onCheckUpdate}
          onRestartBackend={onRestartBackend}
          onOpenPath={onOpenPath}
          onOpenUrl={onOpenUrl}
        />
      </div>
    </div>
  );
};

export default memo(SettingsView);
