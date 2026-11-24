import { describe, expect, it } from 'vitest';
import {
  LLM_API_FORMATS,
  LLM_PROVIDERS,
  LLM_PROVIDER_GROUPS,
  getLlmDisplayName,
  getLlmProvider,
  inferLlmApiFormat,
  inferLlmProvider,
  isLlmConfigured,
  isLocalLlmEndpoint,
  normalizeLlmApiFormat,
} from './llmProviders';

describe('llmProviders catalog & inference', () => {
  it('exposes non-empty formats, groups, and providers with custom fallback', () => {
    expect(LLM_API_FORMATS.length).toBeGreaterThanOrEqual(4);
    expect(LLM_PROVIDER_GROUPS.length).toBeGreaterThanOrEqual(2);
    expect(LLM_PROVIDERS.length).toBeGreaterThanOrEqual(5);
    expect(getLlmProvider('deepseek')?.id).toBe('deepseek');
    expect(getLlmProvider('nonexistent-id')?.id).toBe('custom');
  });

  it('infers API format from provider hints and endpoint URLs', () => {
    expect(inferLlmApiFormat('', 'anthropic')).toBe('anthropic');
    expect(inferLlmApiFormat('', 'claude')).toBe('anthropic');
    expect(inferLlmApiFormat('', 'gemini')).toBe('gemini');
    expect(inferLlmApiFormat('', 'azure')).toBe('azure');
    expect(inferLlmApiFormat('', 'ollama')).toBe('ollama');
    expect(inferLlmApiFormat('https://api.anthropic.com/v1/messages')).toBe('anthropic');
    expect(
      inferLlmApiFormat('https://generativelanguage.googleapis.com/v1beta/models/gemini:generateContent')
    ).toBe('gemini');
    expect(
      inferLlmApiFormat('https://generativelanguage.googleapis.com/v1beta/openai/')
    ).toBe('openai');
    expect(inferLlmApiFormat('https://myres.openai.azure.com/openai/deployments/gpt4')).toBe('azure');
    expect(inferLlmApiFormat('http://127.0.0.1:11434/api/chat')).toBe('ollama');
    expect(inferLlmApiFormat('https://api.openai.com/v1/responses')).toBe('openai-responses');
    expect(normalizeLlmApiFormat('unknown-fmt', 'https://api.anthropic.com')).toBe('anthropic');
    expect(normalizeLlmApiFormat('gemini', 'https://api.openai.com/v1')).toBe('gemini');
  });

  it('infers provider, detects local endpoints, and checks configured status', () => {
    expect(inferLlmProvider('https://api.deepseek.com/v1', 'openai')).toBe('deepseek');
    expect(inferLlmProvider('http://127.0.0.1:11434/v1', '')).toBe('ollama');
    expect(inferLlmProvider('https://unknown.relay.example/v1', 'anthropic')).toBe('anthropic');
    expect(inferLlmProvider('https://unknown.relay.example/v1', 'openai')).toBe('custom');

    expect(isLocalLlmEndpoint({ provider: 'ollama' })).toBe(true);
    expect(isLocalLlmEndpoint({ apiBase: 'http://localhost:1234/v1' })).toBe(true);
    expect(isLocalLlmEndpoint({ apiBase: 'https://api.openai.com/v1' })).toBe(false);

    // Cloud endpoints require apiKey + apiBase + model; local endpoints do not require apiKey
    expect(isLlmConfigured({ apiBase: 'https://api.openai.com/v1', model: 'gpt-4o', apiKey: '' })).toBe(false);
    expect(isLlmConfigured({ apiBase: 'https://api.openai.com/v1', model: 'gpt-4o', apiKey: 'sk-1' })).toBe(true);
    expect(isLlmConfigured({ provider: 'ollama', apiBase: 'http://127.0.0.1:11434', model: 'qwen2.5' })).toBe(true);
    expect(getLlmDisplayName({ provider: 'deepseek' })).toBeTruthy();
  });
});
