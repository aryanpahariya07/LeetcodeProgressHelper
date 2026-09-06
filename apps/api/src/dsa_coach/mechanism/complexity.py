"""A rough static estimate of a solution's time complexity.

This exists to recover a signal the questionnaire cannot afford to ask for. A
1-2 click prompt has no room for "what complexity did you write?", so spec §3.5
moves that question here: read it off the code instead of the user.

**It is a heuristic and it is wrong sometimes.** It counts loop nesting, notices
sorts and binary searches, and spots recursion. It does not understand the code.
So it returns a *confidence* alongside the estimate, and the caller is expected
to present it as "looks like" rather than "is" — and never to score the user on
it. Its job is to give the coach something concrete to talk about, and to flag
the case worth flagging: an accepted solution whose complexity is worse than the
problem wanted.

Pure and language-agnostic-ish: it works on indentation and keywords, which is
enough for the mainstream interview languages and honest about the rest.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class Complexity(StrEnum):
    CONSTANT = "O(1)"
    LOGARITHMIC = "O(log n)"
    LINEAR = "O(n)"
    LINEARITHMIC = "O(n log n)"
    QUADRATIC = "O(n^2)"
    CUBIC = "O(n^3)"
    EXPONENTIAL = "O(2^n)"
    UNKNOWN = "unknown"


#: Ordered cheapest to most expensive, for comparison.
ORDER: tuple[Complexity, ...] = (
    Complexity.CONSTANT,
    Complexity.LOGARITHMIC,
    Complexity.LINEAR,
    Complexity.LINEARITHMIC,
    Complexity.QUADRATIC,
    Complexity.CUBIC,
    Complexity.EXPONENTIAL,
)

_LOOP = re.compile(r"^\s*(for|while)\b|^\s*}?\s*(for|while)\s*\(")
_SORT = re.compile(r"\.sort\(|\bsorted\(|Arrays\.sort|Collections\.sort|std::sort")
_BINARY_SEARCH = re.compile(r"\bbisect|binary_search|lower_bound|upper_bound")
_HALVING = re.compile(r"(//|/)\s*2\b|>>\s*1\b|mid\s*=|\bhi\b|\blo\b")
_COMMENT = re.compile(r"^\s*(#|//|/\*|\*)")


@dataclass(frozen=True)
class ComplexityEstimate:
    time: Complexity
    confidence: str  # "low" | "medium"
    signals: tuple[str, ...]

    @property
    def display(self) -> str:
        if self.time is Complexity.UNKNOWN:
            return "could not tell"
        return f"looks like {self.time.value}"


def estimate(code: str) -> ComplexityEstimate:
    """A best guess at time complexity, with how much to trust it."""
    lines = [line for line in code.splitlines() if line.strip() and not _COMMENT.match(line)]
    if not lines:
        return ComplexityEstimate(Complexity.UNKNOWN, "low", ())

    signals: list[str] = []
    depth = _max_loop_nesting(lines)
    if depth:
        signals.append(f"{depth} level(s) of loop nesting")

    has_sort = any(_SORT.search(line) for line in lines)
    if has_sort:
        signals.append("a sort")

    has_binary_search = any(_BINARY_SEARCH.search(line) for line in lines) or (
        depth >= 1 and any(_HALVING.search(line) for line in lines)
    )
    if has_binary_search:
        signals.append("halving search")

    recursive = _recursion_depth(lines)
    if recursive > 1:
        signals.append("multiple recursive calls")

    time = _combine(depth, has_sort, has_binary_search, recursive)

    # Never claim more than "medium". This reads syntax, not meaning.
    confidence = "medium" if signals and time is not Complexity.UNKNOWN else "low"
    return ComplexityEstimate(time=time, confidence=confidence, signals=tuple(signals))


def _combine(
    depth: int, has_sort: bool, has_binary_search: bool, recursive_calls: int
) -> Complexity:
    if recursive_calls > 1 and depth == 0:
        return Complexity.EXPONENTIAL
    if depth >= 3:
        return Complexity.CUBIC
    if depth == 2:
        return Complexity.QUADRATIC
    if depth == 1:
        if has_sort:
            return Complexity.LINEARITHMIC
        return Complexity.LINEAR
    if has_sort:
        return Complexity.LINEARITHMIC
    if has_binary_search:
        return Complexity.LOGARITHMIC
    if recursive_calls == 1:
        return Complexity.LINEAR
    return Complexity.CONSTANT


def _max_loop_nesting(lines: list[str]) -> int:
    """Deepest loop nesting, by indentation.

    Indentation is a proxy for block structure. It is right for Python and
    usually right for well-formatted braces languages, which is the level of
    accuracy this whole module is claiming.
    """
    deepest = 0
    stack: list[int] = []

    for line in lines:
        indent = len(line) - len(line.lstrip())
        while stack and indent <= stack[-1]:
            stack.pop()
        if _LOOP.search(line):
            stack.append(indent)
            deepest = max(deepest, len(stack))

    return deepest


def _recursion_depth(lines: list[str]) -> int:
    """How many times a function appears to call itself."""
    definition = re.compile(r"\b(?:def|function|fn)\s+(\w+)|(\w+)\s*=\s*(?:function|lambda)")
    names = set()
    for line in lines:
        match = definition.search(line)
        if match:
            names.add(match.group(1) or match.group(2))

    calls = 0
    for name in names:
        if not name:
            continue
        pattern = re.compile(rf"\b{re.escape(name)}\s*\(")
        # One occurrence is the definition itself.
        calls = max(calls, sum(len(pattern.findall(line)) for line in lines) - 1)
    return max(0, calls)


def is_worse_than(actual: Complexity, expected: Complexity) -> bool:
    """Whether the written solution is asymptotically worse than intended.

    The one comparison worth surfacing: an accepted submission that would not
    have survived a bigger input, or an interviewer.
    """
    if Complexity.UNKNOWN in (actual, expected):
        return False
    return ORDER.index(actual) > ORDER.index(expected)
