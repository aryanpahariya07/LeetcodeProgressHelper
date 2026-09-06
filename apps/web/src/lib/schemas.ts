/** Form validation. Mirrors the server's constraints so errors surface early. */

import { z } from "zod";

export const onboardingSchema = z.object({
  display_name: z.string().min(1, "Required").max(120),
  timezone: z.string().min(1).default("UTC"),
  preferred_language: z.string().min(1).default("python"),
  target_companies: z.string().max(500).default(""),
  target_date: z.string().default(""),
  days_per_week: z.coerce
    .number()
    .int()
    .min(1, "At least 1 day")
    .max(7, "At most 7 days"),
  minutes_per_day: z.coerce
    .number()
    .int()
    .min(10, "At least 10 minutes")
    .max(600, "At most 600 minutes"),
  self_assessed_level: z.enum(["beginner", "intermediate", "advanced"]),
  approx_problems_solved: z.coerce.number().int().min(0).max(10_000),
});

export type OnboardingFormValues = z.infer<typeof onboardingSchema>;

export const attemptSchema = z
  .object({
    problem_slug: z
      .string()
      .min(1, "Required")
      .max(200)
      .transform((v) => normalizeSlug(v)),
    resolution: z.enum([
      "independent",
      "after_hint",
      "after_editorial",
      "failed",
      "unknown",
    ]),
    blocker: z
      .enum([
        "",
        "pattern_not_recognized",
        "pattern_known_impl_failed",
        "edge_cases",
        "complexity",
        "data_structure_choice",
        "language_api",
        "misread_problem",
      ])
      .default(""),
    submission_outcome: z.enum([
      "accepted",
      "wrong_answer",
      "runtime_error",
      "compile_error",
      "tle",
      "unknown",
    ]),
    language: z.string().max(32).default(""),
    active_minutes: z.coerce.number().int().min(0).max(1440).optional(),
    confidence_cold_redo: z.coerce.number().int().min(1).max(5).optional(),
    is_resolve: z.boolean().default(false),
    notes: z.string().max(4000).default(""),
  })
  .refine((v) => v.resolution !== "independent" || !v.blocker, {
    message: "An independently solved attempt has no blocker.",
    path: ["blocker"],
  });

export type AttemptFormValues = z.infer<typeof attemptSchema>;

/**
 * Accept either a slug or a pasted LeetCode URL.
 *
 * The whole point of the extension (Phase 2) is that this never has to be typed.
 * Until then, make typing it as forgiving as possible.
 */
export function normalizeSlug(input: string): string {
  const trimmed = input.trim();
  const match = trimmed.match(/leetcode\.com\/problems\/([^/?#]+)/i);
  if (match) return match[1].toLowerCase();
  return trimmed.replace(/^\/+|\/+$/g, "").toLowerCase();
}
