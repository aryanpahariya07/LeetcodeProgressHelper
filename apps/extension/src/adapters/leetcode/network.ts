/**
 * LeetCode's own submission requests, observed.
 *
 * **Everything that knows LeetCode's network shape lives here**, exactly as
 * everything that knows its markup lives in `adapter.ts`. Nothing else in the
 * extension may read a request.
 *
 * ## Why this exists
 *
 * Two things were unobtainable from the DOM:
 *
 * 1. **Run detection.** `run_count` was structurally zero — the session reducer
 *    has always handled a `run` event and nothing ever dispatched one, because
 *    a Run click is not reliably distinguishable from any other button press.
 * 2. **The language**, which the adapter could only infer from a button label,
 *    and which therefore capped `capture_confidence` at `medium`.
 *
 * Both are stated outright in the request LeetCode already sends when you press
 * Run or Submit. Reading it is more accurate than any DOM heuristic, and it
 * cannot silently truncate the way a Monaco scrape can.
 *
 * ## Verified shape
 *
 * Captured from a real submission on 2026-09-09 (spec §19 — this is one of the
 * few things in the extension that has been checked against the live site):
 *
 * ```
 * POST /problems/{slug}/interpret_solution/   ← Run
 *   {"lang":"java","question_id":"27","typed_code":"…","data_input":"[3,2,2,3]\n3"}
 *
 * POST /problems/{slug}/submit/               ← Submit
 *   {"lang":"java","question_id":"27","typed_code":"…"}
 * ```
 *
 * `data_input` appears on Run only, but the URL already separates the two, so
 * the distinction does not rest on it.
 *
 * ## What is deliberately NOT read here
 *
 * `typed_code` is in both bodies and is **not** extracted. Code capture is off
 * by default and requires explicit consent (invariant 9); wiring it up before
 * the consent plumbing exists would mean code flowing with nobody having agreed
 * to it. This file reads only what the extension is already permitted to record.
 *
 * ## Fragility, stated plainly
 *
 * This breaks if LeetCode moves submissions to GraphQL or renames the routes.
 * It breaks *loudly* — no events observed, `run_count` back to zero — rather
 * than by quietly reporting wrong numbers, which is the failure mode that
 * matters. The DOM adapter remains the source of the submission verdict, so a
 * break here degrades capture rather than stopping it.
 */

/** The marker on every message this module posts, so the listener can trust it. */
export const NETWORK_MESSAGE = "dsa-coach:leetcode-network";

export type SubmissionKind = "run" | "submit";

export interface ObservedSubmission {
  source: typeof NETWORK_MESSAGE;
  kind: SubmissionKind;
  slug: string;
  /** LeetCode's own id for the problem. Stable across slug changes. */
  questionId: string | null;
  /** Authoritative, unlike the language read from a button label. */
  lang: string | null;
  at: number;
}

// Anchored at the end, so a route that merely *starts* with the same path —
// `/submit_batch/`, say — is not mistaken for a submission. An unanchored
// version matched `/interpret_solution_x/`, which would have invented runs from
// whatever unrelated endpoint LeetCode added next.
const RUN_URL = /\/problems\/([^/?#]+)\/interpret_solution\/?(?:[?#]|$)/;
const SUBMIT_URL = /\/problems\/([^/?#]+)\/submit\/?(?:[?#]|$)/;

/**
 * Classify a request, or return null if it is not a submission.
 *
 * Pure, so the URL and body parsing are testable without a browser — which is
 * the whole reason it is separated from the patching below.
 */
export function classify(
  url: string,
  body: unknown,
  at: number,
): ObservedSubmission | null {
  const submitMatch = SUBMIT_URL.exec(url);
  const runMatch = RUN_URL.exec(url);
  const match = submitMatch ?? runMatch;
  if (!match) return null;

  const parsed = parseBody(body);
  return {
    source: NETWORK_MESSAGE,
    kind: submitMatch ? "submit" : "run",
    slug: match[1].toLowerCase(),
    questionId: stringOrNull(parsed?.question_id),
    lang: stringOrNull(parsed?.lang)?.toLowerCase() ?? null,
    at,
  };
}

function parseBody(body: unknown): Record<string, unknown> | null {
  if (typeof body !== "string") return null;
  try {
    const parsed: unknown = JSON.parse(body);
    return typeof parsed === "object" && parsed !== null
      ? (parsed as Record<string, unknown>)
      : null;
  } catch {
    // A body we cannot read is not a reason to lose the event: the URL alone
    // still tells us a run or submit happened. Fields simply stay null, and
    // null means unknown rather than guessed (invariant 6).
    return null;
  }
}

function stringOrNull(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

/**
 * Patch `fetch` and `XMLHttpRequest` so submissions can be observed.
 *
 * Runs in the page's MAIN world, because an isolated content script cannot see
 * the page's own `fetch`. It is a pass-through in both directions: the request
 * is forwarded untouched and the response is never inspected. If anything here
 * throws, LeetCode keeps working and we simply observe nothing.
 */
export function installNetworkObserver(win: Window = window): void {
  const originalFetch = win.fetch;
  if (typeof originalFetch === "function") {
    win.fetch = function patchedFetch(
      this: unknown,
      input: RequestInfo | URL,
      init?: RequestInit,
    ): Promise<Response> {
      try {
        const url = typeof input === "string" ? input : (input as Request).url ?? String(input);
        report(win, classify(url, init?.body, Date.now()));
      } catch {
        // Observation must never break the page.
      }
      return originalFetch.call(this, input as RequestInfo, init);
    } as typeof win.fetch;
  }

  // `Window` in lib.dom does not declare XMLHttpRequest, though every real one
  // has it. Reached through the global rather than widening the parameter type.
  const xhr = (win as Window & typeof globalThis).XMLHttpRequest;
  if (typeof xhr === "function") {
    const originalOpen = xhr.prototype.open;
    const originalSend = xhr.prototype.send;
    const urlKey = "__dsaCoachUrl";

    xhr.prototype.open = function patchedOpen(
      this: XMLHttpRequest & Record<string, unknown>,
      method: string,
      url: string | URL,
      ...rest: unknown[]
    ) {
      this[urlKey] = String(url);
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      return (originalOpen as any).call(this, method, url, ...rest);
    } as typeof xhr.prototype.open;

    xhr.prototype.send = function patchedSend(
      this: XMLHttpRequest & Record<string, unknown>,
      body?: Document | XMLHttpRequestBodyInit | null,
    ) {
      try {
        report(win, classify(String(this[urlKey] ?? ""), body, Date.now()));
      } catch {
        // As above.
      }
      return originalSend.call(this, body);
    } as typeof xhr.prototype.send;
  }
}

function report(win: Window, observation: ObservedSubmission | null): void {
  if (observation) win.postMessage(observation, win.location.origin);
}
