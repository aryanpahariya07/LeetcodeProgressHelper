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

export interface ProblemPageAdapter {
  readonly id: string;
  isProblemPage(url: string): boolean;
  problemSlug(url: string): Observed<string>;
  language(root: ParentNode): Observed<string>;
  submissionOutcome(root: ParentNode): Observed<SubmissionOutcome>;
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

const RESULT_SELECTORS = [
  '[data-e2e-locator="submission-result"]',
  '[data-e2e-locator="console-result"]',
];

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

    // Nothing anchored matched. Rather than guess from arbitrary page text,
    // report that it is unknown (invariant 6).
    return { value: null, confidence: "low" };
  }

  submissionOutcome(root: ParentNode): Observed<SubmissionOutcome> {
    for (const selector of RESULT_SELECTORS) {
      const text = textOf(root.querySelector(selector));
      const matched = matchOutcome(text);
      if (matched) return observed(matched, "high");
    }

    // Fall back to scanning for a verdict anywhere on the page. This can pick up
    // a stale panel from a previous submission, so it is explicitly medium.
    const matched = matchOutcome(textOf(root.querySelector("body")));
    if (matched) return observed(matched, "medium");

    return { value: null, confidence: "low" };
  }

  checkHealth(root: ParentNode): AdapterHealth {
    const missing: string[] = [];
    if (!RESULT_SELECTORS.some((s) => root.querySelector(s))) {
      missing.push("submission-result");
    }
    if (!LANGUAGE_SELECTORS.some((s) => root.querySelector(s))) {
      missing.push("lang-select");
    }
    return { healthy: missing.length === 0, missing };
  }
}

function textOf(node: Element | null): string {
  return (node?.textContent ?? "").toLowerCase();
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
