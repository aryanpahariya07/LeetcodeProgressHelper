"""Tuning thresholds — product hypotheses, not constants.

CLAUDE.md: "Tuning thresholds live in one config module with their rationale in
comments. They are product hypotheses, not constants."

Every value here is a guess that should be revisited against real data. Each one
carries the reasoning behind it and what evidence would change it.
"""

from dsa_coach.models import Difficulty, Level

# --------------------------------------------------------------------- provisional

# Minutes budgeted per problem by difficulty.
#
# Hypothesis: a learner at the level the problem is aimed at takes roughly this
# long including reading and debugging. Used only to size the provisional plan so
# it fits the stated daily budget. Phase 1's block assembler estimates from the
# rating gap instead.
#
# Revisit when: real `active_seconds` data exists. If median actual time differs
# from these by more than ~40%, the plan is systematically over- or under-filling.
PROVISIONAL_MINUTES_BY_DIFFICULTY: dict[Difficulty, int] = {
    Difficulty.EASY: 20,
    Difficulty.MEDIUM: 35,
    Difficulty.HARD: 55,
}

# Used only for an uncatalogued problem, whose difficulty was never observed.
# The middle estimate, because with nothing known there is no reason to guess
# high or low — and it is a display figure, never an input to readiness.
PROVISIONAL_MINUTES_DEFAULT: int = 35

# How many days of practice the provisional plan materialises up front.
#
# Hypothesis: one week is enough to feel like a real plan without pretending to
# knowledge the system does not have. The plan is explicitly labelled provisional
# and is replaced once placement produces evidence (spec §9).
PROVISIONAL_PLAN_DAYS: int = 7

# Fraction of the daily budget the provisional plan will fill.
#
# Hypothesis: under-scheduling beats over-scheduling (spec §6.7). A plan you finish
# builds momentum; a plan you never finish trains you to ignore it.
PROVISIONAL_BUDGET_FILL: float = 0.85

# Ordered curriculum by self-assessed level.
#
# This is a *lookup*, not a scheduler: no readiness, no prerequisite evaluation, no
# mix ratios, no optimisation. It is used until enough evidence exists for the real
# block assembler to take over.
#
# Revisit when: placement data shows the starting point is systematically wrong for
# a given self-assessed level.
# Note: `complexity-basics` and `recursion-basics` are DAG roots with no tagged
# problems — they are prerequisites, not practice targets — so they do not appear
# here. Patterns with fewer problems than requested contribute what they have.
CURRICULUM_BY_LEVEL: dict[Level, list[str]] = {
    Level.BEGINNER: [
        "array-traversal",
        "hashmap-counting",
        "two-pointers-opposite",
        "sliding-window-fixed",
        "binary-search-sorted",
    ],
    Level.INTERMEDIATE: [
        "hashmap-counting",
        "sliding-window-variable",
        "two-pointers-same-direction",
        "binary-search-answer",
        "monotonic-stack",
        "tree-dfs-return-value",
        "bfs-shortest-path",
    ],
    Level.ADVANCED: [
        "sliding-window-variable",
        "binary-search-answer",
        "monotonic-stack",
        "tree-dfs-return-value",
        "bfs-shortest-path",
        "backtracking-pruning",
        "dp-1d-linear",
        "dp-knapsack",
    ],
}

# Problems drawn per pattern when materialising the provisional plan.
#
# Hypothesis: two problems is enough to meet a pattern without committing a large
# share of a one-week plan to it before any evidence exists.
PROVISIONAL_PROBLEMS_PER_PATTERN: int = 2

# ------------------------------------------------------------------------ evidence

# Rating deviation assigned to a manually estimated problem rating (spec §11).
#
# Hypothesis: a curator's eyeball estimate is materially less reliable than a
# contest-derived rating, and the readiness model must discount it. 350 is the
# conventional "no information" RD in Glicko-family systems; a manual estimate is
# better than nothing but not by much.
#
# Revisit when: a licensed contest-derived dataset is adopted, at which point those
# problems get a low RD and these stay high until re-sourced.
MANUAL_RATING_RD: int = 300
CONTEST_RATING_RD: int = 75

