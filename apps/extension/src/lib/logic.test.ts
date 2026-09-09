import { describe, expect, it } from "vitest";

import { LeetCodeAdapter, matchLanguage, matchOutcome } from "../adapters/leetcode/adapter";
import { accountTime, type TimelineEvent } from "./activeTime";
import { delayFor, isRetryable, MAX_ATTEMPTS, shouldRetry } from "./backoff";
import {
  buildAttemptEvent,
  emptySession,
  isReportable,
  overallConfidence,
  sessionTime,
  reduce,
  type SessionEvent,
  type SessionState,
} from "./session";
import { observed } from "./types";

const T0 = 1_700_000_000_000;
const minute = 60_000;

function run(events: SessionEvent[], from: SessionState = emptySession()): SessionState {
  return events.reduce(reduce, from);
}

function openEvent(slug = "two-sum", at = T0): SessionEvent {
  return { type: "open", slug, language: observed("python3"), at };
}

function submitEvent(at: number, outcome: "accepted" | "wrong_answer" = "accepted"): SessionEvent {
  return { type: "submit", observation: { outcome: observed(outcome), at } };
}

describe("accountTime", () => {
  it("counts a straightforward session in full", () => {
    const timeline: TimelineEvent[] = [
      { kind: "start", at: T0 },
      { kind: "input", at: T0 + minute },
      { kind: "input", at: T0 + 5 * minute },
      { kind: "end", at: T0 + 10 * minute },
    ];

    expect(accountTime(timeline).activeSeconds).toBe(600);
  });

  it("counts thinking time as working time", () => {
    // Four minutes of silence: someone staring at the screen working out the
    // recurrence. Not subtracted.
    const timeline: TimelineEvent[] = [
      { kind: "start", at: T0 },
      { kind: "input", at: T0 + 4 * minute },
      { kind: "end", at: T0 + 5 * minute },
    ];

    const result = accountTime(timeline);

    expect(result.activeSeconds).toBe(300);
    expect(result.excludedSeconds).toBe(0);
  });

  it("counts a LONG silence as working time too (spec §3.4)", () => {
    // The load-bearing test for the v3 revision. A twenty-minute silence with
    // the tab visible used to be charged as fifteen minutes away; it is now
    // counted in full. Silence is indistinguishable from thinking, and an
    // arbitrary threshold that guesses otherwise under-measures exactly the
    // sessions where the hardest thinking happened.
    const timeline: TimelineEvent[] = [
      { kind: "start", at: T0 },
      { kind: "input", at: T0 + 20 * minute },
      { kind: "end", at: T0 + 21 * minute },
    ];

    const result = accountTime(timeline);

    expect(result.activeSeconds).toBe(21 * 60);
    expect(result.excludedSeconds).toBe(0);
  });

  it("counts a long silence before the submission too", () => {
    // Symmetric: the last stretch before submitting is not treated differently
    // just because a submission follows it.
    const timeline: TimelineEvent[] = [
      { kind: "start", at: T0 },
      { kind: "input", at: T0 + minute },
      { kind: "end", at: T0 + 10 * minute },
    ];

    const result = accountTime(timeline);

    expect(result.activeSeconds).toBe(10 * 60);
    expect(result.excludedSeconds).toBe(0);
  });

  it("counts a session with no input at all", () => {
    // Reading the problem statement for ten minutes without touching anything
    // is working on it.
    const timeline: TimelineEvent[] = [
      { kind: "start", at: T0 },
      { kind: "end", at: T0 + 10 * minute },
    ];

    expect(accountTime(timeline).activeSeconds).toBe(10 * 60);
  });

  it("excludes time spent on another tab", () => {
    // The one exclusion that remains: being elsewhere is observed, not inferred.
    const timeline: TimelineEvent[] = [
      { kind: "start", at: T0 },
      { kind: "hidden", at: T0 + 2 * minute },
      { kind: "visible", at: T0 + 12 * minute },
      { kind: "end", at: T0 + 14 * minute },
    ];

    const result = accountTime(timeline);

    expect(result.hiddenSeconds).toBe(10 * 60);
    expect(result.activeSeconds).toBe(4 * 60);
  });

  it("charges a hidden stretch once, not twice", () => {
    // A silence spanning a hidden stretch is a single exclusion. This used to
    // require special-casing against the idle rule; with only one rule left
    // there is nothing to double-count, and the test guards that it stays so.
    const timeline: TimelineEvent[] = [
      { kind: "start", at: T0 },
      { kind: "hidden", at: T0 + minute },
      { kind: "visible", at: T0 + 30 * minute },
      { kind: "input", at: T0 + 31 * minute },
      { kind: "end", at: T0 + 32 * minute },
    ];

    const result = accountTime(timeline);

    expect(result.hiddenSeconds).toBe(29 * 60);
    expect(result.activeSeconds).toBe(3 * 60);
  });

  it("handles a tab still hidden when the session ends", () => {
    const timeline: TimelineEvent[] = [
      { kind: "start", at: T0 },
      { kind: "hidden", at: T0 + 5 * minute },
      { kind: "end", at: T0 + 15 * minute },
    ];

    expect(accountTime(timeline).activeSeconds).toBe(5 * 60);
  });

  it("never reports negative or excessive time", () => {
    const timeline: TimelineEvent[] = [
      { kind: "start", at: T0 },
      { kind: "hidden", at: T0 },
      { kind: "end", at: T0 + minute },
    ];

    const result = accountTime(timeline);

    expect(result.activeSeconds).toBeGreaterThanOrEqual(0);
    expect(result.excludedSeconds).toBeLessThanOrEqual(60);
  });

  it("returns zero for an unterminated session", () => {
    expect(accountTime([{ kind: "start", at: T0 }]).activeSeconds).toBe(0);
  });

  it("tolerates events arriving out of order", () => {
    const ordered = accountTime([
      { kind: "start", at: T0 },
      { kind: "input", at: T0 + minute },
      { kind: "end", at: T0 + 2 * minute },
    ]);
    const shuffled = accountTime([
      { kind: "end", at: T0 + 2 * minute },
      { kind: "start", at: T0 },
      { kind: "input", at: T0 + minute },
    ]);

    expect(shuffled).toEqual(ordered);
  });
});

