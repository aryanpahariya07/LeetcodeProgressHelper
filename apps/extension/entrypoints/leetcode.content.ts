/**
 * Content script: watches a LeetCode problem page.
 *
 * Responsibilities, and nothing else:
 *
 * - Notice which problem is open, across SPA navigation.
 * - Feed the session state machine timing and interaction events.
 * - Detect a submission result via the adapter.
 * - Ask the 1–2 click questionnaire, and hand the finished attempt to the
 *   background worker.
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
import { askQuestionnaire } from "../src/ui/questionnaire";
import { observed, type SubmissionOutcome } from "../src/lib/types";

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
    openCurrentProblem();
  });

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
    void finish(outcome.value);
  });
  observer.observe(document.body, { childList: true, subtree: true, characterData: true });

  // --- Adapter health. If the anchors are gone, say so loudly (spec §4.3).
  const health = adapter.checkHealth(document);
  if (!health.healthy) {
    dispatch({ type: "adapter_failed" });
  }
  void send({
    type: "adapter_health",
    healthy: health.healthy,
    missing: health.missing,
  });

  const finish = async (outcome: SubmissionOutcome): Promise<void> => {
    const answer = await askQuestionnaire(outcome);
    dispatch(answer ? { type: "answer", answer } : { type: "dismiss" });

    const event = buildAttemptEvent(state, { eventUuid: crypto.randomUUID() });
    if (!event) return;
    await send({ type: "capture", event });
    state = emptySession();
    openCurrentProblem();
  };

  openCurrentProblem();
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
