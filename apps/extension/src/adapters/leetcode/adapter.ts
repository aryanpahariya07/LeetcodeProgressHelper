/**
 * The LeetCode DOM adapter.
 *
 * **Everything that knows LeetCode's markup lives in this directory.** Nothing
 * else in the extension may read the page. When LeetCode reships their frontend
 * — and they will, without notice — this is the only file that should need
 * touching.
 *
 * ## Why selectors here look the way they do
 *
 * LeetCode ships hashed CSS class names that change on essentially every
 * deploy, so any selector built on them is guaranteed to rot. In rough order of
 * durability, this adapter prefers:
 *
 * 1. **The URL** — `/problems/<slug>/` is a public contract. Effectively stable.
 * 2. **`data-e2e-locator` attributes** — LeetCode's own test hooks. They change
 *    less often than classes because their own tests depend on them.
 * 3. **User-facing text** ("Accepted", "Wrong Answer") — these are translated
 *    and occasionally reworded, but they cannot churn silently the way a hashed
 *    class can, because users would notice.
 *
 * Class names are used nowhere.
 *
 * ## UNVERIFIED
 *
 * These selectors have **not** been checked against a live LeetCode page (spec
 * §19). They are reasoned guesses. The fixture tests below prove the parsing
 * logic is correct *given* markup of a particular shape; they cannot prove the
 * shape is right. That requires loading a real problem page, and until it is
 * done this adapter should be assumed broken.
 *
 * When it is wrong, it must fail loudly rather than quietly: every read returns
 * an `Observed<T>` whose `confidence` says how much to trust it, and
 * `checkHealth` reports whether the adapter can find its anchors at all.
 */

import { observed, type Observed, type SubmissionOutcome } from "../../lib/types";

export interface AdapterHealth {
  healthy: boolean;
  /** Which anchors were missing, for the degraded-state message and telemetry. */
  missing: string[];
}

/**
 * Which surface a verdict came from — and therefore whether it is an attempt.
 *
 * This distinction is load-bearing. Pressing **Run** judges your code against
 * the sample cases and prints a verdict into the console panel; pressing
 * **Submit** judges it against the full suite and prints one into the
 * submission panel. They read identically ("Accepted", "Wrong Answer"), and
 * treating a Run verdict as a submission records an attempt that never
 * happened (invariant 5) *and* wipes the session, so the runs leading up to
 * the real submission are lost with it.
 *
 * - `submission` — Submit's own panel. A genuine attempt.
 * - `console` — Run's output. Not an attempt.
 * - `unknown` — matched by the fallback text scan, which cannot tell them
 *   apart. The caller decides, using the observed request kind (`network.ts`).
 */
export type VerdictSource = "submission" | "console" | "unknown";

export interface VerdictObservation {
  outcome: Observed<SubmissionOutcome>;
  source: VerdictSource;
}

export interface ProblemPageAdapter {
  readonly id: string;
  isProblemPage(url: string): boolean;
  problemSlug(url: string): Observed<string>;
  language(root: ParentNode): Observed<string>;
  submissionOutcome(root: ParentNode): VerdictObservation;
  checkHealth(root: ParentNode): AdapterHealth;
}

