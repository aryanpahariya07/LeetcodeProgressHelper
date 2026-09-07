/**
 * Adapter tests against markup shaped like a real LeetCode page.
 *
 * The fixtures here are built from a DOM dump of an actual problem page, not
 * from what the adapter wished it would find. The locators are the ones LeetCode
 * genuinely ships (`console-run-button`, `console-submit-button`,
 * `console-testcase-*`) — notably *not* `submission-result` or `lang-select`,
 * which the first version of this adapter guessed at and which do not exist.
 *
 * The most important test in this file is `test the statistics panel`. A problem
 * page renders "Accepted 2,554,913/3.9M Acceptance Rate 66.1%" *before you
 * submit anything*, and an earlier body-wide text scan read that as a passing
 * submission — inventing an attempt nobody made.
 */

import { JSDOM } from "jsdom";
import { describe, expect, it } from "vitest";

import { LeetCodeAdapter, consolePanel } from "./adapter";

const adapter = new LeetCodeAdapter();

/** The statistics block every problem page shows, submitted or not. */
const STATS = `
  <div class="stats">
    <span>Accepted</span><span>2,554,913/3.9M</span>
    <span>Acceptance Rate</span><span>66.1%</span>
  </div>
`;

const LANGUAGE_PICKER = `<button type="button">C++</button>`;

function page(consoleBody: string, extra = ""): ParentNode {
  return new JSDOM(`
    <body>
      <div class="description">
        <h1>485. Max Consecutive Ones</h1>
        ${STATS}
      </div>
      <div class="editor">${LANGUAGE_PICKER}</div>
      <div class="console-wrapper"><div class="a"><div class="b"><div class="c">
        <button data-e2e-locator="console-run-button">Run</button>
        <button data-e2e-locator="console-submit-button">Submit</button>
        <div class="result">${consoleBody}</div>
      </div></div></div></div>
      ${extra}
    </body>
  `).window.document;
}

const IDLE_CONSOLE = `
  <div data-e2e-locator="console-testcase-tag">Case 1</div>
  <div data-e2e-locator="console-testcase-tag">Case 2</div>
  <div data-e2e-locator="console-testcase-input">[1,1,0,1,1,1]</div>
`;

describe("locating the console", () => {
  it("finds it from the submit button", () => {
    expect(consolePanel(page(IDLE_CONSOLE))).not.toBeNull();
  });

  it("returns null when the page has no console at all", () => {
    const dom = new JSDOM(`<body><div>${STATS}</div></body>`).window.document;

    expect(consolePanel(dom)).toBeNull();
  });

  it("does not reach up as far as the problem statement", () => {
    const panel = consolePanel(page(IDLE_CONSOLE));

    expect(panel?.textContent).not.toContain("Max Consecutive Ones");
  });
});

