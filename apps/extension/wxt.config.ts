import { defineConfig } from "wxt";

export default defineConfig({
  modules: ["@wxt-dev/module-react"],
  srcDir: ".",
  manifest: {
    name: "DSA Coach",
    description:
      "Records your LeetCode attempts so DSA Coach can schedule practice. Runs only on LeetCode problem pages.",
    // Minimal by design (spec §4.1). Problem pages only — not all of LeetCode,
    // and certainly not <all_urls>.
    //
    // The localhost entry is the DSA Coach API itself. A host permission is what
    // lets the service worker reach it without CORS; the API deliberately
    // restricts its CORS allowlist to the web app, so without this the
    // extension could not sync at all. Change it alongside `apiBaseUrl` if the
    // API ever moves.
    host_permissions: [
      "https://leetcode.com/problems/*",
      "http://127.0.0.1:8000/*",
      "http://localhost:8000/*",
    ],
    // `storage` for the durable queue, `alarms` to retry it after the service
    // worker is evicted. Nothing else — notably not `tabs` or `scripting`.
    permissions: ["storage", "alarms"],
    action: { default_title: "DSA Coach" },
  },
});
