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

  const adapter = leetcodeAdapter;
  let lastOutcomeText = "";

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
  const observer = new MutationObserver(() => {
    const observation = adapter.submissionOutcome(document);
    if (!observation.value) return;

    // The result panel persists after a submission, so the same verdict would
    // fire on every subsequent mutation. Only a *change* counts as new.
    const signature = `${observation.value}:${observation.confidence}`;
    if (signature === lastOutcomeText) return;
    lastOutcomeText = signature;

    dispatch({ type: "submit", observation: { outcome: observation, at: Date.now() } });
    void finish(observation.value);
  });
  observer.observe(document.body, { childList: true, subtree: true, characterData: true });

  // --- Adapter health. If the anchors are gone, say so loudly (spec §4.3).
  const health = adapter.checkHealth(document);
  if (!health.healthy) {
    dispatch({ type: "adapter_failed" });
  }
  void chrome.runtime.sendMessage({
    type: "adapter_health",
    healthy: health.healthy,
    missing: health.missing,
  });

  const finish = async (outcome: SubmissionOutcome): Promise<void> => {
    const answer = await askQuestionnaire(outcome);
    dispatch(answer ? { type: "answer", answer } : { type: "dismiss" });

    const event = buildAttemptEvent(state, { eventUuid: crypto.randomUUID() });
    if (!event) return;
    await chrome.runtime.sendMessage({ type: "capture", event });
    state = emptySession();
    openCurrentProblem();
  };

  openCurrentProblem();
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
