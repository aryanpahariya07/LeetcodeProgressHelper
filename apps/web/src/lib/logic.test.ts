import { describe, expect, it } from "vitest";

import { toAttemptEvent } from "./api";
import { BAND_LABELS, BAND_STYLES, formatMinutes, ratingBand } from "./format";
import { attemptSchema, normalizeSlug, type AttemptFormValues } from "./schemas";
import type { ReadinessBand } from "./types";

const base: AttemptFormValues = {
  problem_slug: "two-sum",
  resolution: "independent",
  blocker: "",
  submission_outcome: "accepted",
  language: "python",
  active_minutes: 15,
  confidence_cold_redo: 4,
  is_resolve: false,
  notes: "",
};

describe("normalizeSlug", () => {
  it("accepts a bare slug", () => {
    expect(normalizeSlug("two-sum")).toBe("two-sum");
  });

  it("extracts the slug from a pasted URL", () => {
    expect(normalizeSlug("https://leetcode.com/problems/3sum/description/")).toBe("3sum");
  });

  it("ignores query strings and fragments", () => {
    expect(normalizeSlug("https://leetcode.com/problems/koko-eating-bananas/?envType=x")).toBe(
      "koko-eating-bananas",
    );
  });

  it("trims and lowercases", () => {
    expect(normalizeSlug("  Merge-Intervals/ ")).toBe("merge-intervals");
  });
});

describe("attemptSchema", () => {
  it("accepts a well-formed attempt", () => {
    expect(attemptSchema.safeParse(base).success).toBe(true);
  });

  it("rejects an empty problem", () => {
    const result = attemptSchema.safeParse({ ...base, problem_slug: "" });
    expect(result.success).toBe(false);
  });

  it("rejects a blocker on an independently solved attempt", () => {
    const result = attemptSchema.safeParse({ ...base, blocker: "edge_cases" });
    expect(result.success).toBe(false);
  });

  it("allows a blocker when the attempt was not independent", () => {
    const result = attemptSchema.safeParse({
      ...base,
      resolution: "failed",
      blocker: "pattern_not_recognized",
    });
    expect(result.success).toBe(true);
  });

  it("rejects a confidence rating outside 1-5", () => {
    expect(attemptSchema.safeParse({ ...base, confidence_cold_redo: 9 }).success).toBe(false);
  });

  it("allows time spent to be omitted", () => {
    const result = attemptSchema.safeParse({ ...base, active_minutes: undefined });
    expect(result.success).toBe(true);
  });
});

describe("toAttemptEvent", () => {
  it("never sends a source — the server assigns it", () => {
    expect(toAttemptEvent(base)).not.toHaveProperty("source");
  });

  it("marks hand-typed entries as medium confidence, not high", () => {
    expect(toAttemptEvent(base).capture_confidence).toBe("medium");
  });

  it("converts minutes to seconds", () => {
    expect(toAttemptEvent(base).active_seconds).toBe(900);
  });

  it("records omitted time as null rather than zero", () => {
    const event = toAttemptEvent({ ...base, active_minutes: undefined });
    expect(event.active_seconds).toBeNull();
  });

  it("generates a fresh idempotency key each time", () => {
    expect(toAttemptEvent(base).event_uuid).not.toBe(toAttemptEvent(base).event_uuid);
  });

  it("sends an empty blocker as null", () => {
    expect(toAttemptEvent(base).blocker).toBeNull();
  });
});

describe("ratingBand", () => {
  it("hides the number when the deviation is high", () => {
    // Phase 0 ratings are curator estimates with rd=300 (spec §11).
    expect(ratingBand(1500, 300)).toBe("approx.");
  });

  it("shows the number once the rating is well established", () => {
    expect(ratingBand(1500, 75)).toBe("1500");
  });
});

describe("formatMinutes", () => {
  it.each([
    [45, "45 min"],
    [60, "1h"],
    [95, "1h 35m"],
  ])("formats %i as %s", (input, expected) => {
    expect(formatMinutes(input)).toBe(expected);
  });
});

describe("readiness bands", () => {
  it("labels every band a pattern can be in", () => {
    const bands: ReadinessBand[] = [
      "calibrating",
      "not_ready",
      "developing",
      "approaching",
      "ready",
    ];
    for (const band of bands) {
      expect(BAND_LABELS[band]).toBeTruthy();
      expect(BAND_STYLES[band]).toBeTruthy();
    }
  });

  it("shows 'Calibrating' rather than a number when evidence is thin", () => {
    // Invariant 12: never present an uncalibrated estimate as a probability.
    expect(BAND_LABELS.calibrating).toBe("Calibrating");
    expect(BAND_LABELS.calibrating).not.toMatch(/\d/);
  });

  it("never renders a band label as a percentage", () => {
    for (const label of Object.values(BAND_LABELS)) {
      expect(label).not.toContain("%");
    }
  });
});
