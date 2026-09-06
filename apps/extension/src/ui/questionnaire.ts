/**
 * The post-submission questionnaire (spec §3.2).
 *
 * The entire manual burden of the product is this panel, so its constraints are
 * strict:
 *
 * - **One click on the common path.** "Solved it independently" is one button.
 * - **A second click only when needed.** The blocker is asked only when the
 *   answer was not "independently", because that is the only time it means
 *   anything.
 * - **Dismissable, always.** Dismissal is missing information, not a failure —
 *   the attempt is still recorded, with no resolution.
 * - **Never blocks the page.** It is a small corner panel, not a modal. Anything
 *   that interrupts solving would get the extension uninstalled.
 *
 * Built with plain DOM rather than React: it is injected into someone else's
 * page, and a framework runtime is a lot of weight (and a lot of style
 * collision surface) for six buttons.
 */

import type { Blocker, Resolution, SubmissionOutcome } from "../lib/types";

export interface Answer {
  resolution: Resolution;
  blocker?: Blocker | null;
}

const HOST_ID = "dsa-coach-questionnaire";

const RESOLUTIONS: Array<[Resolution, string]> = [
  ["independent", "Solved it myself"],
  ["after_hint", "After a hint"],
  ["after_editorial", "After the editorial"],
  ["failed", "Didn't solve it"],
];

const BLOCKERS: Array<[Blocker, string]> = [
  ["pattern_not_recognized", "Didn't spot the pattern"],
  ["pattern_known_impl_failed", "Knew it, botched the code"],
  ["edge_cases", "Edge cases"],
  ["complexity", "Too slow"],
  ["data_structure_choice", "Wrong data structure"],
  ["language_api", "Language / API"],
  ["misread_problem", "Misread it"],
];

/** How long the panel waits before giving up and recording a dismissal. */
const AUTO_DISMISS_MS = 90_000;

export function askQuestionnaire(
  outcome: SubmissionOutcome,
): Promise<Answer | null> {
  document.getElementById(HOST_ID)?.remove();

  return new Promise((resolve) => {
    const host = document.createElement("div");
    host.id = HOST_ID;
    // A shadow root so LeetCode's stylesheet cannot reach in, and ours cannot
    // leak out and break their page.
    const shadow = host.attachShadow({ mode: "closed" });
    shadow.append(styles(), panel(outcome, finish));
    document.body.append(host);

    const timer = window.setTimeout(() => finish(null), AUTO_DISMISS_MS);

    function finish(answer: Answer | null): void {
      window.clearTimeout(timer);
      host.remove();
      resolve(answer);
    }
  });
}

function panel(
  outcome: SubmissionOutcome,
  finish: (answer: Answer | null) => void,
): HTMLElement {
  const root = document.createElement("div");
  root.className = "panel";
  root.setAttribute("role", "dialog");
  root.setAttribute("aria-label", "How did that attempt go?");

  const heading = document.createElement("p");
  heading.className = "heading";
  heading.textContent =
    outcome === "accepted" ? "Accepted — how did it go?" : "How did that go?";
  root.append(heading);

  const choices = document.createElement("div");
  choices.className = "choices";
  for (const [resolution, label] of RESOLUTIONS) {
    const button = document.createElement("button");
    button.textContent = label;
    button.className = resolution === "independent" ? "primary" : "";
    button.addEventListener("click", () => {
      if (resolution === "independent") {
        // The common path ends here: one click, done.
        finish({ resolution });
        return;
      }
      root.replaceChildren(blockerStep(resolution, finish));
    });
    choices.append(button);
  }
  root.append(choices);

  const dismiss = document.createElement("button");
  dismiss.className = "dismiss";
  dismiss.textContent = "Skip";
  dismiss.setAttribute("aria-label", "Skip this question");
  dismiss.addEventListener("click", () => finish(null));
  root.append(dismiss);

  return root;
}

function blockerStep(
  resolution: Resolution,
  finish: (answer: Answer | null) => void,
): HTMLElement {
  const step = document.createElement("div");

  const heading = document.createElement("p");
  heading.className = "heading";
  heading.textContent = "What got in the way?";
  step.append(heading);

  const choices = document.createElement("div");
  choices.className = "choices";
  for (const [blocker, label] of BLOCKERS) {
    const button = document.createElement("button");
    button.textContent = label;
    button.addEventListener("click", () => finish({ resolution, blocker }));
    choices.append(button);
  }
  step.append(choices);

  const skip = document.createElement("button");
  skip.className = "dismiss";
  skip.textContent = "Not sure";
  // Still records the resolution — the half we do know is worth keeping.
  skip.addEventListener("click", () => finish({ resolution, blocker: null }));
  step.append(skip);

  return step;
}

function styles(): HTMLStyleElement {
  const style = document.createElement("style");
  style.textContent = `
    .panel {
      position: fixed; right: 16px; bottom: 16px; z-index: 2147483647;
      width: 260px; padding: 14px; border-radius: 10px;
      background: #ffffff; color: #0f172a;
      border: 1px solid #cbd5e1;
      box-shadow: 0 8px 24px rgba(15, 23, 42, 0.18);
      font: 13px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif;
    }
    @media (prefers-color-scheme: dark) {
      .panel { background: #1e293b; color: #e2e8f0; border-color: #334155; }
      .panel button { background: #334155; color: #e2e8f0; border-color: #475569; }
      .panel button.primary { background: #e2e8f0; color: #0f172a; }
    }
    .heading { margin: 0 0 10px; font-weight: 600; }
    .choices { display: flex; flex-direction: column; gap: 6px; }
    .panel button {
      width: 100%; padding: 7px 10px; text-align: left; cursor: pointer;
      border-radius: 6px; border: 1px solid #cbd5e1; background: #f8fafc;
      color: inherit; font: inherit;
    }
    .panel button:hover { border-color: #94a3b8; }
    .panel button.primary { background: #0f172a; color: #ffffff; border-color: #0f172a; }
    .panel button.dismiss {
      margin-top: 10px; border: none; background: none; text-align: center;
      opacity: 0.65; padding: 4px;
    }
    .panel button:focus-visible { outline: 2px solid #2563eb; outline-offset: 2px; }
  `;
  return style;
}
