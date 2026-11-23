export const LLM_API_FORMATS = [
  {
    id: 'openai',
    label: 'OpenAI 兼容（Chat Completions）',
    hint: '适用于 DeepSeek、OpenAI、通义千问、智谱、Kimi、豆包、硅基流动、OpenRouter 及绝大多数中转站',
  },
  {
    id: 'openai-responses',
    label: 'OpenAI Responses API',
    hint: '使用 /responses 接口的 OpenAI 新版模型或中转服务',
  },
  {
    id: 'anthropic',
    label: 'Anthropic（Claude 原生）',
    hint: '使用 /v1/messages 与 x-api-key 请求头',
  },
  {
    id: 'gemini',
    label: 'Google Gemini 原生',
    hint: '使用 /v1beta/models/{model}:generateContent 与 x-goog-api-key 请求头',
  },
  {
    id: 'azure',
    label: 'Azure OpenAI',
    hint: '使用 api-key 请求头与 api-version 查询参数，模型名称填 Deployment Name',
  },
  {
    id: 'ollama',
    label: 'Ollama 本地原生',
    hint: '使用 /api/chat，本机部署免 API Key',
  },
];

export const LLM_PROVIDER_GROUPS = [
  { id: 'cn', label: '国内主流模型' },
  { id: 'global', label: '国际主流模型' },
  { id: 'gateway', label: '聚合平台 / 云服务' },
  { id: 'local', label: '本地部署 / 自定义' },
];

