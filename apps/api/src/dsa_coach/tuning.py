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
# it fits the stated daily budget. Phase 1 replaces this with an estimate derived
# from the rating gap between problem and user.
#
# Revisit when: real `active_seconds` data exists. If median actual time differs
# from these by more than ~40%, the plan is systematically over- or under-filling.
PROVISIONAL_MINUTES_BY_DIFFICULTY: dict[Difficulty, int] = {
    Difficulty.EASY: 20,
    Difficulty.MEDIUM: 35,
    Difficulty.HARD: 55,
}

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
# mix ratios, no optimisation. Phase 1 introduces the real block assembler and this
# is used only until placement completes.
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
