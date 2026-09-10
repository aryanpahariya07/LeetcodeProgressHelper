"""The defect vocabulary (spec §3.6).

A closed, versioned list of the specific mistakes a solution can contain. The
coach picks from it or says `other`; it cannot invent a tag.

**Why closed.** A weakness is not a sentence, it is a count — "off_by_one_bounds
in 6 of your last 9 binary-search attempts". Free-form labels cannot be counted:
"struggled with hashing", "didn't reach for a dict" and "hash map issues" are one
observation written three ways, and nothing can add them up. Everything this
vocabulary is for depends on the tag being drawn from a fixed set.

**Why the descriptions.** They go into the prompt verbatim, so they are written
for a reader deciding whether a tag applies, not for someone browsing an enum.
Vague descriptions produce vague tagging, and vague tagging produces counts that
mean nothing.

**Why versioned.** Renaming or re-scoping a tag changes what stored conclusions
mean. `VOCABULARY_VERSION` is recorded on every conclusion so old ones stay
interpretable rather than silently re-interpreted.

**How it grows.** `other` carries free text saying what the vocabulary could not
express. What accumulates there is the backlog: frequent entries get promoted to
real tags, at which point the version increments. The list below is a first
attempt and is expected to be wrong in places — that is what `other` is for.

Pure data and pure functions. No LLM call here (invariant 2).
"""

from __future__ import annotations

VOCABULARY_VERSION = "v1"

#: The escape hatch. Requires a `detail`, since a bare `other` records only that
#: the vocabulary fell short, not how.
OTHER = "other"

#: Mistakes that make the code wrong, or wrong on some inputs.
CORRECTNESS: dict[str, str] = {
    "off_by_one_bounds": (
        "A loop or index boundary is off by one — `<` where `<=` was needed, or similar."
    ),
    "empty_input_unhandled": "Breaks or returns the wrong thing on an empty input.",
    "single_element_unhandled": (
        "Breaks on a one-element input, usually from assuming a pair exists."
    ),
    "duplicate_handling_missed": "Assumes values are distinct when the problem allows duplicates.",
    "negative_or_zero_unhandled": "Assumes positive values where zero or negatives are possible.",
    "integer_overflow": "A sum or product can exceed the type's range.",
    "base_case_wrong": "A recursion or DP base case is missing or returns the wrong value.",
    "null_check_missing": "Dereferences something that can be null or None.",
    "index_out_of_range": "Indexes past the end, or before the start, of a sequence.",
    "mutation_during_iteration": "Modifies a collection while iterating over it.",
    "index_vs_value_confusion": (
        "Returns or compares an index where a value was wanted, or the reverse."
    ),
    "wrong_comparison_operator": (
        "Uses the wrong direction of comparison — min where max was meant."
    ),
}

#: The code works, but reaches the answer the wrong way. These are the tags that
#: distinguish "solved it" from "solved it well".
STRUCTURE: dict[str, str] = {
    "nested_loop_where_hash": "Uses nested loops where a hash map gives a linear solution.",
    "linear_scan_where_binary_search": (
        "Scans a sorted structure that could be searched in log time."
    ),
    "sort_where_heap_suffices": "Sorts everything when only the top-k were needed.",
    "unnecessary_sort": "Sorts without needing the ordering.",
    "repeated_recomputation_no_memo": "Recomputes the same subproblem instead of memoising.",
    "wrong_data_structure": "Picks a structure whose operations do not fit the access pattern.",
    "recursion_depth_ignored": "Recurses deeply enough to risk a stack overflow on real input.",
    "extra_pass_avoidable": "Makes several passes where one would do.",
    "excess_space": "Allocates a copy or auxiliary structure the problem did not require.",
}