export const LLM_PROVIDERS = [
  // 国内主流模型
  {
    id: 'deepseek',
    name: 'DeepSeek（深度求索）',
    shortName: 'DeepSeek',
    group: 'cn',
    apiFormat: 'openai',
    apiBase: 'https://api.deepseek.com',
    models: ['deepseek-chat', 'deepseek-reasoner'],
    keyPlaceholder: 'sk-...',
  },
  {
    id: 'qwen',
    name: '通义千问（阿里云百炼）',
    shortName: '通义千问',
    group: 'cn',
    apiFormat: 'openai',
    apiBase: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
    models: ['qwen-plus', 'qwen-turbo', 'qwen-max', 'qwq-plus'],
    keyPlaceholder: 'sk-...',
  },
  {
    id: 'zhipu',
    name: '智谱 GLM（BigModel）',
    shortName: '智谱 GLM',
    group: 'cn',
    apiFormat: 'openai',
    apiBase: 'https://open.bigmodel.cn/api/paas/v4',
    models: ['glm-4-flash', 'glm-4-air', 'glm-4-plus', 'glm-z1-flash'],
    keyPlaceholder: '智谱 API Key',
  },
  {
    id: 'moonshot',
    name: 'Kimi（月之暗面 Moonshot）',
    shortName: 'Kimi',
    group: 'cn',
    apiFormat: 'openai',
    apiBase: 'https://api.moonshot.cn/v1',
    models: ['kimi-latest', 'moonshot-v1-8k', 'moonshot-v1-32k'],
    keyPlaceholder: 'sk-...',
  },
  {
    id: 'doubao',
    name: '豆包（火山方舟 Doubao）',
    shortName: '豆包',
    group: 'cn',
    apiFormat: 'openai',
    apiBase: 'https://ark.cn-beijing.volces.com/api/v3',
    models: ['doubao-1-5-pro-32k-250115', 'doubao-1-5-lite-32k-250115', 'deepseek-v3-250324'],
    keyPlaceholder: '火山方舟 API Key',
  },
  {
    id: 'siliconflow',
    name: '硅基流动（SiliconFlow）',
    shortName: '硅基流动',
    group: 'cn',
    apiFormat: 'openai',
    apiBase: 'https://api.siliconflow.cn/v1',
    models: ['Qwen/Qwen2.5-72B-Instruct', 'deepseek-ai/DeepSeek-V3', 'deepseek-ai/DeepSeek-R1'],
    keyPlaceholder: 'sk-...',
  },
  {
    id: 'hunyuan',
    name: '腾讯混元（Hunyuan）',
    shortName: '腾讯混元',
    group: 'cn',
    apiFormat: 'openai',
    apiBase: 'https://api.hunyuan.cloud.tencent.com/v1',
    models: ['hunyuan-turbo', 'hunyuan-lite', 'hunyuan-pro'],
    keyPlaceholder: 'sk-...',
  },
  {
    id: 'spark',
    name: '讯飞星火（Spark）',
    shortName: '讯飞星火',
    group: 'cn',
    apiFormat: 'openai',
    apiBase: 'https://spark-api-open.xf-yun.com/v1',
    models: ['generalv3.5', '4.0Ultra', 'lite'],
    keyPlaceholder: 'APIPassword',
  },
  {
    id: 'qianfan',
    name: '百度千帆（文心一言）',
    shortName: '百度千帆',
    group: 'cn',
    apiFormat: 'openai',
    apiBase: 'https://qianfan.baidubce.com/v2',
    models: ['ernie-4.0-8k', 'ernie-speed-128k', 'deepseek-v3'],
    keyPlaceholder: 'bce-v3/...',
  },
  {
    id: 'minimax',
    name: 'MiniMax（海螺）',
    shortName: 'MiniMax',
    group: 'cn',
    apiFormat: 'openai',
    apiBase: 'https://api.minimax.chat/v1',
    models: ['MiniMax-Text-01', 'abab6.5s-chat'],
    keyPlaceholder: 'eyJ...',
  },
  {
    id: 'yi',
    name: '零一万物（Yi）',
    shortName: '零一万物',
    group: 'cn',
    apiFormat: 'openai',
    apiBase: 'https://api.lingyiwanwu.com/v1',
    models: ['yi-lightning', 'yi-large'],
    keyPlaceholder: 'API Key',
  },

  // 国际主流模型
  {
    id: 'openai',
    name: 'OpenAI（GPT / o 系列）',
    shortName: 'OpenAI',
    group: 'global',
    apiFormat: 'openai',
    apiBase: 'https://api.openai.com/v1',
    models: ['gpt-4o-mini', 'gpt-4o', 'gpt-4.1-mini', 'gpt-4.1', 'o4-mini'],
    keyPlaceholder: 'sk-...',
  },
  {
    id: 'anthropic',
    name: 'Anthropic（Claude 原生）',
    shortName: 'Claude',
    group: 'global',
    apiFormat: 'anthropic',
    apiBase: 'https://api.anthropic.com',
    models: ['claude-sonnet-4-20250514', 'claude-3-7-sonnet-latest', 'claude-3-5-haiku-latest'],
    keyPlaceholder: 'sk-ant-...',
  },
  {
    id: 'gemini',
    name: 'Google Gemini（原生）',
    shortName: 'Gemini',
    group: 'global',
    apiFormat: 'gemini',
    apiBase: 'https://generativelanguage.googleapis.com/v1beta',
    models: ['gemini-2.5-flash', 'gemini-2.5-pro', 'gemini-2.0-flash'],
    keyPlaceholder: 'AIza...',
  },
  {
    id: 'xai',
    name: 'xAI（Grok）',
    shortName: 'Grok',
    group: 'global',
    apiFormat: 'openai',
    apiBase: 'https://api.x.ai/v1',
    models: ['grok-3-mini', 'grok-3', 'grok-2-latest'],
    keyPlaceholder: 'xai-...',
  },
  {
    id: 'groq',
    name: 'Groq（超快推理）',
    shortName: 'Groq',
    group: 'global',
    apiFormat: 'openai',
    apiBase: 'https://api.groq.com/openai/v1',
    models: ['llama-3.3-70b-versatile', 'qwen-qwq-32b'],
    keyPlaceholder: 'gsk_...',
  },
  {
    id: 'mistral',
    name: 'Mistral AI',
    shortName: 'Mistral',
    group: 'global',
    apiFormat: 'openai',
    apiBase: 'https://api.mistral.ai/v1',
    models: ['mistral-small-latest', 'mistral-large-latest'],
    keyPlaceholder: 'Mistral API Key',
  },

  // 聚合平台 / 云服务
  {
    id: 'openrouter',
    name: 'OpenRouter（全模型聚合）',
    shortName: 'OpenRouter',
    group: 'gateway',
    apiFormat: 'openai',
    apiBase: 'https://openrouter.ai/api/v1',
    models: ['openai/gpt-4o-mini', 'anthropic/claude-3.5-sonnet', 'google/gemini-2.5-flash', 'deepseek/deepseek-chat'],
    keyPlaceholder: 'sk-or-...',
  },
  {
    id: 'azure',
    name: 'Azure OpenAI',
    shortName: 'Azure',
    group: 'gateway',
    apiFormat: 'azure',
    apiBase: 'https://your-resource.openai.azure.com',
    apiVersion: '2024-10-21',
    models: ['gpt-4o-mini', 'gpt-4o'],
    keyPlaceholder: 'Azure api-key',
  },

  // 本地部署 / 自定义
  {
    id: 'ollama',
    name: 'Ollama（本地模型 · 免 Key）',
    shortName: 'Ollama',
    group: 'local',
    apiFormat: 'ollama',
    apiBase: 'http://127.0.0.1:11434',
    models: ['qwen2.5:7b', 'deepseek-r1:7b', 'llama3.1:8b'],
    keyPlaceholder: '本地部署留空即可',
    optionalKey: true,
  },
  {
    id: 'lmstudio',
    name: 'LM Studio（本地模型 · 免 Key）',
    shortName: 'LM Studio',
    group: 'local',
    apiFormat: 'openai',
    apiBase: 'http://127.0.0.1:1234/v1',
    models: ['local-model'],
    keyPlaceholder: '本地部署留空即可',
    optionalKey: true,
  },
  {
    id: 'custom',
    name: '自定义 / 中转接口（OneAPI / NewAPI 等）',
    shortName: '自定义模型',
    group: 'local',
    apiFormat: 'openai',
    apiBase: '',
    models: [],
    keyPlaceholder: '填写服务商或中转站 API Key',
  },
];

