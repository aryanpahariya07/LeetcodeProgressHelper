/**
 * MAIN-world content script: observes LeetCode's own submission requests.
 *
 * This exists as a separate entrypoint for one reason: a normal content script
 * runs in an **isolated world** and cannot see the page's `fetch`. Patching it
 * requires running in the page's own world, and code there has no access to
 * `chrome.*` APIs — so it can only `postMessage` across to
 * `leetcode.content.ts`, which holds the session and does the real work.
 *
 * Keep this file trivial. Everything that knows what a LeetCode request looks
 * like lives in `src/adapters/leetcode/network.ts`, so a route change is a
 * one-file fix in the same directory as the DOM selectors.
 */

import { installNetworkObserver } from "../src/adapters/leetcode/network";

export default defineContentScript({
  matches: ["https://leetcode.com/problems/*"],
  // Before the page's own scripts, so the patch is in place by the time
  // LeetCode captures a reference to `fetch`.
  runAt: "document_start",
  world: "MAIN",
  main() {
    installNetworkObserver();
  },
});
