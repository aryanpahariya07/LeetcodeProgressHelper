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
