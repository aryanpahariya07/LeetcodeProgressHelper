/** Display helpers. Kept out of components so they can be tested directly. */

import type { CaptureConfidence, Difficulty, Resolution } from "./types";

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
  easy: "bg-emerald-100 text-emerald-800",
  medium: "bg-amber-100 text-amber-800",
  hard: "bg-rose-100 text-rose-800",
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