#: The pattern the problem calls for was absent, or present but applied wrongly.
#: These are the tags most directly about skill rather than carelessness.
PATTERN: dict[str, str] = {
    "pattern_absent": "Does not use the technique the problem is built around.",
    "pattern_misapplied": "Reaches for the right technique but implements it incorrectly.",
    "visited_set_missing": "A graph or grid traversal that can revisit nodes.",
    "pointer_advance_wrong": (
        "A two-pointer loop advances the wrong pointer, or both when one was right."
    ),
    "window_shrink_wrong": "A sliding window grows or shrinks on the wrong condition.",
    "partition_condition_wrong": (
        "A binary search narrows on the wrong side, or fails to terminate."
    ),
    "state_incomplete": "A DP state does not capture everything the recurrence depends on.",
    "greedy_without_justification": (
        "Takes a locally best choice where it does not give a global optimum."
    ),
}

#: Friction with the language rather than the problem. Worth separating: it is a
#: real blocker, and a completely different thing to work on.
LANGUAGE: dict[str, str] = {
    "language_api_misuse": (
        "Misuses a standard library call — wrong arguments, wrong return handling."
    ),
    "type_coercion_error": (
        "An implicit conversion produces the wrong value, such as integer division."
    ),
    "reference_vs_copy": "Aliases a structure that needed copying, or the reverse.",
    "iterator_invalidated": "Holds an iterator or reference past the point it stays valid.",
}

#: Only visible because every run was kept. These say something about how the
#: problem was approached, which the final source cannot show.
PROCESS: dict[str, str] = {
    "thrashing_no_hypothesis": "Successive runs change things without a clear direction.",
    "premature_submission": "Submitted without running, or straight after a failing run.",
    "fixed_symptom_not_cause": "Patched the failing case rather than the reason it failed.",
    "no_edge_case_testing": "Never ran a custom input despite failing on edge cases.",
    "abandoned_working_approach": "Discarded an approach that was close to correct.",
}

#: Every tag, in one place. Insertion order groups them by category, which is how
#: they are presented to the coach.
DEFECT_TAGS: dict[str, str] = {
    **CORRECTNESS,
    **STRUCTURE,
    **PATTERN,
    **LANGUAGE,
    **PROCESS,
    OTHER: (
        "Something real that no tag above describes. Always say what it was in "
        "`detail` — a bare `other` records that the vocabulary fell short "
        "without recording how, which helps nobody."
    ),
}


def is_known(tag: str) -> bool:
    """Whether a tag is in the vocabulary."""
    return tag in DEFECT_TAGS


def normalise(entries: list[dict[str, object]]) -> list[dict[str, str | None]]:
    """Coerce a coach's `defects` into the stored shape, discarding nothing real.

    An unrecognised tag becomes `other` with the invented name preserved in
    `detail`. Dropping it would lose a genuine observation; keeping it as-is
    would let the vocabulary grow by accident, and a vocabulary that grows by
    accident cannot be counted — which is the one thing it exists for.

    Entries with no usable tag are dropped: there is nothing to record.
    """
    cleaned: list[dict[str, str | None]] = []
    for entry in entries:
        raw_tag = entry.get("tag")
        if not isinstance(raw_tag, str) or not raw_tag.strip():
            continue
        tag = raw_tag.strip().lower()

        raw_detail = entry.get("detail")
        detail = raw_detail.strip() if isinstance(raw_detail, str) and raw_detail.strip() else None

        if not is_known(tag):
            detail = f"[{tag}] {detail}" if detail else f"[{tag}]"
            tag = OTHER

        cleaned.append({"tag": tag, "detail": detail})
    return cleaned


def prompt_vocabulary() -> str:
    """The vocabulary as the coach sees it, grouped so related tags read together."""
    sections = [
        ("Correctness", CORRECTNESS),
        ("Structure and efficiency", STRUCTURE),
        ("Pattern application", PATTERN),
        ("Language and API", LANGUAGE),
        ("Process — visible only across the run sequence", PROCESS),
    ]
    lines: list[str] = []
    for title, tags in sections:
        lines.append(f"\n{title}:")
        lines.extend(f"  {tag} — {description}" for tag, description in tags.items())
    lines.append(f"\n  {OTHER} — {DEFECT_TAGS[OTHER]}")
    return "\n".join(lines)