# A problem seen in the wild but absent from the catalogue, so carrying no rating
# at all. The RD is maximal because the uncertainty is total — this is not a wide
# estimate, it is the absence of one. Nothing actually reads such a problem's
# rating (it is null), so this exists to make the intent legible in the row.
UNRATED_RATING_RD: int = 350


# ============================================================ PHASE 1: MECHANISM
# Readiness, retention and scheduling. Bump ScoringConfigVersion when a change
# here invalidates stored estimates.

# ---------------------------------------------------------------- outcome scores

# Maps how an attempt was resolved to a score in [0, 1] (spec §6.2).
#
# Hypothesis: solving after a hint demonstrates most of the skill but not the
# recognition step; solving after the editorial demonstrates comprehension, not
# recall. The gap between 0.6 and 0.25 encodes that recognition is the harder and
# more transferable half.
#
# Revisit when: calibration shows hint-resolved attempts predict future unaided
# success materially better or worse than 0.6 implies.
OUTCOME_SCORES: dict[str, float] = {
    "independent": 1.0,
    "after_hint": 0.6,
    "after_editorial": 0.25,
    "failed": 0.0,
}

# A dismissed questionnaire carries no resolution, so the score falls back to the
# judge result at reduced weight (spec §6.2).
#
# Hypothesis: an accepted submission with no questionnaire was probably solved
# without the editorial, but we do not know. 0.7 sits below `independent` and
# above `after_hint`; the halved weight is what actually protects the estimate.
DISMISSED_ACCEPTED_SCORE: float = 0.7
DISMISSED_FAILED_SCORE: float = 0.1
DISMISSED_WEIGHT_MULTIPLIER: float = 0.5

# Evidence weight by capture confidence.
#
# Source is deliberately NOT applied on top of this: ingestion already caps
# confidence by source, so weighting both would penalise manual entry twice.
CONFIDENCE_WEIGHTS: dict[str, float] = {"high": 1.0, "medium": 0.7, "low": 0.4}

# Two attempts at one problem inside this window are a single sitting, not two
# independent pieces of evidence.
#
# Hypothesis: six hours separates "kept working at it" from "came back to it".
SESSION_WINDOW_HOURS: int = 6

# Beyond this many counted attempts, one problem stops moving readiness.
#
# Hypothesis: grinding a single problem says more about persistence than skill,
# and uncapped it would dominate a sparse pattern's estimate.
MAX_COUNTED_ATTEMPTS_PER_PROBLEM: int = 3

# ------------------------------------------------------- Beta baseline (PRIMARY)

# Rating-bucket boundaries for the Beta-Binomial baseline (spec §6.3).
#
# Hypothesis: three coarse bands capture most of the difficulty signal a bucketed
# model can use. Finer buckets would fragment already-sparse evidence.
BASELINE_BUCKET_BOUNDS: tuple[int, int] = (1400, 1650)

# Prior strength in pseudo-observations.
#
# Hypothesis: two pseudo-observations is weak enough that four real attempts
# dominate it, and strong enough to keep the first estimate away from 0 and 1.
BASELINE_PRIOR_STRENGTH: float = 2.0

# Prior mean by self-assessed level — where the user is assumed to start before
# any evidence exists. Deliberately pessimistic: rising reads better than falling,
# and placement corrects it quickly either way.
BASELINE_PRIOR_MEAN: dict[Level, float] = {
    Level.BEGINNER: 0.35,
    Level.INTERMEDIATE: 0.50,
    Level.ADVANCED: 0.65,
}

# Mild discount for unreliable problem ratings (spec §6.2).
#
# The Beta model uses a rating only to choose a bucket, so rating error mostly
# costs an occasional wrong bucket rather than a wrong update. Glicko-2 handles
# this properly through g(phi); this is the baseline's cruder equivalent.
BASELINE_MAX_RATING_RD_PENALTY: float = 0.3

# ------------------------------------------------------- Glicko-2 (EXPERIMENTAL)