describe("session state machine", () => {
  it("starts working when a problem is opened", () => {
    const state = run([openEvent()]);

    expect(state.phase).toBe("working");
    expect(state.slug).toBe("two-sum");
  });

  it("counts runs and submissions", () => {
    const state = run([
      openEvent(),
      { type: "run", at: T0 + minute },
      { type: "run", at: T0 + 2 * minute },
      submitEvent(T0 + 3 * minute),
    ]);

    expect(state.runCount).toBe(2);
    expect(state.submitCount).toBe(1);
  });

  it("asks for an answer after a submission", () => {
    const state = run([openEvent(), submitEvent(T0 + minute)]);

    expect(state.phase).toBe("awaiting_answer");
  });

  it("completes when the questionnaire is answered", () => {
    const state = run([
      openEvent(),
      submitEvent(T0 + minute),
      { type: "answer", answer: { resolution: "independent" } },
    ]);

    expect(state.phase).toBe("complete");
    expect(isReportable(state)).toBe(true);
  });

  it("still reports an attempt when the questionnaire is dismissed", () => {
    // Dismissal is missing information, not a failure (spec §3.2).
    const state = run([openEvent(), submitEvent(T0 + minute), { type: "dismiss" }]);

    expect(isReportable(state)).toBe(true);
    expect(buildAttemptEvent(state, { eventUuid: "x" })?.resolution).toBe("unknown");
  });

  it("reports no attempt when the user leaves without submitting", () => {
    // Recording an attempt here would invent an outcome nobody observed.
    const state = run([openEvent(), { type: "leave", at: T0 + minute }]);

    expect(state.phase).toBe("left");
    expect(isReportable(state)).toBe(false);
    expect(buildAttemptEvent(state, { eventUuid: "x" })).toBeNull();
  });

  it("keeps the measured time when the user leaves without submitting", () => {
    // No attempt, but the time was real. Discarding it would leave every
    // abandoned problem with no record of the work done on it (spec §3.6).
    const state = run([
      openEvent("two-sum"),
      { type: "run", at: T0 + 5 * minute },
      { type: "leave", at: T0 + 12 * minute },
    ]);

    const time = sessionTime(state);

    expect(time?.slug).toBe("two-sum");
    expect(time?.activeSeconds).toBe(12 * 60);
    expect(time?.runCount).toBe(1);
    expect(time?.submitCount).toBe(0);
  });

  it("reports no time for a session still in progress", () => {
    // An open timeline has no end; a partial figure would read as a real one.
    const state = run([openEvent(), { type: "run", at: T0 + minute }]);

    expect(sessionTime(state)).toBeNull();
  });

  it("starts a fresh session when returning to an abandoned problem", () => {
    // A second visit is its own stretch of time, not an extension of the first.
    const state = run([
      openEvent("two-sum"),
      { type: "leave", at: T0 + 10 * minute },
      openEvent("two-sum", T0 + 60 * minute),
      { type: "run", at: T0 + 62 * minute },
    ]);

    expect(state.phase).toBe("working");
    expect(state.startedAt).toBe(T0 + 60 * minute);
    expect(state.runCount).toBe(1);
  });

  it("ignores input after the session has been closed", () => {
    // Appending past `end` would silently extend a finished measurement.
    const state = run([
      openEvent(),
      { type: "leave", at: T0 + minute },
      { type: "input", at: T0 + 30 * minute },
    ]);

    expect(sessionTime(state)?.activeSeconds).toBe(60);
  });

  it("abandons the old session when navigating to another problem", () => {
    // LeetCode is a single-page app: this is the common path, not an edge case.
    const state = run([
      openEvent("two-sum"),
      { type: "run", at: T0 + minute },
      openEvent("3sum", T0 + 2 * minute),
    ]);

    expect(state.slug).toBe("3sum");
    expect(state.runCount).toBe(0);
  });

  it("ignores a repeated open of the same problem", () => {
    const state = run([
      openEvent(),
      { type: "run", at: T0 + minute },
      openEvent("two-sum", T0 + 2 * minute),
    ]);

    expect(state.runCount).toBe(1);
  });

  it("counts a second submission on the same problem", () => {
    const state = run([
      openEvent(),
      submitEvent(T0 + minute, "wrong_answer"),
      submitEvent(T0 + 2 * minute, "accepted"),
    ]);

    expect(state.submitCount).toBe(2);
    expect(state.lastSubmission?.outcome.value).toBe("accepted");
  });
});

