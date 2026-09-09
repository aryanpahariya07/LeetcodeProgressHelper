/** Display helpers. Kept out of components so they can be tested directly. */

import type { CaptureConfidence, Difficulty, ReadinessBand, Resolution } from "./types";

export const RESOLUTION_LABELS: Record<Resolution, string> = {
  independent: "Solved independently",
  after_hint: "Solved after a hint",
  after_editorial: "Solved after the editorial",
  failed: "Did not solve",
  unknown: "Not recorded",
};

export const BLOCKER_LABELS: Record<string, string> = {
  pattern_not_recognized: "Didn't recognise the pattern",
  pattern_known_impl_failed: "Knew the pattern, implementation failed",
  edge_cases: "Edge cases",
  complexity: "Too slow / wrong complexity",
  data_structure_choice: "Wrong data structure",
  language_api: "Language or API friction",
  misread_problem: "Misread the problem",
};

export const DIFFICULTY_CLASSES: Record<Difficulty, string> = {
  easy: "bg-success-bg text-success-ink",
  medium: "bg-warn-bg text-warn-ink",
  hard: "bg-danger-bg text-danger-ink",
};

export function formatMinutes(total: number): string {
  if (total < 60) return `${total} min`;
  const hours = Math.floor(total / 60);
  const minutes = total % 60;
  return minutes ? `${hours}h ${minutes}m` : `${hours}h`;
}

/**
 * Ratings are curator estimates with a high deviation (spec §11), so show a
 * band rather than a number that implies precision the data does not have.
 */
export function ratingBand(rating: number, rd: number): string {
  if (rd >= 200) return "approx.";
  return String(rating);
}

export function confidenceNote(confidence: CaptureConfidence): string | null {
  if (confidence === "high") return null;
  return confidence === "medium"
    ? "Recorded by hand — weaker evidence than a live capture."
    : "Low-confidence capture.";
}

export function formatDateTime(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

/**
 * Readiness is shown as a band, never a percentage, until calibration is proven
 * (invariant 12). "Calibrating" is an honest answer, not a placeholder.
 */
export const BAND_LABELS: Record<ReadinessBand, string> = {
  calibrating: "Calibrating",
  not_ready: "Not ready",
  developing: "Developing",
  approaching: "Approaching",
  ready: "Ready",
};

export const BAND_STYLES: Record<ReadinessBand, string> = {
  calibrating: "bg-inset text-ink-faint",
  not_ready: "bg-danger-bg text-danger-ink",
  developing: "bg-warn-bg text-warn-ink",
  approaching: "bg-info-bg text-info-ink",
  ready: "bg-success-bg text-success-ink",
};

export const ROLE_LABELS: Record<string, string> = {
  weakness: "Target weakness",
  interleaved: "Mixed in",
  retention: "Review",
};
