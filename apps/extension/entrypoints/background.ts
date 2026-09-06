/**
 * Background service worker: owns the queue and all network access.
 *
 * The content script never talks to the server. It observes the page and hands
 * finished attempts here, so a page navigation mid-sync cannot lose an event —
 * the queue outlives the tab.
 */

import { sendBatch } from "../src/lib/api";
import { delayFor, shouldRetry } from "../src/lib/backoff";
import * as store from "../src/lib/storage";
import type { AttemptEvent, ExtensionStatus } from "../src/lib/types";

const SYNC_ALARM = "dsa-coach-sync";
const SYNC_PERIOD_MINUTES = 1;

export default defineBackground(() => {
  chrome.alarms.create(SYNC_ALARM, { periodInMinutes: SYNC_PERIOD_MINUTES });

  chrome.alarms.onAlarm.addListener((alarm) => {
    if (alarm.name === SYNC_ALARM) void drainQueue();
  });

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    void handleMessage(message).then(sendResponse);
    // Keeps the message channel open for the async reply.
    return true;
  });
});

type Message =
  | { type: "capture"; event: AttemptEvent }
  | { type: "adapter_health"; healthy: boolean; missing: string[] }
  | { type: "get_status" }
  | { type: "sync_now" };

async function handleMessage(message: Message): Promise<unknown> {
  switch (message.type) {
    case "capture": {
      const config = await store.getConfig();
      // Paused and disconnected both mean "do not record". Dropping it here
      // rather than in the content script keeps one source of truth.
      if (config.state !== "monitoring") return { queued: false, reason: config.state };
      await store.enqueue({
        event: message.event,
        queuedAt: Date.now(),
        attempts: 0,
        lastError: null,
      });
      void drainQueue();
      return { queued: true };
    }

    case "adapter_health": {
      await store.setStatus({ adapterHealthy: message.healthy });
      if (!message.healthy) {
        const config = await store.getConfig();
        // Surface the breakage instead of silently recording nothing (spec §4.3).
        if (config.state === "monitoring") await store.setConfig({ state: "degraded" });
      }
      return { ok: true };
    }

    case "sync_now":
      return drainQueue();

    case "get_status":
      return currentStatus();
  }
}

export async function currentStatus(): Promise<ExtensionStatus> {
  const [config, status, queue] = await Promise.all([
    store.getConfig(),
    store.getStatus(),
    store.getQueue(),
  ]);
  return {
    state: config.state,
    lastSyncAt: status.lastSyncAt,
    queuedCount: queue.length,
    lastError: status.lastError,
    adapterHealthy: status.adapterHealthy,
  };
}

let draining = false;

async function drainQueue(): Promise<{ sent: number }> {
  // One drain at a time. Two overlapping drains would send the same batch
  // twice — harmless, because ingestion is idempotent, but wasteful and it
  // muddles the retry counters.
  if (draining) return { sent: 0 };
  draining = true;

  try {
    const config = await store.getConfig();
    if (!config.deviceToken) return { sent: 0 };

    const queue = await store.getQueue();
    if (queue.length === 0) return { sent: 0 };

    const batch = queue.slice(0, config.maxBatchSize);
    const outcome = await sendBatch(
      config.apiBaseUrl,
      config.deviceToken,
      batch.map((q) => q.event),
    );

    if (outcome.ok) {
      await store.removeFromQueue(outcome.rejectedUuids);
      await store.setStatus({ lastSyncAt: Date.now(), lastError: null });
      return { sent: outcome.accepted };
    }

    if (outcome.status === 401 || outcome.status === 403) {
      // The device was revoked. Stop trying, and say so plainly rather than
      // retrying into a wall.
      await store.setConfig({ state: "disconnected", deviceToken: null });
      await store.setStatus({
        lastError: "This device was disconnected. Pair it again to resume.",
      });
      return { sent: 0 };
    }

    const uuids = batch.map((q) => q.event.event_uuid);
    await store.recordFailure(uuids, outcome.error ?? "Sync failed.");
    await store.setStatus({ lastError: outcome.error });

    const worst = Math.max(...batch.map((q) => q.attempts + 1));
    if (!shouldRetry(worst, outcome.status)) {
      // Out of retries: drop the batch rather than blocking everything behind
      // it forever. The attempts are lost, and the UI says so.
      await store.removeFromQueue(uuids);
      await store.setStatus({
        lastError: `Gave up on ${uuids.length} event(s) after repeated failures.`,
      });
      return { sent: 0 };
    }

    setTimeout(() => void drainQueue(), delayFor(worst));
    return { sent: 0 };
  } finally {
    draining = false;
  }
}
