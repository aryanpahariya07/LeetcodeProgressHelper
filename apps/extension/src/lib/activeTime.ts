/**
 * Active-time accounting (spec §3.4).
 *
 * The trap this module exists to avoid: **staring at the screen thinking is the
 * most valuable activity in a DSA attempt, and it is indistinguishable from
 * idleness by any input-based heuristic.** Subtracting it would systematically
 * under-measure exactly the sessions where the real work happened.
 *
 * So only two things are excluded:
 *
 * 1. Time when the tab was not visible. You were somewhere else.
 * 2. The *excess* of a no-input gap beyond the idle threshold — not the whole
 *    gap. A seven-minute silence is five minutes of plausible thinking plus two
 *    minutes of absence, and is charged as such. Excluding the whole gap would
 *    punish thinking, which is the failure mode this design is guarding against.
 *
 * Everything excluded is reported, so the heuristic stays auditable rather than
 * being an unexplained number.
 *
 * Pure: takes a timeline, returns a result. No clock, no DOM.
 */

export type TimelineEvent =
  | { kind: "start"; at: number }
  | { kind: "input"; at: number }
  | { kind: "hidden"; at: number }
  | { kind: "visible"; at: number }
  | { kind: "end"; at: number };

export interface TimeAccounting {
  activeSeconds: number;
  excludedSeconds: number;
  hiddenSeconds: number;
  idleSeconds: number;
}

/** No-input gaps longer than this start accruing excluded time (spec §3.4). */
export const IDLE_THRESHOLD_MS = 5 * 60 * 1000;

export function accountTime(
  events: TimelineEvent[],
  idleThresholdMs: number = IDLE_THRESHOLD_MS,
): TimeAccounting {
  const timeline = [...events].sort((a, b) => a.at - b.at);
  const start = timeline.find((e) => e.kind === "start");
  const end = [...timeline].reverse().find((e) => e.kind === "end");

  if (!start || !end || end.at <= start.at) {
    return { activeSeconds: 0, excludedSeconds: 0, hiddenSeconds: 0, idleSeconds: 0 };
  }

  const total = end.at - start.at;
  const hidden = hiddenMs(timeline, start.at, end.at);
  const idle = idleExcessMs(timeline, start.at, end.at, idleThresholdMs);

  // A hidden tab produces no input, so its silence would be counted twice.
  const excluded = Math.min(total, hidden + idle);

  return {
    activeSeconds: Math.max(0, Math.round((total - excluded) / 1000)),
    excludedSeconds: Math.round(excluded / 1000),
    hiddenSeconds: Math.round(hidden / 1000),
    idleSeconds: Math.round(idle / 1000),
  };
}

function hiddenMs(timeline: TimelineEvent[], start: number, end: number): number {
  let hidden = 0;
  let hiddenSince: number | null = null;

  for (const event of timeline) {
    if (event.at < start || event.at > end) continue;
    if (event.kind === "hidden" && hiddenSince === null) {
      hiddenSince = event.at;
    } else if (event.kind === "visible" && hiddenSince !== null) {
      hidden += event.at - hiddenSince;
      hiddenSince = null;
    }
  }

  // Still hidden when the session ended.
  if (hiddenSince !== null) hidden += end - hiddenSince;
  return hidden;
}

/**
 * Excluded time from long silences — the excess only.
 *
 * Gaps that overlap a hidden stretch are skipped, because that time is already
 * accounted for as hidden.
 */
function idleExcessMs(
  timeline: TimelineEvent[],
  start: number,
  end: number,
  threshold: number,
): number {
  const marks = timeline
    .filter((e) => e.kind === "input" || e.kind === "start" || e.kind === "end")
    .map((e) => e.at)
    .filter((at) => at >= start && at <= end);

  const hiddenRanges = hiddenIntervals(timeline, start, end);
  let excess = 0;

  for (let i = 1; i < marks.length; i += 1) {
    const gapStart = marks[i - 1];
    const gapEnd = marks[i];
    const gap = gapEnd - gapStart;
    if (gap <= threshold) continue;

    const overlapped = hiddenRanges.some(
      ([hStart, hEnd]) => hStart < gapEnd && hEnd > gapStart,
    );
    if (overlapped) continue;

    excess += gap - threshold;
  }

  return excess;
}

function hiddenIntervals(
  timeline: TimelineEvent[],
  start: number,
  end: number,
): Array<[number, number]> {
  const ranges: Array<[number, number]> = [];
  let hiddenSince: number | null = null;

  for (const event of timeline) {
    if (event.at < start || event.at > end) continue;
    if (event.kind === "hidden" && hiddenSince === null) {
      hiddenSince = event.at;
    } else if (event.kind === "visible" && hiddenSince !== null) {
      ranges.push([hiddenSince, event.at]);
      hiddenSince = null;
    }
  }
  if (hiddenSince !== null) ranges.push([hiddenSince, end]);
  return ranges;
}
