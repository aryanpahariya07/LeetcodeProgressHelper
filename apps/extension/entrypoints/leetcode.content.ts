/**
 * Content script: watches a LeetCode problem page.
 *
 * Responsibilities, and nothing else:
 *
 * - Notice which problem is open, across SPA navigation.
 * - Feed the session state machine timing and interaction events.
 * - Detect a submission result via the adapter.
 * - Derive the resolution from the verdict, and hand the finished attempt to
 *   the background worker.
 *
 * It holds no credentials and makes no network calls. Every read of the page
 * goes through the adapter, so LeetCode's markup is knowable from exactly one
 * directory.
 */

import { leetcodeAdapter } from "../src/adapters/leetcode/adapter";
import {
  NETWORK_MESSAGE,
  type ObservedSubmission,
  type SubmissionKind,
} from "../src/adapters/leetcode/network";
import {
  buildAttemptEvent,
  emptySession,
  reduce,
  type SessionEvent,
  type SessionState,
} from "../src/lib/session";
import { observed } from "../src/lib/types";

export default defineContentScript({
  matches: ["https://leetcode.com/problems/*"],
  runAt: "document_idle",
  main() {
    start();
  },
});

function start(): void {
  let state: SessionState = emptySession();
  const dispatch = (event: SessionEvent): void => {
    state = reduce(state, event);
  };

  let orphaned = false;

  /**
   * Send to the background worker, surviving an orphaned content script.
   *
   * Reloading the extension does not replace the content scripts already
   * injected into open tabs. They keep running with a dead `chrome.runtime`, so
   * every send throws "Extension context invalidated" — and because the failure
   * is an unhandled rejection deep inside a MutationObserver callback, the
   * visible symptom is simply that nothing is ever captured. That is exactly
   * how it presented: LeetCode working, the API never hit, and no obvious cause.
   *
   * The page must be reloaded to get a live content script. Nothing here can
   * fix that, so this makes it *legible* instead: say so once, and stop
   * pretending to monitor.
   */
  const send = async (message: object): Promise<void> => {
    if (orphaned) return;
    try {
      await chrome.runtime.sendMessage(message);
    } catch (error) {
      if (!isContextInvalidated(error)) throw error;
      orphaned = true;
      stopObserving();
      console.warn(
        "[DSA Coach] The extension was reloaded, so this tab is no longer " +
          "being monitored. Reload the page to resume capturing attempts.",
      );
    }
  };

  const adapter = leetcodeAdapter;
  let lastOutcomeText = "";
  /// The kind of the last Run/Submit request seen, used to attribute a verdict
  /// the adapter could not place. Null until the network observer reports one.
  let lastRequestKind: SubmissionKind | null = null;

  const openCurrentProblem = (): void => {
    const url = location.href;
    if (!adapter.isProblemPage(url)) return;
    const slug = adapter.problemSlug(url);
    if (!slug.value) return;
    dispatch({
      type: "open",
      slug: slug.value,
      language: adapter.language(document),
      at: Date.now(),
    });
  };

  // --- SPA navigation. A full page load is the exception on LeetCode, so route
  // changes must be observed directly rather than relying on script re-injection.
  installNavigationHook(() => {
    if (state.phase === "working" && state.slug) dispatch({ type: "leave", at: Date.now() });
    lastOutcomeText = "";
    noteAidView();
    openCurrentProblem();
  });

  /**
   * Notice the editorial or a community solution being opened.
   *
   * The one thing telemetry cannot recover afterwards: an accepted verdict looks
   * identical whether you solved it yourself or read the answer first. With the
   * questionnaire gone, this is what keeps `independent` honest.
   *
   * Checked after `openCurrentProblem` has run at least once, and dispatched
   * before it on navigation, so the flag lands on the session for the problem
   * whose editorial was opened.
   */
  function noteAidView(): void {
    if (/\/(editorial|solutions)\b/i.test(location.pathname)) {
      dispatch({ type: "aid_viewed" });
    }
  }

  // --- Closing the tab.
  //
  // `installNavigationHook` only catches SPA route changes; closing the tab or
  // navigating off LeetCode entirely fires neither. Without this the session is
  // simply lost, taking its measured time with it (spec §3.6).
  //
  // `pagehide` rather than `beforeunload`: it fires on mobile and for
  // bfcache-restored pages, where `beforeunload` does not.
  window.addEventListener("pagehide", () => {
    if (state.phase === "working" && state.slug) dispatch({ type: "leave", at: Date.now() });
  });

  // --- Run and Submit, observed from LeetCode's own requests.
  //
  // The MAIN-world script (`leetcode-network.content.ts`) posts here whenever a
  // submission request goes out. This is the only reliable source of two things
  // the DOM could not give:
  //
  //   - `run_count`, which was structurally zero: the reducer has always had a
  //     `run` case and nothing ever dispatched one.
  //   - the language, previously guessed from a button label, which is why
  //     `capture_confidence` was capped at medium on every attempt.
  //
  // A submit is *not* dispatched here. The request only says one was sent; the
  // verdict comes later, and the DOM observer below remains what reports it.
  // Dispatching on the request would record an outcome before the judge had
  // returned one (invariant 5).
  window.addEventListener("message", (message: MessageEvent) => {
    if (message.source !== window) return;
    const data = message.data as ObservedSubmission | undefined;
    if (data?.source !== NETWORK_MESSAGE) return;
    if (state.phase !== "working" || state.slug !== data.slug) return;

    if (data.lang) {
      // Authoritative: LeetCode is telling us what it is about to compile.
      dispatch({ type: "language", language: observed(data.lang, "high") });
    }
    // Remembered so an ambiguous verdict can be attributed. A verdict the
    // adapter cannot place belongs to whichever request was last sent.
    lastRequestKind = data.kind;
    if (data.kind === "run") {
      dispatch({ type: "run", at: data.at });
    }
  });

  // --- Interaction and visibility, feeding the active-time accounting.
  for (const type of ["keydown", "pointerdown"] as const) {
    document.addEventListener(type, () => dispatch({ type: "input", at: Date.now() }), {
      passive: true,
      capture: true,
    });
  }
  document.addEventListener("visibilitychange", () => {
    dispatch({
      type: document.visibilityState === "visible" ? "visible" : "hidden",
      at: Date.now(),
    });
  });

  // --- Submission results.
  let observer: MutationObserver | null = null;
  const stopObserving = (): void => {
    observer?.disconnect();
    observer = null;
  };

  observer = new MutationObserver(() => {
    // The editor renders after `document_idle`, so this is where a failed
    // startup health check gets to recover.
    reportHealth();

    const { outcome, source } = adapter.submissionOutcome(document);
    if (!outcome.value) return;

    // The result panel persists after a submission, so the same verdict would
    // fire on every subsequent mutation. Only a *change* counts as new.
    const signature = `${outcome.value}:${outcome.confidence}:${source}`;
    if (signature === lastOutcomeText) return;
    lastOutcomeText = signature;

    // A Run prints a verdict too, in a different panel, worded identically.
    // Recording it as a submission invents an attempt that never happened
    // (invariant 5) — and, worse, `finish` then resets the session, so the runs
    // leading up to the real submission are destroyed along with it. That is
    // why `run_count` was always zero and why one problem produced an attempt
    // per Run.
    //
    // `unknown` means the fallback text scan matched and cannot tell the
    // panels apart; the last observed request decides. If the network observer
    // never reported anything either, fall through to treating it as a
    // submission — the previous behaviour, and the safer default for the case
    // where the observer is broken but the DOM still works.
    if (source === "console") return;
    if (source === "unknown" && lastRequestKind === "run") return;

    dispatch({ type: "submit", observation: { outcome, at: Date.now() } });
    void finish();
  });
  observer.observe(document.body, { childList: true, subtree: true, characterData: true });

  // --- Adapter health (spec §4.3).
  //
  // Re-checked as the page changes, not once at startup. LeetCode is a
  // single-page app and `document_idle` fires well before the editor pane
  // renders, so a single check at start reliably ran *before* the language
  // picker existed. Health failed, the extension went `degraded` — and because
  // nothing ever reported health again, it stayed there permanently, with no
  // route back short of re-pairing. Two of this session's debugging days ended
  // at that state.
  //
  // Only *changes* are reported, so the service worker is not told the same
  // thing on every mutation.
  let lastHealthy: boolean | null = null;

  const reportHealth = (): void => {
    const health = adapter.checkHealth(document);
    if (health.healthy === lastHealthy) return;
    lastHealthy = health.healthy;

    if (!health.healthy) dispatch({ type: "adapter_failed" });
    void send({
      type: "adapter_health",
      healthy: health.healthy,
      missing: health.missing,
    });
  };

  reportHealth();

  const finish = async (): Promise<void> => {
    // No questionnaire. The resolution is derived from the verdict plus whether
    // the editorial was opened; the blocker is left unknown until the
    // code-conclusion pipeline can infer it (spec §3.2).
    const event = buildAttemptEvent(state, { eventUuid: crypto.randomUUID() });
    if (!event) return;
    await send({ type: "capture", event });
    state = emptySession();
    openCurrentProblem();
  };

  openCurrentProblem();
  noteAidView();
}

/**
 * Whether an error is the orphaned-content-script one.
 *
 * Matched on the message because Chrome throws a plain `Error` for it — there
 * is no type or code to check. Narrow deliberately: any other failure is a real
 * one and must not be swallowed as "the extension was reloaded".
 */
function isContextInvalidated(error: unknown): boolean {
  return error instanceof Error && error.message.includes("Extension context invalidated");
}

/**
 * Fire `onChange` on any SPA route change.
 *
 * `popstate` alone misses `pushState`/`replaceState`, which is how a client-side
 * router actually navigates, so both are patched.
 */
function installNavigationHook(onChange: () => void): void {
  let lastUrl = location.href;
  const check = (): void => {
    if (location.href === lastUrl) return;
    lastUrl = location.href;
    onChange();
  };

  for (const method of ["pushState", "replaceState"] as const) {
    const original = history[method];
    history[method] = function patched(
      this: History,
      ...args: Parameters<History["pushState"]>
    ) {
      const result = original.apply(this, args);
      queueMicrotask(check);
      return result;
    };
  }

  window.addEventListener("popstate", check);
  // Belt and braces: some routers swap content without touching history.
  setInterval(check, 1000);
}
