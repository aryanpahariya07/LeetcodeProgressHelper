/** Shared types. Mirrors the API's `AttemptEventIn` (apps/api schemas.py). */

export type Resolution =
  | "independent"
  | "after_hint"
  | "after_editorial"
  | "failed"
  | "unknown";

export type Blocker =
  | "pattern_not_recognized"
  | "pattern_known_impl_failed"
  | "edge_cases"
  | "complexity"
  | "data_structure_choice"
  | "language_api"
  | "misread_problem";

export type SubmissionOutcome =
  | "accepted"
  | "wrong_answer"
  | "runtime_error"
  | "compile_error"
  | "tle"
  | "unknown";

export type CaptureConfidence = "high" | "medium" | "low";

/**
 * A value the adapter read from the page, with how much it trusts it.
 *
 * Invariant 6: the adapter never returns a bare value. If it could not
 * determine something it says so, and the field is omitted rather than guessed.
 */
export interface Observed<T> {
  value: T | null;
  confidence: CaptureConfidence;
}

export const UNKNOWN: Observed<never> = { value: null, confidence: "low" };

export function observed<T>(
  value: T,
  confidence: CaptureConfidence = "high",
): Observed<T> {
  return { value, confidence };
}

/** One captured attempt, ready to send. */
export interface AttemptEvent {
  event_uuid: string;
  problem_slug: string;
  provider: "leetcode";
  submitted_at: string;
  started_at: string | null;
  language: string | null;
  active_seconds: number | null;
  excluded_seconds: number;
  run_count: number;
  submit_count: number;
  submission_outcome: SubmissionOutcome;
  resolution: Resolution;
  blocker: Blocker | null;
  confidence_cold_redo: number | null;
  is_resolve: boolean;
  capture_confidence: CaptureConfidence;
  raw_metadata: Record<string, unknown> | null;
}

export interface QueuedEvent {
  event: AttemptEvent;
  queuedAt: number;
  attempts: number;
  lastError: string | null;
}

export type MonitoringState = "monitoring" | "paused" | "disconnected" | "degraded";

export interface ExtensionStatus {
  state: MonitoringState;
  lastSyncAt: number | null;
  queuedCount: number;
  lastError: string | null;
  adapterHealthy: boolean;
}
