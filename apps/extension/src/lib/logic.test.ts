import { describe, expect, it } from "vitest";

import { LeetCodeAdapter, matchLanguage, matchOutcome } from "../adapters/leetcode/adapter";
import { accountTime, type TimelineEvent } from "./activeTime";
import { delayFor, isRetryable, MAX_ATTEMPTS, shouldRetry } from "./backoff";
import { shouldCapture, stateAfterHealth } from "./monitoring";
import {
  buildAttemptEvent,
  deriveResolution,
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

  it("completes on a submission, with no questionnaire in between", () => {
    // The 1–2 click questionnaire is gone (spec §3.2). The resolution is
    // derived from the verdict plus whether the editorial was opened, so there
    // is nothing left to wait for.
    const state = run([openEvent(), submitEvent(T0 + minute)]);

    expect(state.phase).toBe("complete");
    expect(isReportable(state)).toBe(true);
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

  it("reports an unreadable verdict as unknown, not as a failure", () => {
    // A verdict the adapter could not read is missing information, not a failed
    // attempt — §6.2 treats unknown as weak evidence rather than a loss.
    const state = run([
      openEvent(),
      {
        type: "submit",
        observation: { outcome: { value: null, confidence: "low" }, at: T0 + minute },
      },
    ]);

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

  it("starts a new session for a second submission on the same problem", () => {
    // A submission now completes the session outright, so the retry after a
    // wrong answer is its own attempt rather than a second count on the first.
    // That is what makes each submission a separate row of evidence.
    const state = run([
      openEvent(),
      submitEvent(T0 + minute, "wrong_answer"),
      openEvent("two-sum", T0 + 90_000),
      submitEvent(T0 + 2 * minute, "accepted"),
    ]);

    expect(state.submitCount).toBe(1);
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
      { type: "adapter_health", healthy: false },
      submitEvent(T0 + minute),
    ]);

    expect(overallConfidence(state)).toBe("low");
  });

  it("recovers when the adapter starts working again", () => {
    // The health check races the editor rendering, so an early miss is normal.
    // Latching on it permanently downgraded every attempt in the session to
    // low confidence, on the strength of a page that had not finished loading.
    const state = run([
      openEvent(),
      { type: "adapter_health", healthy: false },
      { type: "adapter_health", healthy: true },
      submitEvent(T0 + minute),
    ]);

    expect(state.adapterHealthy).toBe(true);
    expect(overallConfidence(state)).not.toBe("low");
  });
});

describe("buildAttemptEvent", () => {
  const complete = run([
    openEvent(),
    { type: "run", at: T0 + minute },
    { type: "run", at: T0 + 5 * minute },
    submitEvent(T0 + 10 * minute),
  ]);

  it("produces nothing from an incomplete session", () => {
    expect(buildAttemptEvent(run([openEvent()]), { eventUuid: "x" })).toBeNull();
  });

  it("derives the resolution and leaves the blocker unknown", () => {
    // Nothing is asked for, so nothing is guessed. The blocker stays null until
    // the code-conclusion pipeline can infer it from what was written; a made-up
    // blocker would steer prescription on invented evidence.
    const event = buildAttemptEvent(complete, { eventUuid: "abc" });

    expect(event?.resolution).toBe("independent");
    expect(event?.blocker).toBeNull();
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

describe("language, upgraded from the network observation", () => {
  it("replaces a guessed language with the one LeetCode states", () => {
    // The adapter can only infer the language from a button label. LeetCode's
    // own request states it outright, which is why this event exists.
    const state = run([
      { type: "open", slug: "two-sum", language: observed("python3", "medium"), at: T0 },
      { type: "language", language: observed("java", "high") },
    ]);

    expect(state.language.value).toBe("java");
    expect(state.language.confidence).toBe("high");
  });

  it("does not let a weaker read overwrite a stronger one", () => {
    const state = run([
      { type: "open", slug: "two-sum", language: observed("java", "high"), at: T0 },
      { type: "language", language: observed("python3", "low") },
    ]);

    expect(state.language.value).toBe("java");
  });

  it("ignores an empty observation", () => {
    // Invariant 6: unknown stays unknown rather than blanking what is known.
    const state = run([
      openEvent(),
      { type: "language", language: { value: null, confidence: "low" } },
    ]);

    expect(state.language.value).toBe("python3");
  });

  it("is ignored once the session is finished", () => {
    const state = run([
      openEvent(),
      { type: "leave", at: T0 + minute },
      { type: "language", language: observed("java", "high") },
    ]);

    expect(state.language.value).toBe("python3");
  });
});

describe("run counting", () => {
  it("counts each observed run", () => {
    // `run_count` was structurally zero before the network observer existed:
    // the reducer had always handled this event and nothing ever dispatched it.
    const state = run([
      openEvent(),
      { type: "run", at: T0 + minute },
      { type: "run", at: T0 + 2 * minute },
      { type: "run", at: T0 + 3 * minute },
    ]);

    expect(state.runCount).toBe(3);
  });

  it("reaches the attempt event", () => {
    const state = run([
      openEvent(),
      { type: "run", at: T0 + minute },
      { type: "run", at: T0 + 2 * minute },
      submitEvent(T0 + 3 * minute),
      { type: "answer", answer: { resolution: "independent" } },
    ]);

    expect(buildAttemptEvent(state, { eventUuid: "x" })?.run_count).toBe(2);
  });
});

describe("monitoring state (spec §4.3)", () => {
  describe("what still gets captured", () => {
    it("records while degraded", () => {
      // The bug that discarded every attempt for a day. `degraded` means the
      // adapter could not find an anchor, so the event goes out with
      // capture_confidence: low — which §4.3 requires, in as many words:
      // degrade "rather than silently capturing nothing".
      expect(shouldCapture("degraded")).toBe(true);
    });

    it("records while monitoring", () => {
      expect(shouldCapture("monitoring")).toBe(true);
    });

    it.each(["paused", "disconnected"] as const)("does not record while %s", (state) => {
      // These are decisions the user made. Honour them.
      expect(shouldCapture(state)).toBe(false);
    });
  });

  describe("degradation is reversible", () => {
    it("degrades when the adapter cannot find its anchors", () => {
      expect(stateAfterHealth("monitoring", false)).toBe("degraded");
    });

    it("recovers when the adapter works again", () => {
      // Without this, one health check failing on a page where the console had
      // not rendered yet left the extension degraded forever — no route back
      // short of re-pairing.
      expect(stateAfterHealth("degraded", true)).toBe("monitoring");
    });

    it("stays degraded while still unhealthy", () => {
      expect(stateAfterHealth("degraded", false)).toBe("degraded");
    });

    it.each(["paused", "disconnected"] as const)(
      "never overrides %s in either direction",
      (state) => {
        // A health check does not get to undo a user's choice.
        expect(stateAfterHealth(state, true)).toBe(state);
        expect(stateAfterHealth(state, false)).toBe(state);
      },
    );

    it("is idempotent for a healthy monitoring extension", () => {
      expect(stateAfterHealth("monitoring", true)).toBe("monitoring");
    });
  });
});

describe("deriving the resolution without a questionnaire (spec §3.2)", () => {
  it("an accepted verdict is independent", () => {
    const state = run([openEvent(), submitEvent(T0 + minute, "accepted")]);

    expect(deriveResolution(state)).toBe("independent");
  });

  it("accepted AFTER opening the editorial is not independent", () => {
    // The whole reason the questionnaire could be dropped safely. An accepted
    // verdict looks identical whether you solved it or read the answer, so
    // without this every pass would credit readiness that was never earned.
    const state = run([
      openEvent(),
      { type: "aid_viewed" },
      submitEvent(T0 + minute, "accepted"),
    ]);

    expect(deriveResolution(state)).toBe("after_editorial");
  });

  it("a rejected verdict is a failure", () => {
    const state = run([openEvent(), submitEvent(T0 + minute, "wrong_answer")]);

    expect(deriveResolution(state)).toBe("failed");
  });

  it("an unreadable verdict is unknown, never a failure", () => {
    const state = run([
      openEvent(),
      {
        type: "submit",
        observation: { outcome: { value: null, confidence: "low" }, at: T0 + minute },
      },
    ]);

    expect(deriveResolution(state)).toBe("unknown");
  });

  it("having seen the editorial cannot be un-seen", () => {
    // Sticky for the session: flicking back to the description afterwards does
    // not restore an independent solve.
    const state = run([
      openEvent(),
      { type: "aid_viewed" },
      { type: "input", at: T0 + minute },
      submitEvent(T0 + 2 * minute, "accepted"),
    ]);

    expect(state.sawAid).toBe(true);
    expect(deriveResolution(state)).toBe("after_editorial");
  });

  it("a fresh session on the same problem forgets the editorial", () => {
    // Coming back tomorrow and solving it unaided is an independent solve.
    const state = run([
      openEvent(),
      { type: "aid_viewed" },
      submitEvent(T0 + minute, "wrong_answer"),
      openEvent("two-sum", T0 + 10 * minute),
      submitEvent(T0 + 11 * minute, "accepted"),
    ]);

    expect(deriveResolution(state)).toBe("independent");
  });
});

describe("recovering from a failed startup health check (spec §4.3)", () => {
  it("a late-rendering page recovers once the anchors appear", () => {
    // The failure that cost two debugging sessions. `checkHealth` used to run
    // once at document_idle, which on this single-page app is before the editor
    // pane exists — so the language picker was missing, health failed, and the
    // extension sat `degraded` forever because nothing reported health again.
    const afterFailedStartup = stateAfterHealth("monitoring", false);
    expect(afterFailedStartup).toBe("degraded");

    // The editor renders; the next check succeeds and must undo it.
    expect(stateAfterHealth(afterFailedStartup, true)).toBe("monitoring");
  });

  it("keeps capturing throughout, degraded or not", () => {
    // Even while degraded the attempt is recorded, with reduced confidence.
    // Losing evidence to a rendering race is the worse of the two errors.
    expect(shouldCapture("degraded")).toBe(true);
    expect(shouldCapture("monitoring")).toBe(true);
  });
});
