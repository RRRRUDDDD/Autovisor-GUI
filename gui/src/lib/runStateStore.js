import { createContext, useContext, useSyncExternalStore } from 'react';
import { LOG_LIMIT } from './logPresentation';

const NOOP_SUBSCRIBE = () => () => {};
const TERMINAL_STATUSES = new Set(['completed', 'time_limit', 'error', 'cancelled']);

function sameShutdown(a, b) {
  if (a === b) return true;
  if (!a || !b) return false;
  return (
    a.state === b.state &&
    a.delaySeconds === b.delaySeconds &&
    a.scheduledAt === b.scheduledAt &&
    a.shutdownAt === b.shutdownAt &&
    a.error === b.error
  );
}

function lastLogSeq(logs) {
  if (!Array.isArray(logs) || logs.length === 0) return 0;
  const last = logs[logs.length - 1];
  return typeof last?.seq === 'number' ? last.seq : 0;
}

function isStaleSnapshot(prev, incoming) {
  if (!prev || typeof prev !== 'object' || !incoming || typeof incoming !== 'object') {
    return false;
  }
  const prevRunId = Number(prev.runId || 0);
  const incRunId = Number(incoming.runId || 0);
  if (incRunId > 0 && prevRunId > incRunId) {
    return true;
  }
  const sameRun =
    incRunId > 0 && prevRunId > 0
      ? prevRunId === incRunId
      : incoming.startedAt != null && prev.startedAt === incoming.startedAt;
  if (!sameRun) {
    return false;
  }
  const prevRev = Number(prev.revision || 0);
  const incRev = Number(incoming.revision || 0);
  if (incRev > 0 && prevRev > incRev) {
    return true;
  }
  if (
    TERMINAL_STATUSES.has(prev.status) &&
    (incoming.status === 'running' || incoming.status === 'stopping')
  ) {
    return true;
  }
  return lastLogSeq(prev.logs) > lastLogSeq(incoming.logs);
}

export function applyRunStatePayload(prev, payload) {
  if (payload === null || payload === undefined) {
    return null;
  }
  if (typeof payload !== 'object') {
    return prev;
  }

  if (payload.__delta === 'log') {
    const incoming = Array.isArray(payload.entries) ? payload.entries : [];
    const base = prev && typeof prev === 'object' ? prev : { status: 'idle', logs: [] };
    const prevRunId = Number(base.runId || 0);
    const incRunId = Number(payload.runId || 0);
    if (incRunId > 0 && prevRunId > 0 && incRunId < prevRunId) {
      return prev;
    }
    const existingLogs = Array.isArray(base.logs) ? base.logs : [];
    let lastSeq = lastLogSeq(existingLogs);
    const toAppend = [];
    for (const entry of incoming) {
      if (entry && typeof entry.seq === 'number' && entry.seq > lastSeq) {
        lastSeq = entry.seq;
        toAppend.push(entry);
      }
    }
    const nextTruncated = Boolean(base.logsTruncated || payload.logsTruncated);
    const nextRunId = incRunId > prevRunId ? incRunId : prevRunId;
    const nextRevision = Math.max(Number(base.revision || 0), Number(payload.revision || 0));
    if (
      toAppend.length === 0 &&
      nextTruncated === Boolean(base.logsTruncated) &&
      nextRunId === prevRunId &&
      nextRevision === Number(base.revision || 0)
    ) {
      return prev;
    }
    let nextLogs = toAppend.length > 0 ? existingLogs.concat(toAppend) : existingLogs;
    let truncated = nextTruncated;
    if (nextLogs.length > LOG_LIMIT) {
      nextLogs = nextLogs.slice(nextLogs.length - LOG_LIMIT);
      truncated = true;
    }
    return {
      ...base,
      ...(nextRunId > 0 ? { runId: nextRunId } : {}),
      ...(nextRevision > 0 ? { revision: nextRevision } : {}),
      logs: nextLogs,
      logsTruncated: truncated,
    };
  }

  if (payload.__delta === 'state') {
    const { __delta: _ignore, ...patch } = payload;
    const base = prev && typeof prev === 'object' ? prev : { logs: [] };
    const prevRunId = Number(base.runId || 0);
    const incRunId = Number(patch.runId || 0);
    if (incRunId > 0 && prevRunId > 0) {
      if (incRunId < prevRunId) return prev;
      const prevRev = Number(base.revision || 0);
      const incRev = Number(patch.revision || 0);
      if (incRunId === prevRunId && incRev > 0 && prevRev > incRev) {
        return prev;
      }
    }
    const nextShutdown = sameShutdown(base.shutdown ?? null, patch.shutdown ?? null)
      ? (base.shutdown ?? null)
      : (patch.shutdown ?? null);
    const nextRevision = Math.max(Number(base.revision || 0), Number(patch.revision || 0));
    return {
      ...base,
      ...patch,
      ...(nextRevision > 0 ? { revision: nextRevision } : {}),
      shutdown: nextShutdown,
      // 保留既有 logs 引用，避免进度刷新导致 memo(ExecutionLog) 失效
      logs: Array.isArray(base.logs) ? base.logs : [],
      logsTruncated: Boolean(base.logsTruncated || patch.logsTruncated),
    };
  }

  // 全量快照（get_run_state / start / stop / snapshot / 兼容旧版全量 state 事件）
  if (isStaleSnapshot(prev, payload)) {
    return prev;
  }
  const nextShutdown = sameShutdown(prev?.shutdown ?? null, payload.shutdown ?? null)
    ? (prev?.shutdown ?? null)
    : (payload.shutdown ?? null);
  return {
    ...payload,
    shutdown: nextShutdown,
  };
}

export function createRunStateStore(initialState = null) {
  let state = initialState;
  const listeners = new Set();

  const notify = () => {
    for (const listener of listeners) {
      listener();
    }
  };

  return {
    getSnapshot: () => state,
    subscribe: (listener) => {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    applyPayload: (payload) => {
      const next = applyRunStatePayload(state, payload);
      if (next !== state) {
        state = next;
        notify();
      }
      return state;
    },
    reset: (nextState = null) => {
      state = nextState;
      notify();
    },
  };
}

export const RunStateContext = createContext(null);

export function useRunStateStore() {
  return useContext(RunStateContext);
}

export function useRunStateSelector(selector) {
  const store = useContext(RunStateContext);
  return useSyncExternalStore(
    store ? store.subscribe : NOOP_SUBSCRIBE,
    () => selector(store ? store.getSnapshot() : null)
  );
}

export function useRunState() {
  return useRunStateSelector((s) => s);
}
