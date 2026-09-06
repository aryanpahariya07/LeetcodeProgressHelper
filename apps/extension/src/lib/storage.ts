/**
 * Durable local state.
 *
 * Spec §4.2 suggests IndexedDB for the queue. `chrome.storage.local` is used
 * instead: it is equally durable across restarts, is the only store a service
 * worker can touch without keeping a connection alive, and attempt events are a
 * few hundred bytes each. The requirement is durability, not a particular store.
 * If the queue ever grows past the quota, this is the one module to rewrite.
 *
 * Nothing here holds a secret except the device token, which is what it is: a
 * bearer credential the extension needs to send anything at all.
 */

import type { MonitoringState, QueuedEvent } from "./types";

const KEYS = {
  config: "config",
  queue: "queue",
  status: "status",
} as const;

export interface ExtensionConfig {
  apiBaseUrl: string;
  deviceToken: string | null;
  deviceName: string | null;
  state: MonitoringState;
  idleThresholdMs: number;
  maxBatchSize: number;
}

export const DEFAULT_CONFIG: ExtensionConfig = {
  apiBaseUrl: "http://127.0.0.1:8000/api/v1",
  deviceToken: null,
  deviceName: null,
  // Nothing is captured until the user explicitly connects. Monitoring is
  // opt-in (spec §4.4), so the default state is disconnected, not paused.
  state: "disconnected",
  idleThresholdMs: 5 * 60 * 1000,
  maxBatchSize: 100,
};

export interface StoredStatus {
  lastSyncAt: number | null;
  lastError: string | null;
  adapterHealthy: boolean;
}

const DEFAULT_STATUS: StoredStatus = {
  lastSyncAt: null,
  lastError: null,
  adapterHealthy: true,
};

async function read<T>(key: string, fallback: T): Promise<T> {
  const stored = await chrome.storage.local.get(key);
  return (stored[key] as T | undefined) ?? fallback;
}

export async function getConfig(): Promise<ExtensionConfig> {
  return { ...DEFAULT_CONFIG, ...(await read(KEYS.config, {})) };
}

export async function setConfig(patch: Partial<ExtensionConfig>): Promise<ExtensionConfig> {
  const next = { ...(await getConfig()), ...patch };
  await chrome.storage.local.set({ [KEYS.config]: next });
  return next;
}

export async function getStatus(): Promise<StoredStatus> {
  return { ...DEFAULT_STATUS, ...(await read(KEYS.status, {})) };
}

export async function setStatus(patch: Partial<StoredStatus>): Promise<StoredStatus> {
  const next = { ...(await getStatus()), ...patch };
  await chrome.storage.local.set({ [KEYS.status]: next });
  return next;
}

export async function getQueue(): Promise<QueuedEvent[]> {
  return read<QueuedEvent[]>(KEYS.queue, []);
}

async function writeQueue(queue: QueuedEvent[]): Promise<void> {
  await chrome.storage.local.set({ [KEYS.queue]: queue });
}

/**
 * Add an event to the queue, ignoring one already there.
 *
 * The `event_uuid` is generated once, when the attempt is captured, and is the
 * idempotency key end to end (invariant 7). Deduplicating here too means a
 * double-fired DOM observer cannot enqueue the same attempt twice.
 */
export async function enqueue(event: QueuedEvent): Promise<QueuedEvent[]> {
  const queue = await getQueue();
  if (queue.some((q) => q.event.event_uuid === event.event.event_uuid)) return queue;
  const next = [...queue, event];
  await writeQueue(next);
  return next;
}

export async function removeFromQueue(eventUuids: string[]): Promise<QueuedEvent[]> {
  const drop = new Set(eventUuids);
  const next = (await getQueue()).filter((q) => !drop.has(q.event.event_uuid));
  await writeQueue(next);
  return next;
}

export async function recordFailure(
  eventUuids: string[],
  error: string,
): Promise<QueuedEvent[]> {
  const bump = new Set(eventUuids);
  const next = (await getQueue()).map((q) =>
    bump.has(q.event.event_uuid)
      ? { ...q, attempts: q.attempts + 1, lastError: error }
      : q,
  );
  await writeQueue(next);
  return next;
}

/** Wipe everything. Used by disconnect, so nothing is left behind. */
export async function clearAll(): Promise<void> {
  await chrome.storage.local.clear();
}
