import { describe, it, expect } from 'vitest';
import {
  detectCourseType,
  extractCourseUrlsFromText,
  isNotificationConfigured,
  isValidCourseUrl,
  isValidLlmApiBase,
  isValidProxyUrl,
  normalizeConfig,
  configSnapshot,
} from './configModel';
import { filterLogs, formatLogsForCopy, redactSensitive } from './logPresentation';
import { isLlmConfigured, isLocalLlmEndpoint } from './llmProviders';

describe('configModel', () => {
  it('detects supported Zhihuishu course URL types', () => {
    expect(detectCourseType('https://studyvideoh5.zhihuishu.com/stuStudy?recruitAndCourseId=abc').key).toBe('shared');
    expect(detectCourseType('https://fusioncourseh5.zhihuishu.com/stuStudy?id=1').key).toBe('fusion');
    expect(detectCourseType('https://studywisdomh5.zhihuishu.com/course').key).toBe('hike');
    expect(detectCourseType('https://wisdom-mooc.zhihuishu.com/play/123').key).toBe('ai-mooc');
    expect(detectCourseType('https://ai-smart-course-student-pro.zhihuishu.com/learnPage?courseId=1').key).toBe('ai-smart');
  });

  it('validates proxy URLs and normalizes notification settings', () => {
    expect(isValidProxyUrl('')).toBe(true);
    expect(isValidProxyUrl('http://127.0.0.1:7890')).toBe(true);
    expect(isValidProxyUrl('socks5://127.0.0.1:1080')).toBe(true);
    expect(isValidProxyUrl('127.0.0.1:7890')).toBe(false);

    const cfg = normalizeConfig({
      proxy: '  http://127.0.0.1:7890  ',
      notificationProvider: 'telegram',
      notificationUrl: ' https://api.telegram.org/bot123:abc/sendMessage ',
      notificationTgChatId: ' 987654321 ',
      notifyOnVerify: false,
    });
    expect(cfg.proxy).toBe('http://127.0.0.1:7890');
    expect(cfg.notificationProvider).toBe('Telegram');
    expect(cfg.notificationUrl).toBe('https://api.telegram.org/bot123:abc/sendMessage');
    expect(cfg.notificationTgChatId).toBe('987654321');
    expect(cfg.notifyOnVerify).toBe(false);
    expect(cfg.notifyOnFinish).toBe(true);
    expect(cfg.notifyOnError).toBe(true);
    expect(isNotificationConfigured(cfg)).toBe(true);
    expect(normalizeConfig({}).notificationProvider).toBe('Windows');
    expect(isNotificationConfigured(normalizeConfig({}))).toBe(true);
    expect(normalizeConfig({ notificationProvider: '' }).notificationProvider).toBe('');
    expect(isNotificationConfigured({ notificationProvider: '' })).toBe(false);
    expect(isNotificationConfigured({ notificationProvider: 'Windows' })).toBe(true);
    expect(isNotificationConfigured({ notificationProvider: 'ServerChan', notificationUrl: '' })).toBe(false);
    expect(
      isNotificationConfigured({
        notificationProvider: 'Telegram',
        notificationUrl: 'https://api.telegram.org/bot123/sendMessage',
        notificationTgChatId: '',
      })
    ).toBe(false);
  });

  it('extracts and deduplicates course URLs from batch paste and config.ini lines', () => {
    const raw = `
      ; comment line
      URL1 = https://studyvideoh5.zhihuishu.com/stuStudy?id=111
      https://wisdom-mooc.zhihuishu.com/play/222
      https://studyvideoh5.zhihuishu.com/stuStudy?id=111
      not-a-url
    `;
    const { valid, invalid } = extractCourseUrlsFromText(raw);
    expect(valid).toEqual([
      'https://studyvideoh5.zhihuishu.com/stuStudy?id=111',
      'https://wisdom-mooc.zhihuishu.com/play/222',
    ]);
    expect(invalid).toEqual(['not-a-url']);
    expect(isValidCourseUrl(valid[0])).toBe(true);
    expect(isValidCourseUrl('http://insecure.example.com')).toBe(false);
  });

  it('clamps speed and generates stable dirty snapshots', () => {
    const a = normalizeConfig({ limitSpeed: 2.5, limitMaxTime: 30 });
    expect(a.limitSpeed).toBe(1.8);
    const b = normalizeConfig({ limitSpeed: 1.8, limitMaxTime: 30 });
    expect(configSnapshot(a)).toBe(configSnapshot(b));
  });

  it('normalizes LLM quiz settings, mastery options, and preserves CLI QR fields', () => {
    expect(isValidLlmApiBase('')).toBe(true);
    expect(isValidLlmApiBase('https://api.deepseek.com')).toBe(true);
    expect(isValidLlmApiBase('api.deepseek.com')).toBe(false);

    const cfg = normalizeConfig({
      qrLogin: true,
      qrTerminal: true,
      qrUnicode: true,
      qrSaveDir: '  C:\\qr  ',
      enableExamApi: false,
      enableReviewExamApi: true,
      enableZhidaoQuiz: false,
      enableMastery: false,
      masteryTarget: 85.4,
      llm: {
        api_key: ' sk-llm-demo ',
        api_base: ' https://api.example.com/v1 ',
        model: ' deepseek-reasoner ',
        auto_submit: true,
      },
    });
    expect(cfg.qrLogin).toBe(true);
    expect(cfg.qrTerminal).toBe(true);
    expect(cfg.qrUnicode).toBe(true);
    expect(cfg.qrSaveDir).toBe('C:\\qr');
    expect(cfg.enableExamApi).toBe(false);
    expect(cfg.enableReviewExamApi).toBe(true);
    expect(cfg.enableZhidaoQuiz).toBe(false);
    expect(cfg.enableMastery).toBe(false);
    expect(cfg.masteryTarget).toBe(85);
    expect(normalizeConfig({}).enableHideWindow).toBe(true);
    expect(normalizeConfig({ enableHideWindow: false }).enableHideWindow).toBe(false);
    expect(normalizeConfig({}).shutdownOnComplete).toBe(false);
    expect(normalizeConfig({ shutdownOnComplete: true }).shutdownOnComplete).toBe(true);
    expect(normalizeConfig({}).limitMaxTime).toBe(0);
    expect(normalizeConfig({ limitMaxTime: -5 }).limitMaxTime).toBe(0);
    expect(normalizeConfig({}).enableExamApi).toBe(true);
    expect(normalizeConfig({}).enableReviewExamApi).toBe(false);
    expect(normalizeConfig({}).enableZhidaoQuiz).toBe(true);
    expect(normalizeConfig({}).reviewMode).toBe(false);
    expect(normalizeConfig({ reviewMode: true }).reviewMode).toBe(true);
    expect(cfg.llm).toEqual({
      provider: 'custom',
      apiFormat: 'openai',
      apiKey: 'sk-llm-demo',
      apiBase: 'https://api.example.com/v1',
      model: 'deepseek-reasoner',
      apiVersion: '',
      autoSubmit: true,
    });

    const claudeCfg = normalizeConfig({
      llm: {
        api_key: 'sk-ant-demo',
        api_base: 'https://api.anthropic.com',
        model: 'claude-sonnet-4-20250514',
      },
    });
    expect(claudeCfg.llm.provider).toBe('anthropic');
    expect(claudeCfg.llm.apiFormat).toBe('anthropic');
    expect(isLlmConfigured(claudeCfg.llm)).toBe(true);

    const ollamaCfg = normalizeConfig({
      llm: {
        provider: 'ollama',
        api_format: 'ollama',
        api_key: '',
        api_base: 'http://127.0.0.1:11434',
        model: 'qwen2.5:7b',
      },
    });
    expect(isLocalLlmEndpoint(ollamaCfg.llm)).toBe(true);
    expect(isLlmConfigured(ollamaCfg.llm)).toBe(true);
    expect(isLlmConfigured(normalizeConfig({}).llm)).toBe(false);
  });
});

