/**
 * Retry policy for batch sync (spec §4.2).
 *
 * Two rules that matter more than the curve:
 *
 * - **Only retryable failures are retried.** A 401 means the device was revoked;
 *   retrying it forever would hammer the server and hide the real problem from
 *   the user. A 400 means the payload is wrong and will stay wrong.
 * - **Jitter is not optional.** Without it, every queued batch wakes at the same
 *   moment after a server restart.
 *
 * Pure, so the schedule is testable without waiting for it.
 */

export const BASE_DELAY_MS = 2_000;
export const MAX_DELAY_MS = 15 * 60 * 1000;
export const MAX_ATTEMPTS = 8;

/** Whether a failure is worth trying again. */
export function isRetryable(status: number): boolean {
  // No response at all — offline, DNS, server down.
  if (status === 0) return true;
  // Rate limited: explicitly a "later" signal.
  if (status === 429) return true;
  // Server-side faults.
  if (status >= 500) return true;
  // Everything else in 4xx is the client's fault and will not fix itself.
  return false;
}

/**
 * Delay before attempt `n` (1-based), exponential with full jitter.
 *
 * `random` is injectable so tests can pin it.
 */
export function delayFor(attempt: number, random: () => number = Math.random): number {
  const exponential = Math.min(MAX_DELAY_MS, BASE_DELAY_MS * 2 ** Math.max(0, attempt - 1));
  // Full jitter: uniform in [0, exponential]. Spreads a thundering herd better
  // than a fixed fraction does.
  return Math.floor(random() * exponential);
}

/** Whether to keep trying, or give up and surface it. */
export function shouldRetry(attempt: number, status: number): boolean {
  return attempt < MAX_ATTEMPTS && isRetryable(status);
}
