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

import {
  leetcodeAdapter,
  type VerdictObservation,
} from "../src/adapters/leetcode/adapter";
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
  /**
   * A submission is in flight and its verdict has not been recorded yet.
   *
   * Set when the observer sees a submit request, cleared when a verdict is
   * recorded. This — not the presence of a verdict in the DOM — is what marks
   * an attempt, because the DOM shows old verdicts on page load and shows Run
   * verdicts in the same panels.
   */
  let awaitingVerdict = false;

  /**
   * Whether the network observer has ever reported anything.
   *
   * Until it has, there is no way to know a submission occurred, so the DOM is
   * trusted on its own. Once it has proven itself, its silence is meaningful.
   */
  let observerHasReported = false;

  /**
   * The source last submitted, held only until the attempt is sent.
   *
   * Kept in memory and never written anywhere by the content script. Whether it
   * leaves this tab is the server's decision, asked for below.
   */
  let submittedCode: string | null = null;

  /**
   * Whether the server currently permits code capture (invariant 9).
   *
   * Asked rather than assumed, and re-asked per attempt, so revoking consent in
   * the dashboard stops capture at the source rather than merely stopping
   * storage. Any failure answers "no": if permission cannot be established,
   * code is not sent.
   */
  const codeCaptureAllowed = async (): Promise<boolean> => {
    try {
      const config = await chrome.runtime.sendMessage({ type: "get_config" });
      return config?.code_capture_enabled === true;
    } catch {
      return false;
    }
  };

  /**
   * Whether the user has done anything at all this session.
   *
   * `start` is the only timeline entry a freshly-opened session has; a keypress,
   * a pointer press or a Run each add one more. Used to tell a verdict that was
   * already on screen from one the user just produced.
   */
  const hasInteracted = (): boolean =>
    state.timeline.some((entry) => entry.kind === "input");

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

    // Whatever verdict is already on screen belongs to the past.
    //
    // Opening `/problems/<slug>/submissions/<id>/` — which is where LeetCode
    // lands you after submitting — renders that earlier verdict into
    // `submission-result`. Without this it is read as a fresh attempt the
    // moment the observer first fires, inventing one attempt per page load.
    // Seeding the dedup signature means only a *change* from here counts.
    lastOutcomeText = signatureOf(adapter.submissionOutcome(document));
  };

  // --- SPA navigation. A full page load is the exception on LeetCode, so route
  // changes must be observed directly rather than relying on script re-injection.
  installNavigationHook(() => {
    // Only a move to a *different problem* ends the session.
    //
    // LeetCode routes between a problem's tabs — description, submissions,
    // editorial — and submitting navigates to `/problems/<slug>/submissions/<id>/`
    // on its own. Treating that as leaving parked the session and opened a
    // fresh one *before* the verdict rendered, so every attempt was recorded
    // against a session that had just been created: `run_count` back to zero,
    // `active_seconds` a couple of seconds, and the runs leading up to the
    // submission gone. Switching tabs is not leaving.
    const nextSlug = adapter.problemSlug(location.href).value;
    const movedOn = state.slug !== null && nextSlug !== state.slug;

    if (movedOn) {
      if (state.phase === "working") dispatch({ type: "leave", at: Date.now() });
      // A verdict from the previous problem must not be mistaken for one here.
      lastOutcomeText = "";
      awaitingVerdict = false;
    }

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
    // Guarded on origin rather than `message.source !== window`. A content
    // script runs in an isolated world, so its `window` is a different object
    // from the page's, and identity comparison across that boundary is not
    // dependable — it silently rejected every message, leaving `run_count` at
    // zero and the language stuck on the DOM's guess.
    //
    // Origin is the guard that actually matters: it keeps an embedded frame
    // from injecting events. The marker below then distinguishes ours from
    // whatever else the page posts to itself.
    if (message.origin !== window.location.origin) return;
    const data = message.data as ObservedSubmission | undefined;
    if (data?.source !== NETWORK_MESSAGE) return;

    // Recorded before the session guard below, because whether the observer
    // works is a fact about the observer, not about this session. Setting it
    // afterwards meant a single early return — a message arriving while the
    // session was between states — left it false forever, and the DOM fallback
    // then treated every Run verdict as a submission.
    observerHasReported = true;

    if (state.phase !== "working" || state.slug !== data.slug) return;

    if (data.lang) {
      // Authoritative: LeetCode is telling us what it is about to compile.
      dispatch({ type: "language", language: observed(data.lang, "high") });
    }
    // Every Run and Submit's source, kept as it happens (spec §3.6). The
    // sequence is what carries the information — a clean solve and four passes
    // at the same off-by-one differ only in the order they arrived — so runs
    // are sent as they occur rather than waiting for an attempt that a run may
    // never produce.
    //
    // Fire-and-forget: capture must never delay or interfere with practice, and
    // the server refuses these outright without consent (invariant 9).
    if (data.typedCode) {
      void send({
        type: "snapshot",
        snapshot: {
          snapshot_uuid: crypto.randomUUID(),
          problem_slug: data.slug,
          kind: data.kind,
          language: data.lang,
          code: data.typedCode,
          captured_at: new Date(data.at).toISOString(),
        },
      });
    }

    if (data.kind === "run") {
      dispatch({ type: "run", at: data.at });
    } else {
      // A submission is now in flight. The next verdict the DOM produces is
      // its outcome, and is the one worth recording.
      awaitingVerdict = true;
      submittedCode = data.typedCode;
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
    const signature = signatureOf({ outcome, source });
    if (signature === lastOutcomeText) return;
    lastOutcomeText = signature;


    // A Run prints a verdict too, in a different panel, worded identically.
    // Recording it as a submission invents an attempt that never happened
    // (invariant 5) — and, worse, `finish` then resets the session, so the runs
    // leading up to the real submission are destroyed along with it. That is
    // why `run_count` was always zero and why one problem produced an attempt
    // per Run.
    //
    // A verdict in the DOM says what the outcome was. It does not say that a
    // submission just happened, and the two are genuinely different questions:
    //
    //   - Opening `/problems/<slug>/submissions/<id>/` renders a *previous*
    //     verdict into `submission-result`. Reading that as a new attempt
    //     invents one, and every page load produced another.
    //   - A Run renders its own verdict into `console-result`, and a Submit
    //     that fails to compile renders there too — so the panel alone cannot
    //     separate them either.
    //
    // The submit *request* is what actually marks a submission, so that is what
    // gates recording. The DOM is left to supply the outcome, which is the one
    // thing it is reliable about. `awaitingVerdict` is consumed here so a
    // single submission cannot yield two attempts.
    //
    // If the network observer has never reported anything — broken, or a
    // LeetCode change — this falls back to trusting the DOM, because losing
    // every attempt is worse than occasionally recording a stale one.
    if (observerHasReported) {
      if (!awaitingVerdict) return;
      awaitingVerdict = false;
    } else if (!hasInteracted()) {
      // The fallback, without this, records the verdict already on screen when
      // the page loads. Landing on `/problems/<slug>/submissions/<id>/` — where
      // LeetCode leaves you after submitting, and therefore where a reload
      // starts — renders that old verdict a moment *after* the session opens,
      // so seeding the dedup signature cannot catch it: at seeding time there
      // is nothing on screen yet.
      //
      // An attempt requires evidence that the user did something. No keypress,
      // no click, no run: nothing has happened this session, so whatever is
      // displayed belongs to a previous one.
      return;
    }

    dispatch({ type: "submit", observation: { outcome, at: Date.now() } });
    void finish();
  });
  // The session must exist (and its dedup signature be seeded with whatever
  // verdict is already on screen) before the observer can fire, or a mutation
  // in the gap is read against an empty signature and invents an attempt.
  openCurrentProblem();
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

    dispatch({ type: "adapter_health", healthy: health.healthy });
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

    // Attached only with the server's permission, asked for per attempt
    // (invariant 9). The server checks consent again before storing.
    if (submittedCode && (await codeCaptureAllowed())) {
      event.code = submittedCode;
    }
    submittedCode = null;

    await send({ type: "capture", event });
    state = emptySession();
    openCurrentProblem();
  };

  noteAidView();
}

/** A verdict's identity, so the same one is not reported twice. */
function signatureOf(observation: VerdictObservation): string {
  const { outcome, source } = observation;
  return `${outcome.value}:${outcome.confidence}:${source}`;
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
