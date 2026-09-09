/**
 * The attempt session: one problem, from opening it to the questionnaire.
 *
 * A pure state machine. The content script feeds it observations and it decides
 * what, if anything, is worth sending. Keeping it free of DOM and timers is what
 * makes the awkward cases — SPA navigation mid-attempt, a submission with an
 * unreadable result, a dismissed questionnaire — testable at all.
 *
 * Invariant 6 runs through it: anything the adapter could not read stays null
 * and drags `capture_confidence` down. Nothing is inferred to fill a gap.
 */

import { accountTime, type TimelineEvent } from "./activeTime";
import type {
  AttemptEvent,
  Blocker,
  CaptureConfidence,
  Observed,
  Resolution,
  SubmissionOutcome,
} from "./types";

/**
 * `left` is a terminal phase like `complete`, but carries no submission: the
 * user worked on the problem and navigated away. It exists so the measured
 * time survives (spec §3.6) without implying an outcome.
 */
export type SessionPhase = "idle" | "working" | "awaiting_answer" | "complete" | "left";

export interface SubmissionObservation {
  outcome: Observed<SubmissionOutcome>;
  at: number;
}

export interface QuestionnaireAnswer {
  resolution: Resolution;
  blocker?: Blocker | null;
  confidenceColdRedo?: number | null;
}

export interface SessionState {
  phase: SessionPhase;
  slug: string | null;
  language: Observed<string>;
  startedAt: number | null;
  timeline: TimelineEvent[];
  runCount: number;
  submitCount: number;
  lastSubmission: SubmissionObservation | null;
  answer: QuestionnaireAnswer | null;
  adapterHealthy: boolean;
}

export function emptySession(): SessionState {
  return {
    phase: "idle",
    slug: null,
    language: { value: null, confidence: "low" },
    startedAt: null,
    timeline: [],
    runCount: 0,
    submitCount: 0,
    lastSubmission: null,
    answer: null,
    adapterHealthy: true,
  };
}

export type SessionEvent =
  | { type: "open"; slug: string; language: Observed<string>; at: number }
  | { type: "input"; at: number }
  | { type: "hidden"; at: number }
  | { type: "visible"; at: number }
  | { type: "run"; at: number }
  | { type: "submit"; observation: SubmissionObservation }
  | { type: "answer"; answer: QuestionnaireAnswer }
  | { type: "dismiss" }
  | { type: "leave"; at: number }
  | { type: "adapter_failed" };

export function reduce(state: SessionState, event: SessionEvent): SessionState {
  switch (event.type) {
    case "open": {
      // Navigating to a different problem abandons the previous session.
      // LeetCode is a single-page app, so this is the common path, not the edge.
      //
      // Only an *in-flight* session for the same problem is kept. Coming back to
      // a problem already parked as `complete` or `left` starts a fresh session,
      // which is what makes a second visit its own measured stretch of time
      // rather than an extension of the first.
      const inFlight = state.phase === "working" || state.phase === "awaiting_answer";
      if (state.slug === event.slug && inFlight) return state;
      return {
        ...emptySession(),
        phase: "working",
        slug: event.slug,
        language: event.language,
        startedAt: event.at,
        timeline: [{ kind: "start", at: event.at }],
      };
    }

    case "input":
    case "hidden":
    case "visible": {
      // Terminal phases have a closed timeline; appending after `end` would
      // silently extend a measurement that is already finished.
      if (state.phase === "idle" || state.phase === "complete" || state.phase === "left") {
        return state;
      }
      return { ...state, timeline: [...state.timeline, { kind: event.type, at: event.at }] };
    }

    case "run": {
      if (state.phase !== "working") return state;
      return {
        ...state,
        runCount: state.runCount + 1,
        timeline: [...state.timeline, { kind: "input", at: event.at }],
      };
    }

    case "submit": {
      if (state.phase === "idle" || state.phase === "complete") return state;
      return {
        ...state,
        phase: "awaiting_answer",
        submitCount: state.submitCount + 1,
        lastSubmission: event.observation,
        timeline: [
          ...state.timeline,
          { kind: "input", at: event.observation.at },
          { kind: "end", at: event.observation.at },
        ],
      };
    }

    case "answer": {
      if (state.phase !== "awaiting_answer") return state;
      return { ...state, phase: "complete", answer: event.answer };
    }

    case "dismiss": {
      // A dismissal is missing information, not a failure (spec §3.2). The
      // attempt is still worth sending; it simply carries no resolution.
      if (state.phase !== "awaiting_answer") return state;
      return { ...state, phase: "complete", answer: null };
    }

    case "leave": {
      if (state.phase !== "working") return state;
      // Left without submitting. There is still no *attempt* to report —
      // recording one would invent an outcome nobody observed — but the time
      // spent was observed, and discarding it loses the only measurement of
      // every problem worked on and abandoned (spec §3.6). So the timeline is
      // closed and the session parked as `left`, from which the episode's
      // active time can be read without any outcome being implied.
      return {
        ...state,
        phase: "left",
        timeline: [...state.timeline, { kind: "end", at: event.at }],
      };
    }

    case "adapter_failed":
      return { ...state, adapterHealthy: false };
  }
}

