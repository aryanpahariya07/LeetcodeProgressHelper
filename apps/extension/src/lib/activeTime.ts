/**
 * Active-time accounting (spec §3.4).
 *
 * The trap this module exists to avoid: **staring at the screen thinking is the
 * most valuable activity in a DSA attempt, and it is indistinguishable from
 * idleness by any input-based heuristic.** Subtracting it would systematically
 * under-measure exactly the sessions where the real work happened.
 *
 * So exactly one thing is excluded: time when the tab was not visible. You were
 * somewhere else, and that is *observed* rather than inferred from an absence.
 *
 * **Silence is never excluded.** An earlier version also charged the excess of
 * any no-input gap beyond five minutes — which took the argument above seriously
 * enough to write it down and then contradicted it with an arbitrary threshold.
 * A quiet stretch is indistinguishable from thinking, so it counts as thinking.
 *
 * Known limitation, accepted deliberately (spec §3.4): `visibilitychange` fires
 * on tab switch and minimise, but not reliably when another application is
 * focused over the browser. Walking away from an open problem therefore accrues
 * active time. The alternative under-measures real thinking, which is the worse
 * error here.
 *
 * `input` events remain in the timeline: they are no longer used for exclusion,
 * but they are what tells the session machine the page is being worked on.
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
}

export function accountTime(events: TimelineEvent[]): TimeAccounting {
  const timeline = [...events].sort((a, b) => a.at - b.at);
  const start = timeline.find((e) => e.kind === "start");
  const end = [...timeline].reverse().find((e) => e.kind === "end");

  if (!start || !end || end.at <= start.at) {
    return { activeSeconds: 0, excludedSeconds: 0, hiddenSeconds: 0 };
  }

  const total = end.at - start.at;
  const hidden = Math.min(total, hiddenMs(timeline, start.at, end.at));

  return {
    activeSeconds: Math.max(0, Math.round((total - hidden) / 1000)),
    excludedSeconds: Math.round(hidden / 1000),
    hiddenSeconds: Math.round(hidden / 1000),
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