describe("confidence", () => {
  it("is high when everything was read cleanly", () => {
    const state = run([openEvent(), submitEvent(T0 + minute), { type: "dismiss" }]);

    expect(overallConfidence(state)).toBe("high");
  });

  it("drops when the language could not be read", () => {
    const state = run([
      { type: "open", slug: "two-sum", language: { value: null, confidence: "low" }, at: T0 },
      submitEvent(T0 + minute),
      { type: "dismiss" },
    ]);

    expect(overallConfidence(state)).toBe("low");
  });

  it("drops when the verdict was only inferred from page text", () => {
    const state = run([
      openEvent(),
      { type: "submit", observation: { outcome: observed("accepted", "medium"), at: T0 + minute } },
      { type: "dismiss" },
    ]);

    expect(overallConfidence(state)).toBe("medium");
  });

  it("drops when the adapter is broken", () => {
    const state = run([
      openEvent(),
      { type: "adapter_failed" },
      submitEvent(T0 + minute),
      { type: "dismiss" },
    ]);

    expect(overallConfidence(state)).toBe("low");
  });
});

describe("buildAttemptEvent", () => {
  const complete = run([
    openEvent(),
    { type: "run", at: T0 + minute },
    { type: "run", at: T0 + 5 * minute },
    submitEvent(T0 + 10 * minute),
    { type: "answer", answer: { resolution: "after_hint", blocker: "edge_cases" } },
  ]);

  it("produces nothing from an incomplete session", () => {
    expect(buildAttemptEvent(run([openEvent()]), { eventUuid: "x" })).toBeNull();
  });

  it("carries the questionnaire answers through", () => {
    const event = buildAttemptEvent(complete, { eventUuid: "abc" });

    expect(event?.resolution).toBe("after_hint");
    expect(event?.blocker).toBe("edge_cases");
  });

  it("reports measured time and what was excluded", () => {
    const event = buildAttemptEvent(complete, { eventUuid: "abc" });

    expect(event?.active_seconds).toBe(600);
    expect(event?.excluded_seconds).toBe(0);
  });

  it("sends unknown time as null rather than zero", () => {
    // Zero would read as "no work happened"; null reads as "not measured".
    const instant = run([openEvent(), submitEvent(T0)]);
    const event = buildAttemptEvent({ ...instant, phase: "complete" }, { eventUuid: "x" });

    expect(event?.active_seconds).toBeNull();
  });

  it("sends an unreadable verdict as unknown, never a guess", () => {
    const state = run([
      openEvent(),
      { type: "submit", observation: { outcome: { value: null, confidence: "low" }, at: T0 + 60 } },
      { type: "dismiss" },
    ]);

    expect(buildAttemptEvent(state, { eventUuid: "x" })?.submission_outcome).toBe("unknown");
  });

  it("never sends a source field — the server assigns that", () => {
    expect(buildAttemptEvent(complete, { eventUuid: "x" })).not.toHaveProperty("source");
  });

  it("uses the caller's idempotency key verbatim", () => {
    expect(buildAttemptEvent(complete, { eventUuid: "key-1" })?.event_uuid).toBe("key-1");
  });
});