/** Whether the session has something worth sending. */
export function isReportable(state: SessionState): boolean {
  return state.phase === "complete" && state.slug !== null && state.lastSubmission !== null;
}

export interface SessionTime {
  slug: string;
  startedAt: number;
  activeSeconds: number;
  hiddenSeconds: number;
  runCount: number;
  submitCount: number;
}

/**
 * The measured time for a finished session, submission or not.
 *
 * `buildAttemptEvent` deliberately returns null without a submission, because
 * an attempt without an outcome would be an invented one. But time spent is
 * observed either way, and a problem worked on and walked away from has no
 * other record of it (spec §3.6). This reads that much and nothing more.
 *
 * Returns null while the session is still running — an open timeline has no
 * `end`, and `accountTime` would report zero rather than a partial figure.
 */
export function sessionTime(state: SessionState): SessionTime | null {
  if (state.phase !== "complete" && state.phase !== "left") return null;
  if (!state.slug || state.startedAt === null) return null;

  const timing = accountTime(state.timeline);
  return {
    slug: state.slug,
    startedAt: state.startedAt,
    activeSeconds: timing.activeSeconds,
    hiddenSeconds: timing.hiddenSeconds,
    runCount: state.runCount,
    submitCount: state.submitCount,
  };
}

/**
 * The lowest confidence of everything that went into the attempt.
 *
 * One unreadable field drags the whole record down, because a partly-guessed
 * attempt is not high-confidence evidence.
 */
export function overallConfidence(state: SessionState): CaptureConfidence {
  const order: CaptureConfidence[] = ["high", "medium", "low"];
  const parts: CaptureConfidence[] = [
    state.language.confidence,
    state.lastSubmission?.outcome.confidence ?? "low",
    state.adapterHealthy ? "high" : "low",
  ];
  return parts.reduce((worst, part) =>
    order.indexOf(part) > order.indexOf(worst) ? part : worst,
  );
}

export interface BuildOptions {
  eventUuid: string;
  isResolve?: boolean;
}

/** Turn a completed session into the event the API expects. */
export function buildAttemptEvent(
  state: SessionState,
  options: BuildOptions,
): AttemptEvent | null {
  if (!isReportable(state) || !state.slug || !state.lastSubmission) return null;

  const timing = accountTime(state.timeline);

  return {
    event_uuid: options.eventUuid,
    problem_slug: state.slug,
    provider: "leetcode",
    submitted_at: new Date(state.lastSubmission.at).toISOString(),
    started_at: state.startedAt ? new Date(state.startedAt).toISOString() : null,
    language: state.language.value,
    // Zero active seconds means the measurement failed, not that no work
    // happened — send null so the server records it as unknown.
    active_seconds: timing.activeSeconds > 0 ? timing.activeSeconds : null,
    excluded_seconds: timing.excludedSeconds,
    run_count: state.runCount,
    submit_count: state.submitCount,
    submission_outcome: state.lastSubmission.outcome.value ?? "unknown",
    resolution: state.answer?.resolution ?? "unknown",
    blocker: state.answer?.blocker ?? null,
    confidence_cold_redo: state.answer?.confidenceColdRedo ?? null,
    is_resolve: options.isResolve ?? false,
    capture_confidence: overallConfidence(state),
    raw_metadata: {
      hidden_seconds: timing.hiddenSeconds,
      adapter_healthy: state.adapterHealthy,
    },
  };
}
