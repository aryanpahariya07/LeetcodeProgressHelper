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
