/**
 * API client.
 *
 * The only place the extension talks to the server. Two things it deliberately
 * does not do: it never sends a `source` (the server assigns that, so a client
 * cannot claim its data was captured telemetry), and it never sends a user id
 * (identity comes from the device token — invariant 10).
 */

import type { AttemptEvent } from "./types";

export interface BatchOutcome {
  ok: boolean;
  status: number;
  accepted: number;
  duplicates: number;
  invalid: number;
  /** Events the server will never accept, so they must leave the queue. */
  rejectedUuids: string[];
  error: string | null;
}

interface EventResult {
  event_uuid: string;
  status: "accepted" | "duplicate" | "invalid";
  error: string | null;
}

export interface PairOutcome {
  ok: boolean;
  token: string | null;
  deviceName: string | null;
  error: string | null;
}

export async function pair(
  baseUrl: string,
  code: string,
  deviceName: string,
): Promise<PairOutcome> {
  try {
    const response = await fetch(`${baseUrl}/extension/pair`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code, device_name: deviceName }),
    });
    const body = await response.json().catch(() => ({}));

    if (!response.ok) {
      return {
        ok: false,
        token: null,
        deviceName: null,
        error: typeof body.detail === "string" ? body.detail : "Pairing failed.",
      };
    }
    return { ok: true, token: body.token, deviceName: body.device_name, error: null };
  } catch {
    return {
      ok: false,
      token: null,
      deviceName: null,
      error: "Could not reach DSA Coach. Is the API running?",
    };
  }
}

export async function sendBatch(
  baseUrl: string,
  token: string,
  events: AttemptEvent[],
): Promise<BatchOutcome> {
  try {
    const response = await fetch(`${baseUrl}/extension/events/batch`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({ events }),
    });

    if (!response.ok) {
      const detail = await response.text().catch(() => "");
      return {
        ok: false,
        status: response.status,
        accepted: 0,
        duplicates: 0,
        invalid: 0,
        rejectedUuids: [],
        error: detail.slice(0, 300) || `HTTP ${response.status}`,
      };
    }

    const body = await response.json();
    const results: EventResult[] = body.results ?? [];

    return {
      ok: true,
      status: response.status,
      accepted: body.accepted ?? 0,
      duplicates: body.duplicates ?? 0,
      invalid: body.invalid ?? 0,
      // Accepted, duplicate and invalid all mean "settled" — an invalid event
      // will never become valid, so retrying it forever would wedge the queue.
      rejectedUuids: results.map((r) => r.event_uuid),
      error: null,
    };
  } catch {
    // Status 0 means no response reached us at all: offline, or the API is down.
    return {
      ok: false,
      status: 0,
      accepted: 0,
      duplicates: 0,
      invalid: 0,
      rejectedUuids: [],
      error: "Offline — events are queued and will sync later.",
    };
  }
}

export interface ExtensionConfig {
  code_capture_enabled: boolean;
  max_batch_size: number;
}

/**
 * Ask the server what the extension is currently permitted to do.
 *
 * Only `code_capture_enabled` matters today, and it is deliberately the
 * server's answer rather than a local setting (invariant 9): consent is a
 * versioned decision recorded against disclosure text, and revoking it in the
 * dashboard has to stop capture at the source.
 *
 * Any failure — offline, 401, malformed — answers "not permitted". Sending code
 * because permission could not be *checked* is the one outcome worth ruling out
 * by construction.
 */
export async function fetchConfig(
  baseUrl: string,
  token: string,
): Promise<ExtensionConfig | null> {
  try {
    const response = await fetch(`${baseUrl}/extension/config`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!response.ok) return null;
    return (await response.json()) as ExtensionConfig;
  } catch {
    return null;
  }
}

export interface SnapshotPayload {
  snapshot_uuid: string;
  problem_slug: string;
  kind: "run" | "submit";
  language: string | null;
  code: string;
  captured_at: string;
}

/**
 * Send one Run/Submit snapshot (spec §3.6).
 *
 * Best-effort by design. A lost snapshot costs one entry in a sequence, and is
 * not worth the durable queue that attempts get: attempts are the evidence,
 * snapshots are the colour around them. Retrying would also mean holding source
 * code on disk for longer than the moment it is in flight.
 *
 * The server refuses these outright without code-capture consent, so a `stored:
 * 0` reply is a normal outcome rather than a failure.
 */
export async function sendSnapshot(
  baseUrl: string,
  token: string,
  snapshot: SnapshotPayload,
): Promise<boolean> {
  try {
    const response = await fetch(`${baseUrl}/extension/snapshots`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({ snapshots: [snapshot] }),
    });
    return response.ok;
  } catch {
    return false;
  }
}