const PROVIDER_MAP = new Map(LLM_PROVIDERS.map((p) => [p.id, p]));
const FORMAT_IDS = new Set(LLM_API_FORMATS.map((f) => f.id));

export function getLlmProvider(id) {
  return PROVIDER_MAP.get(String(id || '').trim().toLowerCase()) || PROVIDER_MAP.get('custom');
}

export function inferLlmApiFormat(apiBase = '', provider = '') {
  const prov = String(provider || '').trim().toLowerCase();
  if (prov === 'anthropic' || prov === 'claude') return 'anthropic';
  if (prov === 'gemini') return 'gemini';
  if (prov === 'azure') return 'azure';
  if (prov === 'ollama') return 'ollama';

  const base = String(apiBase || '').trim().replace(/\/+$/, '').toLowerCase();
  if (base.endsWith('/messages') || base.includes('api.anthropic.com')) return 'anthropic';
  if (
    base.includes(':generatecontent') ||
    (base.includes('generativelanguage.googleapis.com') && !base.includes('/openai'))
  ) {
    return 'gemini';
  }
  if (base.includes('.openai.azure.com') || base.includes('/openai/deployments/')) return 'azure';
  if (base.endsWith('/api/chat')) return 'ollama';
  if (base.endsWith('/responses')) return 'openai-responses';
  return 'openai';
}

export function normalizeLlmApiFormat(rawFormat, apiBase = '', provider = '') {
  const fmt = String(rawFormat || '').trim().toLowerCase();
  if (FORMAT_IDS.has(fmt)) return fmt;
  return inferLlmApiFormat(apiBase, provider);
}

export function inferLlmProvider(apiBase = '', apiFormat = '') {
  const base = String(apiBase || '').trim().toLowerCase();
  const fmt = String(apiFormat || '').trim().toLowerCase();
  if (base.includes('api.deepseek.com')) return 'deepseek';
  if (base.includes('api.openai.com')) return 'openai';
  if (base.includes('anthropic.com') || fmt === 'anthropic') return 'anthropic';
  if (base.includes('generativelanguage.googleapis.com') || fmt === 'gemini') return 'gemini';
  if (base.includes('dashscope.aliyuncs.com')) return 'qwen';
  if (base.includes('open.bigmodel.cn')) return 'zhipu';
  if (base.includes('api.moonshot.cn') || base.includes('api.moonshot.ai')) return 'moonshot';
  if (base.includes('volces.com')) return 'doubao';
  if (base.includes('siliconflow.cn') || base.includes('siliconflow.com')) return 'siliconflow';
  if (base.includes('hunyuan.cloud.tencent.com')) return 'hunyuan';
  if (base.includes('spark-api-open.xf-yun.com')) return 'spark';
  if (base.includes('qianfan.baidubce.com')) return 'qianfan';
  if (base.includes('minimax.chat') || base.includes('minimaxi.com')) return 'minimax';
  if (base.includes('lingyiwanwu.com')) return 'yi';
  if (base.includes('api.x.ai')) return 'xai';
  if (base.includes('api.groq.com')) return 'groq';
  if (base.includes('api.mistral.ai')) return 'mistral';
  if (base.includes('openrouter.ai')) return 'openrouter';
  if (base.includes('.openai.azure.com') || fmt === 'azure') return 'azure';
  if (fmt === 'ollama' || base.includes(':11434')) return 'ollama';
  if (base.includes(':1234')) return 'lmstudio';
  return 'custom';
}

export function isLocalLlmEndpoint(llm) {
  if (!llm || typeof llm !== 'object') return false;
  const fmt = String(llm.apiFormat || llm.api_format || '').trim().toLowerCase();
  const prov = String(llm.provider || '').trim().toLowerCase();
  if (fmt === 'ollama' || prov === 'ollama' || prov === 'lmstudio') return true;
  const base = String(llm.apiBase || llm.api_base || '').trim();
  if (!base) return false;
  try {
    const host = new URL(base).hostname.toLowerCase();
    return host === 'localhost' || host === '127.0.0.1' || host === '::1' || host === '0.0.0.0';
  } catch {
    return false;
  }
}

export function isLlmConfigured(llm) {
  if (!llm || typeof llm !== 'object') return false;
  const apiKey = String(llm.apiKey || llm.api_key || '').trim();
  const apiBase = String(llm.apiBase || llm.api_base || '').trim();
  const model = String(llm.model || '').trim();
  if (!apiBase || !model) return false;
  if (apiKey) return true;
  return isLocalLlmEndpoint(llm);
}

export function getLlmDisplayName(llm) {
  const provider = getLlmProvider(llm?.provider || inferLlmProvider(llm?.apiBase, llm?.apiFormat));
  return provider?.shortName || '大模型';
}
