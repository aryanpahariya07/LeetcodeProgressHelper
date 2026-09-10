/**
 * Monitoring-state decisions (spec §4.3).
 *
 * Two rules, pulled out of the service worker because both were wrong in ways
 * that were invisible from the outside: the extension looked connected, the API
 * was reachable, the queue was empty, and every attempt was being thrown away.
 * Pure functions so the rules can be asserted rather than inferred from
 * behaviour.
 */

import type { MonitoringState } from "./types";

/**
 * States in which an attempt is discarded rather than queued.
 *
 * Both are user decisions. Notably absent is `degraded`.
 */
const BLOCKED: ReadonlySet<MonitoringState> = new Set(["paused", "disconnected"]);

/**
 * Whether an attempt should be recorded in this state.
 *
 * `degraded` records. It means the adapter could not find some anchor, so the
 * event carries `capture_confidence: low` and omits what could not be read —
 * which is worth much more than nothing, and is what §4.3 requires: degrade
 * "rather than silently capturing nothing".
 *
 * The original check was `state !== "monitoring"`, which also caught
 * `degraded` and did precisely the opposite.
 */
export function shouldCapture(state: MonitoringState): boolean {
  return !BLOCKED.has(state);
}

/**
 * The state after a health report.
 *
 * Degradation must be reversible. Without the recovery branch, one health check
 * failing — on a page where the console panel has not rendered yet, say — left
 * the extension degraded permanently, with no route back short of re-pairing.
 *
 * `paused` and `disconnected` are never touched: those are the user's choices,
 * and a health check does not get to overrule them in either direction.
 */
export function stateAfterHealth(
  current: MonitoringState,
  healthy: boolean,
): MonitoringState {
  if (!healthy) return current === "monitoring" ? "degraded" : current;
  return current === "degraded" ? "monitoring" : current;
}
