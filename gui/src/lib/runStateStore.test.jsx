import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, render, waitFor } from '@testing-library/react';
import { applyRunStatePayload } from './runStateStore';
import { tauriApi } from './tauriApi';
import { defaultConfig } from './configModel';
import App from '../App';

describe('T2.3 runStateStore delta protocol compatibility', () => {
  it('merges snapshot, state delta (preserving logs reference), log delta (dedup & cap), and legacy full state', () => {
    const snap = applyRunStatePayload(null, {
      status: 'running',
      mode: 'study',
      stage: '初始化',
      lessonProgress: 0,
      shutdown: null,
      logs: [{ seq: 1, timestamp: 100, level: 'info', message: 'boot' }],
      logsTruncated: false,
    });
    expect(snap.status).toBe('running');
    expect(snap.logs).toHaveLength(1);

    // State delta preserves exact logs array reference
    const afterProgress = applyRunStatePayload(snap, {
      __delta: 'state',
      status: 'running',
      mode: 'study',
      stage: '播放视频',
      lessonProgress: 42,
      shutdown: null,
      logsTruncated: false,
    });
    expect(afterProgress.lessonProgress).toBe(42);
    expect(afterProgress.logs).toBe(snap.logs);

    // Log delta deduplicates seq <= lastSeq and appends new entries
    const afterLog = applyRunStatePayload(afterProgress, {
      __delta: 'log',
      entries: [
        { seq: 1, timestamp: 100, level: 'info', message: 'dup' },
        { seq: 2, timestamp: 101, level: 'info', message: 'playing' },
      ],
      logsTruncated: false,
    });
    expect(afterLog.logs).toHaveLength(2);
    expect(afterLog.logs[1].message).toBe('playing');
    expect(afterLog.lessonProgress).toBe(42);

    // Legacy full state replaces everything
    const legacy = applyRunStatePayload(afterLog, {
      status: 'completed',
      mode: 'study',
      stage: '已结束',
      lessonProgress: 100,
      shutdown: null,
      logs: [{ seq: 9, timestamp: 200, level: 'info', message: 'done' }],
      logsTruncated: false,
    });
    expect(legacy.status).toBe('completed');
    expect(legacy.logs).toHaveLength(1);
    expect(legacy.logs[0].seq).toBe(9);
  });

  it('ignores stale HTTP start_task snapshot when SSE deltas already advanced logs or reached terminal status', () => {
    const afterStartSnap = applyRunStatePayload(null, {
      runId: 2,
      revision: 1,
      status: 'running',
      mode: 'import_cookies',
      startedAt: 500,
      logs: [{ seq: 1, timestamp: 500, level: 'info', message: 'init' }],
      logsTruncated: false,
    });
    const afterLog = applyRunStatePayload(afterStartSnap, {
      __delta: 'log',
      runId: 2,
      revision: 2,
      entries: [{ seq: 2, timestamp: 501, level: 'info', message: 'imported' }],
      logsTruncated: false,
    });
    const afterDone = applyRunStatePayload(afterLog, {
      __delta: 'state',
      runId: 2,
      revision: 3,
      status: 'completed',
      mode: 'import_cookies',
      startedAt: 500,
      finishedAt: 502,
      stage: '已完成',
      logsTruncated: false,
    });

    // Late HTTP start_task response arrives after SSE already advanced
    const afterStaleHttp = applyRunStatePayload(afterDone, {
      runId: 2,
      revision: 1,
      status: 'running',
      mode: 'import_cookies',
      startedAt: 500,
      logs: [{ seq: 1, timestamp: 500, level: 'info', message: 'init' }],
      logsTruncated: false,
    });
    expect(afterStaleHttp).toBe(afterDone);
    expect(afterStaleHttp.status).toBe('completed');
    expect(afterStaleHttp.logs).toHaveLength(2);
  });

  it('resets previous run logs and revision if a newer runId delta arrives before its snapshot', () => {
    const run2 = applyRunStatePayload(null, {
      runId: 2,
      revision: 8,
      status: 'completed',
      mode: 'study',
      logs: [
        { seq: 1, timestamp: 100, level: 'info', message: 'old-1' },
        { seq: 5, timestamp: 105, level: 'info', message: 'old-5' },
      ],
      logsTruncated: true,
    });

    const run3Log = applyRunStatePayload(run2, {
      __delta: 'log',
      runId: 3,
      revision: 1,
      entries: [{ seq: 1, timestamp: 200, level: 'info', message: 'new-run-start' }],
      logsTruncated: false,
    });
    expect(run3Log.runId).toBe(3);
    expect(run3Log.revision).toBe(1);
    expect(run3Log.logsTruncated).toBe(false);
    expect(run3Log.logs).toEqual([
      { seq: 1, timestamp: 200, level: 'info', message: 'new-run-start' },
    ]);

    const run4State = applyRunStatePayload(run3Log, {
      __delta: 'state',
      runId: 4,
      revision: 1,
      status: 'running',
      mode: 'study',
      stage: '初始化',
      logsTruncated: false,
    });
    expect(run4State.runId).toBe(4);
    expect(run4State.revision).toBe(1);
    expect(run4State.logs).toEqual([]);
  });
});

