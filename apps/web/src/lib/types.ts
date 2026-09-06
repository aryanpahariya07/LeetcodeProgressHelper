/** Shapes returned by the API. Mirrors apps/api/src/dsa_coach/schemas.py. */

export type Level = "beginner" | "intermediate" | "advanced";
export type Difficulty = "easy" | "medium" | "hard";

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
export type PlanStatus = "provisional" | "active" | "superseded" | "rolled_back";

export interface Health {
  status: "ok";
  version: string;
  database: "ok" | "unavailable";
  catalogue_problems: number;
}

export interface Problem {
  id: string;
  slug: string;
  title: string;
  url: string;
  difficulty: Difficulty;
  rating: number;
  rating_rd: number;
}

export interface PlanItem {
  id: string;
  block_id: string;
  sequence: number;
  item_type: "practice" | "resolve" | "assessment" | "learn";
  role: "weakness" | "interleaved" | "retention";
  target_minutes: number;
  status: "pending" | "done" | "skipped";
  problem: Problem | null;
}

export interface Plan {
  id: string;
  version: number;
  status: PlanStatus;
  summary: string;
  generation_context: Record<string, unknown>;
  items: PlanItem[];
}

export interface Today {
  plan: Plan | null;
  block_id: string | null;
  items: PlanItem[];
  total_target_minutes: number;
  is_provisional: boolean;
}

export interface User {
  id: string;
  display_name: string;
  timezone: string;
  preferred_language: string;
}

export interface EventResult {
  event_uuid: string;
  status: "accepted" | "duplicate" | "invalid";
  attempt_id: string | null;
  error: string | null;
}

export interface Attempt {
  id: string;
  problem: Problem;
  resolution: Resolution;
  blocker: Blocker | null;
  confidence_cold_redo: number | null;
  language: string | null;
  started_at: string | null;
  submitted_at: string;
  active_seconds: number | null;
  excluded_seconds: number;
  run_count: number;
  submit_count: number;
  submission_outcome: SubmissionOutcome;
  is_resolve: boolean;
  timed: boolean;
  source: string;
  capture_confidence: CaptureConfidence;
  amended_at: string | null;
  notes: string | null;
}

export type ReadinessBand =
  | "calibrating"
  | "not_ready"
  | "developing"
  | "approaching"
  | "ready";

export interface PatternReadiness {
  pattern_id: string;
  slug: string;
  name: string;
  band: ReadinessBand;
  calibrated: boolean;
  evidence_count: number;
  estimate: number;
  uncertainty: number;
}

export interface ReadinessReport {
  model_version: string;
  patterns: PatternReadiness[];
  calibrated_count: number;
  total_count: number;
  disclaimer: string;
}

export interface Retention {
  tracked: number;
  due: number;
  lapses: number;
}

export interface Unlock {
  slug: string;
  unlocked: boolean;
  blocked_by: string[];
  reason: string;
}

export interface TriggerBatch {
  id: string;
  relevant_count: number;
  material: boolean;
  reasons: string[];
  outcome: "no_change" | "prescribed" | "pending_agent";
  explanation: string;
  evaluated_at: string;
}

export interface BlockResult {
  plan: Plan;
  focus_patterns: string[];
  locked_patterns: string[];
  total_minutes: number;
  budget_minutes: number;
  shortfalls: string[];
}

export interface PairingCode {
  code: string;
  expires_at: string;
  expires_in_seconds: number;
}

export interface DeviceInfo {
  id: string;
  name: string;
  kind: string;
  scopes: string[];
  created_at: string;
  last_seen_at: string | null;
  revoked_at: string | null;
  active: boolean;
}

export interface Placement {
  complete: boolean;
  attempts: number;
  max_attempts: number;
  remaining: number;
  covered: number;
  calibrated: number;
  target: number;
  reason: string;
}

export interface Violation {
  kind: string;
  detail: string;
}

export interface CoachRun {
  run_id: string;
  runtime: string;
  model: string | null;
  status: "succeeded" | "failed" | "unavailable";
  used_fallback: boolean;
  validation: "accepted" | "clamped" | "rejected" | null;
  violations: Violation[];
  diagnosis: string | null;
  message: string;
  plan: Plan;
}

export interface ConsentState {
  decision: "once" | "always" | "never" | null;
  needs_prompt: boolean;
  disclosure: string;
  version: string;
  stored_snippets: number;
}

export interface ConsentResult {
  decision: "once" | "always" | "never";
  deleted_snippets: number;
  message: string;
}

export interface Teaching {
  ok: boolean;
  kind: "hint" | "diagnosis" | "review" | "mock";
  text: string;
  hint_level: number | null;
  level_description: string | null;
  degraded: boolean;
  runtime: string;
  cited_attempt_ids: string[];
  reason: string;
}
