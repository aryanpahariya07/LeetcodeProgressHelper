/**
 * API client.
 *
 * Engineering rule: business logic lives outside React components. Every network
 * call, error shape and payload transformation belongs here, not in a .tsx file.
 */

import type {
  Attempt,
  BlockResult,
  CoachRun,
  DeviceInfo,
  EventResult,
  Health,
  Plan,
  PairingCode,
  Placement,
  ReadinessReport,
  Retention,
  Today,
  TriggerBatch,
  Unlock,
  User,
} from "./types";
import type { AttemptFormValues, OnboardingFormValues } from "./schemas";

const BASE = "/api/v1";

export class ApiError extends Error {
  readonly status: number;
  readonly detail?: unknown;

  constructor(status: number, message: string, detail?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, {
      headers: { "Content-Type": "application/json" },
      ...init,
    });
  } catch (cause) {
    throw new ApiError(0, "Cannot reach the API. Is it running on port 8000?", cause);
  }

  if (!response.ok) {
    const detail = await response.json().catch(() => undefined);
    throw new ApiError(response.status, describeError(response.status, detail), detail);
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

function describeError(status: number, detail: unknown): string {
  if (typeof detail === "object" && detail !== null && "detail" in detail) {
    const value = (detail as { detail: unknown }).detail;
    if (typeof value === "string") return value;
    // FastAPI validation errors arrive as a list of field problems.
    if (Array.isArray(value)) {
      return value
        .map((item) => {
          const loc = (item as { loc?: unknown[] }).loc ?? [];
          const msg = (item as { msg?: string }).msg ?? "invalid";
          return `${loc.slice(1).join(".")}: ${msg}`;
        })
        .join("; ");
    }
  }
  return `Request failed (${status}).`;
}

/** Client-generated idempotency key (invariant 7). */
export function newEventUuid(): string {
  return crypto.randomUUID();
}

export const api = {
  health: () => request<Health>("/health"),
  me: () => request<User>("/me"),

  onboard: (values: OnboardingFormValues) =>
    request<{ user: User; plan: Plan }>("/onboarding", {
      method: "POST",
      body: JSON.stringify({
        ...values,
        target_companies: splitCompanies(values.target_companies),
        target_date: values.target_date || null,
      }),
    }),

  today: () => request<Today>("/today"),
  currentPlan: () => request<Plan>("/plan/current"),

  logAttempt: (values: AttemptFormValues) =>
    request<EventResult>("/attempts", {
      method: "POST",
      body: JSON.stringify(toAttemptEvent(values)),
    }),

  attempts: (limit = 50) => request<Attempt[]>(`/attempts?limit=${limit}`),

  readiness: () => request<ReadinessReport>("/progress/readiness"),
  placement: () => request<Placement>("/progress/placement"),
  retention: () => request<Retention>("/progress/retention"),
  unlocks: () => request<Unlock[]>("/progress/unlocks"),
  triggers: () => request<TriggerBatch[]>("/plan/triggers"),
  buildNextBlock: () => request<BlockResult>("/plan/next-block", { method: "POST" }),
  askCoach: () => request<CoachRun>("/coach/prescribe", { method: "POST" }),

  devices: () => request<DeviceInfo[]>("/devices"),
  createPairingCode: () =>
    request<PairingCode>("/devices/pairing-code", { method: "POST" }),
  revokeDevice: (id: string) =>
    request<DeviceInfo>(`/devices/${id}`, { method: "DELETE" }),
};

function splitCompanies(raw: string): string[] {
  return raw
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean)
    .slice(0, 20);
}

/**
 * Map the form to the ingestion payload.
 *
 * Note what is not sent: `source`. The server assigns it, so a manual entry can
 * never claim to be captured telemetry.
 */
export function toAttemptEvent(values: AttemptFormValues): Record<string, unknown> {
  const minutes = values.active_minutes;
  return {
    event_uuid: newEventUuid(),
    problem_slug: values.problem_slug,
    provider: "leetcode",
    submitted_at: new Date().toISOString(),
    language: values.language || null,
    active_seconds: minutes === undefined || minutes === null ? null : minutes * 60,
    submission_outcome: values.submission_outcome,
    resolution: values.resolution,
    blocker: values.blocker || null,
    confidence_cold_redo: values.confidence_cold_redo ?? null,
    is_resolve: values.is_resolve,
    // A hand-typed recollection is weaker evidence than a live capture, and is
    // recorded as such rather than being flattered (invariant 6).
    capture_confidence: "medium",
    notes: values.notes || null,
  };
}