const PROBLEM_URL = /^https?:\/\/(?:www\.)?leetcode\.com\/problems\/([^/?#]+)/i;

/**
 * Judge verdicts, longest first so "Time Limit Exceeded" is not shadowed by a
 * shorter partial match.
 */
const OUTCOME_TEXT: Array<[string, SubmissionOutcome]> = [
  ["time limit exceeded", "tle"],
  ["memory limit exceeded", "runtime_error"],
  ["output limit exceeded", "wrong_answer"],
  ["compile error", "compile_error"],
  ["runtime error", "runtime_error"],
  ["wrong answer", "wrong_answer"],
  ["accepted", "accepted"],
];

/**
 * Where a verdict may appear. Two surfaces, not one:
 *
 * - `submission-result` — the verdict from **Submit**, judged against the full
 *   test suite. Confirmed present on a live page.
 * - `console-result` — the verdict from **Run**, judged against the sample
 *   cases only. A different element, and worth catching: how many times you ran
 *   before submitting is real evidence about how you work.
 *
 * Nothing speculative belongs in this list. A selector nobody has seen is not a
 * safety net, it is noise that outlives the reason it was added.
 */
const RESULT_SELECTORS: Array<[string, VerdictSource]> = [
  ['[data-e2e-locator="submission-result"]', "submission"],
  ['[data-e2e-locator="console-result"]', "console"],
];

/**
 * The console panel, used as the *scope* for a text scan.
 *
 * Scanning the whole document for a verdict is not safe: a problem page shows
 * "Accepted 2,554,913/3.9M  Acceptance Rate 66.1%" in its statistics before you
 * submit anything, and a body-wide scan reads that as a passing submission.
 * That would fabricate an attempt nobody made.
 *
 * The console is anchored by the run/submit buttons, which are stable locators,
 * so the scan is confined to the region a verdict can legitimately appear in.
 */
const CONSOLE_ANCHORS = [
  '[data-e2e-locator="console-submit-button"]',
  '[data-e2e-locator="console-run-button"]',
];

/**
 * Text that means "no verdict yet", so a stale or idle console is not read as
 * one. LeetCode shows these in the same region a verdict later occupies.
 */
const CONSOLE_IDLE = /you must run your code first|run your code|testcase|test case/i;

/**
 * The language picker has no locator of its own on observed pages — it is a
 * plain button whose text is the language ("C++"). Anchoring on the console and
 * matching known language names is the most stable read available.
 */
const LANGUAGE_SELECTORS = [
  "[data-e2e-locator='lang-select'] button",
  "[data-e2e-locator='lang-select']",
  "button[id^='headlessui-listbox-button']",
];

const KNOWN_LANGUAGES = [
  "python3",
  "python",
  "javascript",
  "typescript",
  "java",
  "c++",
  "csharp",
  "c#",
  "golang",
  "go",
  "kotlin",
  "swift",
  "rust",
  "ruby",
  "scala",
  "php",
  "c",
];

export class LeetCodeAdapter implements ProblemPageAdapter {
  readonly id = "leetcode-dom-v1";

  isProblemPage(url: string): boolean {
    return PROBLEM_URL.test(url);
  }

  /**
   * The slug, from the URL.
   *
   * The one genuinely reliable read in this file — it does not touch the DOM at
   * all, so a frontend rewrite cannot break it.
   */
  problemSlug(url: string): Observed<string> {
    const match = url.match(PROBLEM_URL);
    if (!match) return { value: null, confidence: "low" };
    return observed(match[1].toLowerCase(), "high");
  }

  language(root: ParentNode): Observed<string> {
    for (const selector of LANGUAGE_SELECTORS) {
      const text = textOf(root.querySelector(selector));
      const matched = matchLanguage(text);
      if (matched) return observed(matched, "high");
    }

    // No anchored picker. Fall back to buttons whose entire label is a language
    // name — "C++", "Python3". Requiring the *whole* label to match keeps this
    // from picking up prose that merely mentions a language.
    for (const button of root.querySelectorAll("button")) {
      const label = (button.textContent ?? "").trim();
      if (!label || label.length > 16) continue;
      const matched = matchLanguage(label);
      if (matched && matched.length >= label.length - 1) {
        // Medium: inferred from a label rather than an identified control.
        return observed(matched, "medium");
      }
    }

    // Rather than guess from arbitrary page text, report it as unknown
    // (invariant 6).
    return { value: null, confidence: "low" };
  }

  submissionOutcome(root: ParentNode): VerdictObservation {
    for (const [selector, source] of RESULT_SELECTORS) {
      const text = textOf(root.querySelector(selector));
      const matched = matchOutcome(text);
      if (matched) return { outcome: observed(matched, "high"), source };
    }

    // Scan the console panel only — never the whole document. A problem page
    // shows "Accepted 2,554,913/3.9M" in its statistics before any submission,
    // and a body-wide scan reads that as a passing attempt, inventing one
    // nobody made (invariant 5).
    const panel = consolePanel(root);
    if (panel) {
      const text = (panel.textContent ?? "").toLowerCase();
      if (!CONSOLE_IDLE.test(text)) {
        const matched = matchOutcome(text);
        // Medium: the region is right, but the exact element is not identified,
        // so a verdict left over from an earlier submission is possible.
        if (matched) return { outcome: observed(matched, "medium"), source: "unknown" };
      }
    }

    return { outcome: { value: null, confidence: "low" }, source: "unknown" };
  }

  checkHealth(root: ParentNode): AdapterHealth {
    // Health is judged on anchors that must exist on *any* loaded problem page.
    //
    // The result panel is deliberately not one of them: before you submit
    // anything there is no verdict to find, and reporting that as breakage
    // conflates "the selectors are wrong" with "you have not submitted yet" —
    // which is precisely the false alarm this check produced on first contact
    // with a real page.
    const missing: string[] = [];

    if (!consolePanel(root)) {
      missing.push("console (run/submit buttons)");
    }
    if (this.language(root).value === null) {
      missing.push("language picker");
    }

    return { healthy: missing.length === 0, missing };
  }
}

function textOf(node: Element | null): string {
  return (node?.textContent ?? "").toLowerCase();
}

/**
 * The console region, found by walking up from the run/submit buttons.
 *
 * Those buttons carry stable `data-e2e-locator` attributes, so they are a
 * reliable way to locate a region whose own markup is not. Four levels up is
 * far enough to include the result panel and near enough to exclude the problem
 * statement and its statistics.
 */
export function consolePanel(root: ParentNode): Element | null {
  for (const selector of CONSOLE_ANCHORS) {
    const button = root.querySelector(selector);
    if (!button) continue;
    let node: Element | null = button;
    for (let depth = 0; depth < 4 && node?.parentElement; depth += 1) {
      node = node.parentElement;
    }
    if (node) return node;
  }
  return null;
}

export function matchOutcome(text: string): SubmissionOutcome | null {
  const haystack = text.toLowerCase();
  for (const [needle, outcome] of OUTCOME_TEXT) {
    if (haystack.includes(needle)) return outcome;
  }
  return null;
}

export function matchLanguage(text: string): string | null {
  const haystack = text.toLowerCase().trim();
  if (!haystack) return null;
  // Token boundaries, not substrings. A plain `includes` reported "Brainfuck"
  // as C — the letter is in the word — and would have mislabelled a great deal
  // of ordinary page text the same way.
  //
  // Longest first, so "python3" is never reported as "python".
  for (const language of [...KNOWN_LANGUAGES].sort((a, b) => b.length - a.length)) {
    if (languagePattern(language).test(haystack)) return language;
  }
  return null;
}

const PATTERN_CACHE = new Map<string, RegExp>();

function languagePattern(language: string): RegExp {
  const cached = PATTERN_CACHE.get(language);
  if (cached) return cached;
  const escaped = language.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  // A language token may not run into an adjacent word character, digit, + or #.
  const pattern = new RegExp(`(?<![a-z0-9+#])${escaped}(?![a-z0-9+#])`);
  PATTERN_CACHE.set(language, pattern);
  return pattern;
}

export const leetcodeAdapter = new LeetCodeAdapter();