describe('T2.3 App SSE transitions and cookie import deduplication', () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it('only refreshes getRuntimeInfo on running -> non-running transition and does not double-trigger import_cookies', async () => {
    let emitRunState;
    const runtime = {
      backendPhase: 'ready',
      backendError: '',
      baseDir: 'C:\\Autovisor\\userdata',
      configPath: 'C:\\Autovisor\\userdata\\config.ini',
      cookieExists: false,
      cookieCount: 0,
      cookieModifiedAt: null,
      version: '1.1.0',
      backendVersion: '3.20.0(Autovisor GUI Edition)',
      closeBehavior: 'ask',
      detectedBrowsers: [],
      session: null,
      savedLogin: null,
    };

    const getRuntimeSpy = vi.spyOn(tauriApi, 'getRuntimeInfo').mockResolvedValue(runtime);
    vi.spyOn(tauriApi, 'loadConfig').mockResolvedValue(defaultConfig());
    vi.spyOn(tauriApi, 'getRunState').mockResolvedValue({
      status: 'idle',
      mode: 'study',
      shutdown: null,
      logs: [],
    });
    vi.spyOn(tauriApi, 'listenRunState').mockImplementation(async (handler) => {
      emitRunState = handler;
      return () => {};
    });
    vi.spyOn(tauriApi, 'listenBackendStatus').mockResolvedValue(() => {});
    vi.spyOn(tauriApi, 'listenLoginStatus').mockResolvedValue(() => {});
    vi.spyOn(tauriApi, 'listenCloseRequested').mockResolvedValue(() => {});
    vi.spyOn(tauriApi, 'checkForUpdate').mockResolvedValue({ ok: true, hasUpdate: false });

    render(<App />);
    await waitFor(() => expect(emitRunState).toBeTypeOf('function'));
    const initialCalls = getRuntimeSpy.mock.calls.length;

    // Non-running -> non-running (idle -> idle) must NOT trigger getRuntimeInfo
    await act(async () => {
      emitRunState({ __delta: 'state', status: 'idle', mode: 'study', stage: '就绪' });
    });
    expect(getRuntimeSpy.mock.calls.length).toBe(initialCalls);

    // Running -> log delta must NOT trigger getRuntimeInfo
    await act(async () => {
      emitRunState({ status: 'running', mode: 'study', stage: '初始化', logs: [] });
      emitRunState({
        __delta: 'log',
        entries: [{ seq: 1, timestamp: 100, level: 'info', message: 'tick' }],
      });
    });
    expect(getRuntimeSpy.mock.calls.length).toBe(initialCalls);

    // Running -> completed DOES trigger getRuntimeInfo exactly once
    await act(async () => {
      emitRunState({ __delta: 'state', status: 'completed', mode: 'study', stage: '已结束' });
    });
    await waitFor(() => expect(getRuntimeSpy.mock.calls.length).toBe(initialCalls + 1));

    // Repeated completed delta does NOT trigger getRuntimeInfo again
    await act(async () => {
      emitRunState({ __delta: 'state', status: 'completed', mode: 'study', stage: '已结束' });
    });
    expect(getRuntimeSpy.mock.calls.length).toBe(initialCalls + 1);

    // import_cookies completion followed immediately by trailing log delta triggers getRuntimeInfo only once
    await act(async () => {
      emitRunState({
        runId: 10,
        revision: 1,
        status: 'running',
        mode: 'import_cookies',
        startedAt: 900,
        stage: '初始化',
        logs: [{ seq: 1, timestamp: 900, level: 'info', message: 'start import' }],
      });
    });
    await act(async () => {
      emitRunState({
        __delta: 'state',
        runId: 10,
        revision: 2,
        status: 'completed',
        mode: 'import_cookies',
        startedAt: 900,
        finishedAt: 901,
        stage: '已结束',
      });
      emitRunState({
        __delta: 'log',
        runId: 10,
        revision: 2,
        entries: [{ seq: 2, timestamp: 901, level: 'info', message: 'done import' }],
        logsTruncated: false,
      });
    });
    await waitFor(() => expect(getRuntimeSpy.mock.calls.length).toBe(initialCalls + 2));

    // Another trailing log delta after import_cookies completed must NOT re-trigger getRuntimeInfo
    await act(async () => {
      emitRunState({
        __delta: 'log',
        runId: 10,
        revision: 3,
        entries: [{ seq: 3, timestamp: 902, level: 'info', message: 'trailing' }],
        logsTruncated: false,
      });
    });
    expect(getRuntimeSpy.mock.calls.length).toBe(initialCalls + 2);
  });
});
