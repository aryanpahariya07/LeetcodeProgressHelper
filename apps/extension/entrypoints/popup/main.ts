/**
 * Popup: pairing, monitoring state, and what the extension is currently doing.
 *
 * Spec §4.2 requires the popup to show the current state, the last successful
 * sync, the queued event count, and to put pause and disconnect one click away.
 *
 * Plain DOM again — the popup is six controls, and a framework runtime here
 * would be most of the bundle.
 */

import { pair } from "../../src/lib/api";
import * as store from "../../src/lib/storage";
import type { ExtensionStatus, MonitoringState } from "../../src/lib/types";

const STATE_LABELS: Record<MonitoringState, string> = {
  monitoring: "Monitoring LeetCode",
  paused: "Paused",
  disconnected: "Not connected",
  degraded: "Monitoring impaired",
};

const STATE_COLORS: Record<MonitoringState, string> = {
  monitoring: "#10b981",
  paused: "#f59e0b",
  disconnected: "#94a3b8",
  degraded: "#ef4444",
};

const root = document.getElementById("root")!;

void render();

async function render(): Promise<void> {
  const [config, status] = await Promise.all([store.getConfig(), fetchStatus()]);
  root.replaceChildren(
    header(status),
    config.deviceToken ? connectedView(status) : pairingView(),
    footer(status),
  );
}

async function fetchStatus(): Promise<ExtensionStatus> {
  try {
    return (await chrome.runtime.sendMessage({ type: "get_status" })) as ExtensionStatus;
  } catch {
    return {
      state: "disconnected",
      lastSyncAt: null,
      queuedCount: 0,
      lastError: null,
      adapterHealthy: true,
    };
  }
}

function header(status: ExtensionStatus): HTMLElement {
  const box = el("div", "header");
  const dot = el("span", "dot");
  dot.style.background = STATE_COLORS[status.state];
  const label = el("span", "state", STATE_LABELS[status.state]);
  box.append(dot, label);
  return box;
}

function pairingView(): HTMLElement {
  const box = el("div", "section");
  box.append(
    el(
      "p",
      "muted",
      "Open DSA Coach in your browser, go to Settings, and generate a pairing code.",
    ),
  );

  const input = document.createElement("input");
  input.placeholder = "ABCD-EFGH";
  input.setAttribute("aria-label", "Pairing code");
  input.autocapitalize = "characters";

  const error = el("p", "error");
  error.hidden = true;

  const button = el("button", "primary", "Connect") as HTMLButtonElement;
  button.addEventListener("click", async () => {
    button.disabled = true;
    button.textContent = "Connecting…";
    error.hidden = true;

    const config = await store.getConfig();
    const result = await pair(config.apiBaseUrl, input.value, deviceName());

    if (!result.ok || !result.token) {
      error.textContent = result.error ?? "Pairing failed.";
      error.hidden = false;
      button.disabled = false;
      button.textContent = "Connect";
      return;
    }

    await store.setConfig({
      deviceToken: result.token,
      deviceName: result.deviceName,
      // Pairing is the explicit opt-in. Monitoring starts here and nowhere else.
      state: "monitoring",
    });
    await render();
  });

  box.append(input, button, error);
  return box;
}

function connectedView(status: ExtensionStatus): HTMLElement {
  const box = el("div", "section");

  if (status.state === "degraded") {
    box.append(
      el(
        "p",
        "warning",
        "LeetCode's page layout changed, so attempts may not be captured. " +
          "Log them by hand in DSA Coach until this is fixed.",
      ),
    );
  }

  const paused = status.state === "paused";
  const toggle = el("button", "", paused ? "Resume monitoring" : "Pause monitoring");
  toggle.addEventListener("click", async () => {
    await store.setConfig({ state: paused ? "monitoring" : "paused" });
    await render();
  });

  const sync = el("button", "", "Sync now");
  sync.addEventListener("click", async () => {
    await chrome.runtime.sendMessage({ type: "sync_now" });
    await render();
  });

  const disconnect = el("button", "danger", "Disconnect this device");
  disconnect.addEventListener("click", async () => {
    // Local state only. Revoking the credential server-side is done from
    // Settings, so a stolen browser cannot quietly unpair itself.
    await store.clearAll();
    await render();
  });

  box.append(toggle, sync, disconnect);
  return box;
}

function footer(status: ExtensionStatus): HTMLElement {
  const box = el("div", "footer");
  box.append(
    el(
      "p",
      "muted",
      status.lastSyncAt
        ? `Last synced ${new Date(status.lastSyncAt).toLocaleTimeString()}`
        : "Not synced yet",
    ),
  );
  if (status.queuedCount > 0) {
    box.append(el("p", "muted", `${status.queuedCount} waiting to sync`));
  }
  if (status.lastError) {
    box.append(el("p", "error", status.lastError));
  }
  return box;
}

function deviceName(): string {
  const agent = navigator.userAgent;
  if (agent.includes("Edg/")) return "Edge";
  if (agent.includes("Chrome/")) return "Chrome";
  return "Browser";
}

function el(tag: string, className = "", text = ""): HTMLElement {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

const style = document.createElement("style");
style.textContent = `
  body { width: 260px; margin: 0; padding: 12px;
         font: 13px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif;
         color: #0f172a; background: #ffffff; }
  @media (prefers-color-scheme: dark) {
    body { background: #0f172a; color: #e2e8f0; }
    button { background: #1e293b; color: #e2e8f0; border-color: #334155; }
    input { background: #1e293b; color: #e2e8f0; border-color: #334155; }
    button.primary { background: #e2e8f0; color: #0f172a; }
  }
  .header { display: flex; align-items: center; gap: 8px; margin-bottom: 12px; }
  .dot { width: 9px; height: 9px; border-radius: 50%; }
  .state { font-weight: 600; }
  .section { display: flex; flex-direction: column; gap: 8px; }
  input, button { width: 100%; padding: 7px 10px; border-radius: 6px;
                  border: 1px solid #cbd5e1; font: inherit; background: #f8fafc;
                  color: inherit; }
  button { cursor: pointer; }
  button.primary { background: #0f172a; color: #ffffff; border-color: #0f172a; }
  button.danger { color: #b91c1c; }
  button:disabled { opacity: 0.6; cursor: default; }
  .muted { color: #64748b; margin: 0; font-size: 12px; }
  .error { color: #b91c1c; margin: 4px 0 0; font-size: 12px; }
  .warning { color: #b45309; margin: 0 0 4px; font-size: 12px; }
  .footer { margin-top: 12px; border-top: 1px solid #e2e8f0; padding-top: 8px; }
`;
document.head.append(style);