describe('logPresentation', () => {
  it('filters by level and keyword and redacts sensitive fields on copy', () => {
    const logs = [
      { seq: 1, timestamp: 1700000000, level: 'info', message: '手机号 13812345678 登录成功' },
      { seq: 2, timestamp: 1700000005, level: 'warning', message: '出现安全验证' },
      { seq: 3, timestamp: 1700000010, level: 'error', message: 'api_key=sk-1234567890abcdef 请求失败' },
    ];
    expect(filterLogs(logs, 'errors', '').length).toBe(1);
    expect(filterLogs(logs, 'warnings', '').length).toBe(2);
    expect(filterLogs(logs, 'all', '安全验证').length).toBe(1);

    const copied = formatLogsForCopy(logs);
    expect(copied).toContain('138****5678');
    expect(copied).not.toContain('13812345678');
    expect(copied).not.toContain('sk-1234567890abcdef');
    expect(redactSensitive('password=mysecret')).toContain('***REDACTED***');
    expect(redactSensitive('https://api.day.app/MySecretDeviceKey123/hi')).toContain(
      'https://api.day.app/***REDACTED***/hi'
    );
    expect(redactSensitive('https://sctapi.ftqq.com/SCT123456.send')).toContain(
      'https://sctapi.ftqq.com/***REDACTED***'
    );
    expect(redactSensitive('https://qmsg.zendee.cn/send/abcdef1234')).toContain(
      'https://qmsg.zendee.cn/send/***REDACTED***'
    );
    expect(redactSensitive('https://api.telegram.org/bot123:SECRET/sendMessage')).toContain(
      'https://api.telegram.org/bot***REDACTED***/sendMessage'
    );
    expect(redactSensitive('tg_chat_id=987654321')).toContain('***REDACTED***');
    expect(redactSensitive('proxy=http://alice:s3cret@127.0.0.1:7890')).toContain(
      'http://***:***@127.0.0.1:7890'
    );
    expect(redactSensitive('proxy=http://alice:s3cret@127.0.0.1:7890')).not.toContain('s3cret');
  });
});
