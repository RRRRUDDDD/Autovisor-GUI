import catalog from '../../../resources/llm_providers.json';

export const LLM_API_FORMATS = catalog.apiFormats;
export const LLM_PROVIDER_GROUPS = catalog.providerGroups;
export const LLM_PROVIDERS = catalog.providers;

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
  for (const provider of LLM_PROVIDERS) {
    const hosts = provider.matchHosts || [];
    if (hosts.some((h) => base.includes(h.toLowerCase()))) {
      return provider.id;
    }
  }
  for (const provider of LLM_PROVIDERS) {
    const formats = provider.matchFormats || [];
    if (formats.some((f) => fmt === f.toLowerCase())) {
      return provider.id;
    }
  }
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
