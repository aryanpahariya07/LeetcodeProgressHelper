/**
 * Tests for the network observer.
 *
 * The fixtures are real: captured from an actual Run and Submit on
 * `remove-element` on 2026-09-09, not invented to match the parser. That
 * distinction is the whole point — the DOM adapter's first version was written
 * against guessed markup and was wrong in ways no test could catch, because the
 * tests encoded the same guess.
 *
 * The load-bearing test is `test the code is never read`. Code capture requires
 * explicit consent (invariant 9), and `typed_code` sits right there in the body
 * this module already parses. Nothing but a test stops it being picked up by
 * accident.
 */

import { describe, expect, it, vi } from "vitest";

import { NETWORK_MESSAGE, classify, installNetworkObserver } from "./network";

const AT = 1_700_000_000_000;

/** Verbatim from a real Run request. */
const RUN_BODY = JSON.stringify({
  lang: "java",
  question_id: "27",
  typed_code:
    "class Solution {\n    public int removeElement(int[] nums, int val) {\n        int c=0;\n        for(int x:nums) if(x!=val) c++;\n\n        return c;\n    }\n}",
  data_input: "[3,2,2,3]\n3\n[0,1,2,2,3,0,4,2]\n2",
});

/** Verbatim from a real Submit request — same shape, no `data_input`. */
const SUBMIT_BODY = JSON.stringify({
  lang: "java",
  question_id: "27",
  typed_code:
    "class Solution {\n    public int removeElement(int[] nums, int val) {\n        int c=0;\n        for(int x:nums) if(x!=val) c++;\n\n        return c;\n    }\n}",
});

const RUN_URL = "https://leetcode.com/problems/remove-element/interpret_solution/";
const SUBMIT_URL = "https://leetcode.com/problems/remove-element/submit/";

describe("classifying a request", () => {
  it("recognises a Run", () => {
    const result = classify(RUN_URL, RUN_BODY, AT);

    expect(result?.kind).toBe("run");
    expect(result?.slug).toBe("remove-element");
  });

  it("recognises a Submit", () => {
    const result = classify(SUBMIT_URL, SUBMIT_BODY, AT);

    expect(result?.kind).toBe("submit");
    expect(result?.slug).toBe("remove-element");
  });

  it("reads the language LeetCode states, rather than guessing it", () => {
    // The reason this module exists: the DOM adapter could only infer the
    // language from a button label, capping capture_confidence at medium.
    expect(classify(RUN_URL, RUN_BODY, AT)?.lang).toBe("java");
  });

  it("reads the question id", () => {
    expect(classify(SUBMIT_URL, SUBMIT_BODY, AT)?.questionId).toBe("27");
  });

  it("ignores every other request on the page", () => {
    for (const url of [
      "https://leetcode.com/graphql",
      "https://leetcode.com/problems/remove-element/",
      "https://leetcode.com/submissions/detail/123/check/",
      "https://leetcode.com/problems/remove-element/interpret_solution_x/",
    ]) {
      expect(classify(url, RUN_BODY, AT)).toBeNull();
    }
  });

  it("still reports the event when the body cannot be parsed", () => {
    // The URL alone proves a run happened. Losing the event because a field
    // moved would be worse than reporting it with the fields unknown.
    const result = classify(RUN_URL, "not json at all", AT);

    expect(result?.kind).toBe("run");
    expect(result?.lang).toBeNull();
    expect(result?.questionId).toBeNull();
  });

  it("reports unknown rather than empty for missing fields", () => {
    // Invariant 6: absent is null, never a guess or a blank string.
    const result = classify(RUN_URL, JSON.stringify({ lang: "" }), AT);

    expect(result?.lang).toBeNull();
  });

  it("tolerates a trailing-slash-free URL", () => {
    expect(classify(SUBMIT_URL.replace(/\/$/, ""), SUBMIT_BODY, AT)?.kind).toBe("submit");
  });
});

describe("what is deliberately not read", () => {
  it("never extracts the code", () => {
    // Invariant 9: code capture is off by default and needs explicit consent.
    // `typed_code` is in the body being parsed, so only this test stands
    // between "we parse the body" and "we quietly started collecting code".
    const result = classify(RUN_URL, RUN_BODY, AT);

    expect(JSON.stringify(result)).not.toContain("class Solution");
    expect(JSON.stringify(result)).not.toContain("typed_code");
    expect(Object.keys(result ?? {}).sort()).toEqual([
      "at",
      "kind",
      "lang",
      "questionId",
      "slug",
      "source",
    ]);
  });

  it("does not read the custom test input either", () => {
    expect(JSON.stringify(classify(RUN_URL, RUN_BODY, AT))).not.toContain("data_input");
  });
});

describe("patching the page", () => {
  function fakeWindow() {
    const posted: unknown[] = [];
    const fetchCalls: unknown[] = [];
    const win = {
      location: { origin: "https://leetcode.com" },
      postMessage: (data: unknown) => posted.push(data),
      fetch: (input: unknown, init?: unknown) => {
        fetchCalls.push([input, init]);
        return Promise.resolve("original response" as unknown as Response);
      },
    } as unknown as Window;
    return { win, posted, fetchCalls };
  }

  it("posts an observation when a Run request goes out", async () => {
    const { win, posted } = fakeWindow();
    installNetworkObserver(win);

    await win.fetch(RUN_URL, { method: "POST", body: RUN_BODY });

    expect(posted).toHaveLength(1);
    expect((posted[0] as { kind: string }).kind).toBe("run");
    expect((posted[0] as { source: string }).source).toBe(NETWORK_MESSAGE);
  });

  it("passes the request through untouched", async () => {
    // A pass-through in both directions: LeetCode must behave identically
    // whether or not the extension is installed.
    const { win, fetchCalls } = fakeWindow();
    installNetworkObserver(win);

    const response = await win.fetch(RUN_URL, { method: "POST", body: RUN_BODY });

    expect(response).toBe("original response");
    expect(fetchCalls).toHaveLength(1);
  });

  it("does not post for unrelated requests", async () => {
    const { win, posted } = fakeWindow();
    installNetworkObserver(win);

    await win.fetch("https://leetcode.com/graphql", { method: "POST", body: "{}" });

    expect(posted).toHaveLength(0);
  });

  it("still completes the request if observing throws", async () => {
    // Observation must never break the page.
    const { win, fetchCalls } = fakeWindow();
    installNetworkObserver(win);
    vi.spyOn(win, "postMessage").mockImplementation(() => {
      throw new Error("boom");
    });

    const response = await win.fetch(RUN_URL, { method: "POST", body: RUN_BODY });

    expect(response).toBe("original response");
    expect(fetchCalls).toHaveLength(1);
  });
});