describe("retry policy", () => {
  it.each([
    [0, true],
    [429, true],
    [500, true],
    [503, true],
  ])("retries status %i", (status, expected) => {
    expect(isRetryable(status)).toBe(expected);
  });

  it("does not retry a revoked device", () => {
    // Retrying forever would hammer the server and hide the real problem.
    expect(isRetryable(401)).toBe(false);
    expect(isRetryable(403)).toBe(false);
  });

  it("does not retry a payload the server rejected", () => {
    expect(isRetryable(400)).toBe(false);
    expect(isRetryable(413)).toBe(false);
  });

  it("backs off exponentially", () => {
    const noJitter = () => 1;

    expect(delayFor(1, noJitter)).toBeLessThan(delayFor(3, noJitter));
    expect(delayFor(3, noJitter)).toBeLessThan(delayFor(5, noJitter));
  });

  it("caps the delay", () => {
    expect(delayFor(50, () => 1)).toBeLessThanOrEqual(15 * 60 * 1000);
  });

  it("applies jitter so queues do not wake in lockstep", () => {
    expect(delayFor(5, () => 0)).toBe(0);
    expect(delayFor(5, () => 1)).toBeGreaterThan(0);
  });

  it("eventually gives up", () => {
    expect(shouldRetry(MAX_ATTEMPTS, 500)).toBe(false);
    expect(shouldRetry(1, 500)).toBe(true);
  });
});

describe("leetcode adapter", () => {
  const adapter = new LeetCodeAdapter();

  it("recognises problem pages and nothing else", () => {
    expect(adapter.isProblemPage("https://leetcode.com/problems/two-sum/")).toBe(true);
    expect(adapter.isProblemPage("https://leetcode.com/problemset/all/")).toBe(false);
    expect(adapter.isProblemPage("https://example.com/problems/two-sum/")).toBe(false);
  });

  it("reads the slug from the URL, which markup changes cannot break", () => {
    const result = adapter.problemSlug(
      "https://leetcode.com/problems/Longest-Substring/description/?envType=x",
    );

    expect(result.value).toBe("longest-substring");
    expect(result.confidence).toBe("high");
  });

  it("reports an unrecognised URL as unknown", () => {
    expect(adapter.problemSlug("https://leetcode.com/contest/").value).toBeNull();
  });

  it.each([
    ["Accepted", "accepted"],
    ["Wrong Answer", "wrong_answer"],
    ["Time Limit Exceeded", "tle"],
    ["Runtime Error", "runtime_error"],
    ["Compile Error", "compile_error"],
  ])("maps the verdict %s", (text, expected) => {
    expect(matchOutcome(text)).toBe(expected);
  });

  it("does not let a shorter verdict shadow a longer one", () => {
    // "Time Limit Exceeded" must not be read as anything else.
    expect(matchOutcome("Time Limit Exceeded")).toBe("tle");
  });

  it("returns null for text with no verdict in it", () => {
    expect(matchOutcome("Run your code to see results")).toBeNull();
  });

  it("prefers the more specific language name", () => {
    expect(matchLanguage("Python3")).toBe("python3");
    expect(matchLanguage("Python")).toBe("python");
  });

  it("returns null rather than guessing an unknown language", () => {
    expect(matchLanguage("Brainfuck")).toBeNull();
    expect(matchLanguage("")).toBeNull();
  });
});