GLICKO_START_RATING: float = 1500.0
GLICKO_START_RD: float = 350.0
GLICKO_START_VOLATILITY: float = 0.06
# System constant tau constrains volatility change. Glickman suggests 0.3-1.2;
# smaller is more conservative, which suits sparse data.
GLICKO_TAU: float = 0.5
GLICKO_CONVERGENCE_EPSILON: float = 1e-6

# Starting rating implied by self-assessed level: a prior for where to begin,
# never a claim about the user (spec §9).
GLICKO_PRIOR_RATING: dict[Level, float] = {
    Level.BEGINNER: 1200.0,
    Level.INTERMEDIATE: 1450.0,
    Level.ADVANCED: 1650.0,
}

# ------------------------------------------------------------------ display gate

# A pattern shows "Calibrating" until uncertainty falls below this.
#
# Each model normalises uncertainty to [0, 1]: posterior standard deviation for
# the Beta baseline, RD/GLICKO_START_RD for Glicko-2. 0.18 is roughly "a handful
# of consistent attempts".
#
# Revisit when: real data shows patterns either never calibrating, or calibrating
# so fast that the displayed band swings around afterwards.
CALIBRATED_UNCERTAINTY_MAX: float = 0.18

# Readiness band boundaries — bands, not percentages, until calibration is proven
# (spec §6.3). Display thresholds only; the scheduler uses the raw estimate.
READINESS_BANDS: tuple[tuple[float, str], ...] = (
    (0.35, "not_ready"),
    (0.55, "developing"),
    (0.75, "approaching"),
    (1.01, "ready"),
)

# ---------------------------------------------------------------- prerequisites

# A prerequisite is met at this readiness, provided it is also calibrated.
#
# Hypothesis: "developing" is enough to build on. Demanding mastery of every
# prerequisite would stall the plan indefinitely on sparse evidence.
PREREQUISITE_READINESS_MIN: float = 0.55
# Edges weaker than this are advisory ordering only and do not gate.
PREREQUISITE_GATING_STRENGTH_MIN: float = 0.7

# -------------------------------------------------------------------- retention

# Pinned FSRS implementation, recorded on every row. A version bump must migrate
# or recompute, never silently reinterpret stored parameters (spec §6.5).
FSRS_VERSION: str = "fsrs-6.3.2"
# Fuzzing MUST stay off: the mechanism layer is required to be reproducible.
FSRS_ENABLE_FUZZING: bool = False
FSRS_DESIRED_RETENTION: float = 0.9
# No minute-scale learning steps — a re-solve is a whole problem, not a flashcard.
FSRS_LEARNING_STEPS: tuple[()] = ()
FSRS_RELEARNING_STEPS: tuple[()] = ()

# Re-solve difficulty is derived, not asked (spec §6.5), by comparing the re-solve
# against the user's own first-exposure time on that problem.
#
# Hypothesis: needing 80% of the original time means it did not come back
# cleanly; under 40% means it did.
RESOLVE_HARD_TIME_RATIO: float = 0.8
RESOLVE_EASY_TIME_RATIO: float = 0.4
# More than one submission on a re-solve means it did not come back cleanly.
RESOLVE_CLEAN_SUBMIT_COUNT: int = 1

# ------------------------------------------------------------------ block assembly

# Default block mix (spec §6.7). Interleaving is deliberate: blocked practice
# inflates in-session performance and degrades transfer.
BLOCK_MIX_WEAKNESS: float = 0.60
BLOCK_MIX_INTERLEAVED: float = 0.25
BLOCK_MIX_RETENTION: float = 0.15

# Do not re-show a problem within this many days of the last attempt.
COOLDOWN_DAYS: int = 21

# How long an attempt stays amendable (spec §3.3).
#
# The case this exists for: you fail, dismiss the questionnaire, read the
# editorial, then solve it. The prompt fired at the wrong moment to capture the
# thing it most needed. A week is long enough to notice and correct that, and
# short enough that a correction still describes something you remember rather
# than a reconstruction.
AMENDMENT_WINDOW_DAYS: int = 7