describe("submission outcome", () => {
  it("does NOT read the statistics panel as a verdict", () => {
    // The bug this whole fixture exists for. "Accepted 2,554,913/3.9M" is on
    // the page before any submission; treating it as one fabricates an attempt.
    const result = adapter.submissionOutcome(page(IDLE_CONSOLE));

    expect(result.value).toBeNull();
    expect(result.confidence).toBe("low");
  });

  it("does not report a verdict from an idle console", () => {
    expect(adapter.submissionOutcome(page(IDLE_CONSOLE)).value).toBeNull();
  });

  it("does not report a verdict before the code has been run", () => {
    const result = adapter.submissionOutcome(
      page(`<div>You must run your code first</div>`),
    );

    expect(result.value).toBeNull();
  });

  it("reads an accepted verdict from the console", () => {
    const result = adapter.submissionOutcome(page(`<span>Accepted</span>`));

    expect(result.value).toBe("accepted");
  });

  it.each([
    ["Wrong Answer", "wrong_answer"],
    ["Time Limit Exceeded", "tle"],
    ["Runtime Error", "runtime_error"],
    ["Compile Error", "compile_error"],
  ])("reads %s", (text, expected) => {
    expect(adapter.submissionOutcome(page(`<span>${text}</span>`)).value).toBe(expected);
  });

  it("reads the real post-submission markup", () => {
    // Copied verbatim from a live submission. `submission-result` does exist —
    // it simply is not on the page until you submit, which is why a pre-submit
    // DOM dump appeared to show the locator missing.
    const dom = page(
      IDLE_CONSOLE,
      `<div class="text-green-s dark:text-dark-green-s flex flex-1 items-center gap-2 text-[16px] font-medium leading-6">
         <span data-e2e-locator="submission-result">Accepted</span>
         <div class="text-xs font-normal text-text-tertiary"><span>75 / 75 </span>testcases passed</div>
       </div>`,
    );

    const result = adapter.submissionOutcome(dom);

    expect(result.value).toBe("accepted");
    expect(result.confidence).toBe("high");
  });

  it("reads a failing verdict from the same element", () => {
    const dom = page(
      IDLE_CONSOLE,
      `<div class="text-red-s flex flex-1 items-center gap-2">
         <span data-e2e-locator="submission-result">Wrong Answer</span>
         <div class="text-xs"><span>43 / 75 </span>testcases passed</div>
       </div>`,
    );

    expect(adapter.submissionOutcome(dom).value).toBe("wrong_answer");
  });

  it("the identified element wins even while the stats say Accepted", () => {
    // Both are on the page at once after a failed submission. The locator must
    // decide, not the statistics block.
    const dom = page(
      IDLE_CONSOLE,
      `<span data-e2e-locator="submission-result">Wrong Answer</span>`,
    );

    expect(adapter.submissionOutcome(dom).value).toBe("wrong_answer");
  });

  it("prefers an identified result element over a text scan", () => {
    const dom = page(
      `<span>Wrong Answer</span>`,
      `<div data-e2e-locator="console-result">Accepted</div>`,
    );
    const result = adapter.submissionOutcome(dom);

    expect(result.value).toBe("accepted");
    expect(result.confidence).toBe("high");
  });

  it("marks a scanned verdict as medium, not high", () => {
    // The region is right but the element is not identified, so a verdict left
    // over from an earlier submission is possible. Say so.
    expect(adapter.submissionOutcome(page(`<span>Accepted</span>`)).confidence).toBe(
      "medium",
    );
  });
});

describe("language", () => {
  it("reads it from a plain button label", () => {
    const result = adapter.language(page(IDLE_CONSOLE));

    expect(result.value).toBe("c++");
    expect(result.confidence).toBe("medium");
  });

  it("prefers an identified picker when one exists", () => {
    const dom = page(
      IDLE_CONSOLE,
      `<div data-e2e-locator="lang-select"><button>Python3</button></div>`,
    );
    const result = adapter.language(dom);

    expect(result.value).toBe("python3");
    expect(result.confidence).toBe("high");
  });

  it("ignores buttons that merely mention a language", () => {
    const dom = page(IDLE_CONSOLE, `<button>Switch to C++ mode please</button>`);

    // The C++ picker is still found; the prose button must not be preferred.
    expect(adapter.language(dom).value).toBe("c++");
  });

  it("reports unknown rather than guessing", () => {
    const dom = new JSDOM(
      `<body><button data-e2e-locator="console-run-button">Run</button></body>`,
    ).window.document;

    expect(adapter.language(dom).value).toBeNull();
  });
});

describe("health", () => {
  it("a loaded problem page is healthy before any submission", () => {
    // The false alarm that started this: no verdict yet is not breakage.
    const health = adapter.checkHealth(page(IDLE_CONSOLE));

    expect(health.healthy).toBe(true);
    expect(health.missing).toEqual([]);
  });

  it("a page with no console is reported as broken", () => {
    const dom = new JSDOM(`<body><div>${STATS}</div></body>`).window.document;
    const health = adapter.checkHealth(dom);

    expect(health.healthy).toBe(false);
    expect(health.missing).toContain("console (run/submit buttons)");
  });

  it("says which anchor is missing, for the degraded-state message", () => {
    const dom = new JSDOM(
      `<body><button data-e2e-locator="console-run-button">Run</button></body>`,
    ).window.document;

    expect(adapter.checkHealth(dom).missing).toContain("language picker");
  });
});

describe("the slug still comes from the URL", () => {
  it("is unaffected by any markup change", () => {
    const result = adapter.problemSlug(
      "https://leetcode.com/problems/max-consecutive-ones/description/",
    );

    expect(result.value).toBe("max-consecutive-ones");
    expect(result.confidence).toBe("high");
  });
});