# Minutes estimate: the problem's own difficulty, adjusted by predicted success.
#
# Two inputs because they are known at different times. A problem's rating is a
# fact from day one; readiness is not. Estimating from readiness alone gives every
# problem an identical estimate before any evidence exists, which produced blocks
# of a single item on a fresh account. Estimating from rating alone ignores that
# the same problem is quicker for someone who has got good at it.
#
# minutes = BASE * (rating / REFERENCE) ** EXPONENT * (1 + SPREAD * (0.5 - score))
#
# Revisit when: real `active_seconds` exist. If median actual time diverges from
# the estimate by more than ~40%, blocks are systematically over- or under-filled.
BASE_PROBLEM_MINUTES: int = 25
MINUTES_REFERENCE_RATING: float = 1500.0
MINUTES_RATING_EXPONENT: float = 1.5
MINUTES_SCORE_SPREAD: float = 0.6
MIN_PROBLEM_MINUTES: int = 10
MAX_PROBLEM_MINUTES: int = 75

# Practice target band as a rating offset from the user's own estimate.
#
# Hypothesis: aiming where predicted success sits around 0.6-0.7 keeps attempts
# informative — hard enough to teach something, not so hard they stall.
TARGET_RATING_OFFSET: tuple[int, int] = (-100, 250)

# Fill blocks to this fraction of the daily budget. Under-schedule (spec §6.7).
BLOCK_BUDGET_FILL: float = 0.85

# ------------------------------------------------------- material-change trigger

# Attempts between evaluations (spec §6.6). The user asked for frequent
# reconsideration; the material-change gate is what stops that becoming churn.
TRIGGER_ATTEMPT_COUNT: int = 3

# Readiness movement that counts as material.
#
# Hypothesis: 0.08 exceeds the jitter a single mixed result produces, so the plan
# does not thrash, while still catching a real trend within a week.
MATERIAL_READINESS_DELTA: float = 0.08
# Consecutive identical blockers on one pattern that are material on their own.
MATERIAL_REPEATED_BLOCKER_COUNT: int = 3


# ============================================================ PHASE 3: PLACEMENT

# Placement is not a test. It is the first practice block, chosen to be
# informative (spec §9). The user is never blocked waiting for it to finish.

# Hard ceiling on placement problems.
#
# Hypothesis: twelve real observations tell you more about someone than any
# questionnaire, and beyond that the marginal information is not worth making
# the opening week feel like an exam.
PLACEMENT_MAX_PROBLEMS: int = 12

# Floor, so a lucky start cannot end placement after two problems.
#
# Hypothesis: four attempts is the least that could show breadth across the
# foundational patterns.
PLACEMENT_MIN_PROBLEMS: int = 4

# Placement aims where the outcome is least certain: an attempt you are equally
# likely to pass or fail carries the most information about where you stand.
PLACEMENT_TARGET_SCORE: float = 0.5


# ============================================================ PHASE 4: JUDGMENT

# Floor on the interleaved share of any block, including one the coach asked for.
#
# Interleaving is not a preference to be negotiated away. Blocked practice
# inflates in-session performance and degrades transfer — it feels better and
# works worse — so a prescription that zeroes it out is clamped rather than
# honoured.
MIN_INTERLEAVED_SHARE: float = 0.15

# What the coach is allowed to change in one go.
#
# Hypothesis: a coach that can rewrite everything is not adapting, it is
# thrashing. Bounding the size keeps a single bad run from wiping a week.
MAX_PRESCRIBED_FOCUS_PATTERNS: int = 3

# Bounded retry for a failing coach run (spec §7.4).
COACH_MAX_ATTEMPTS: int = 3
COACH_BASE_DELAY_SECONDS: float = 1.0
COACH_TIMEOUT_SECONDS: float = 45.0


# Where placement starts, by self-assessed level (spec §9: "start near the rating
# implied by self-report").
#
# This is a tie-break, not a claim. At cold start every pattern carries the same
# prior, so every candidate looks equally informative and the choice would
# otherwise fall to arbitrary id order — which once picked a single 1900-rated
# problem that consumed an entire day's budget.
PLACEMENT_START_RATING: dict[Level, int] = {
    Level.BEGINNER: 1200,
    Level.INTERMEDIATE: 1450,
    Level.ADVANCED: 1650,
}
