"""The hint ladder (spec §7.3).

Five levels, each revealing strictly more than the last:

1. Which pattern family this is.
2. The key insight, phrased as a question.
3. The invariant or recurrence.
4. The approach in outline.
5. The full approach.

Two rules the model is not trusted to keep on its own:

- **Levels cannot be skipped.** Asking for level 4 when you have seen level 1
  gets you level 2. Otherwise "give me a hint" quietly becomes "give me the
  answer", and the hint level recorded against the attempt stops meaning
  anything.
- **Levels 1-4 contain no code.** A model asked for a conceptual nudge will
  cheerfully include a snippet, and a snippet at level 2 is level 5 wearing a
  hat. The check here is a blunt one and it is meant to be: a false positive
  costs a slightly worse hint, a false negative costs the whole exercise.

Pure functions, so the guarantee is testable without a model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import IntEnum


class HintLevel(IntEnum):
    PATTERN_FAMILY = 1
    GUIDING_QUESTION = 2
    INVARIANT = 3
    APPROACH_OUTLINE = 4
    FULL_APPROACH = 5


MAX_LEVEL = HintLevel.FULL_APPROACH

LEVEL_BRIEFS: dict[HintLevel, str] = {
    HintLevel.PATTERN_FAMILY: (
        "Name the family of technique this problem belongs to, and nothing more. "
        "Do not hint at the mechanism."
    ),
    HintLevel.GUIDING_QUESTION: (
        "Ask one question whose answer is the key insight. Do not answer it."
    ),
    HintLevel.INVARIANT: (
        "State the invariant to maintain, or the recurrence to solve. Prose only."
    ),
    HintLevel.APPROACH_OUTLINE: ("Outline the approach in three or four steps. Still no code."),
    HintLevel.FULL_APPROACH: (
        "Give the full approach including complexity. A short snippet is allowed here."
    ),
}

#: Signals that prose has turned into code. Deliberately trigger-happy.
_CODE_SIGNALS = (
    re.compile(r"```"),
    re.compile(r"^\s{4,}\S", re.MULTILINE),
    re.compile(r"\bfor\s*\([^)]*;", re.IGNORECASE),
    re.compile(r"\bwhile\s*\([^)]*\)\s*[:{]"),
    re.compile(r"\bdef\s+\w+\s*\("),
    re.compile(r"\bclass\s+\w+\s*[:(]"),
    re.compile(r"\breturn\s+\w+[\[.(]"),
    re.compile(r"[a-zA-Z_]\w*\s*=\s*[a-zA-Z_0-9\[\]{}(). ]+;"),
    re.compile(r"\bfor\s+\w+(?:\s*,\s*\w+)*\s+in\s+[^\n]*:"),
    re.compile(r"\bif\s+.+:\s*$", re.MULTILINE),
    re.compile(r"[{};]\s*$", re.MULTILINE),
    re.compile(r"\+\+|--|=>|->|\[\]|\(\)"),
)


def next_level(seen: int | None, requested: int | None = None) -> HintLevel:
    """The level the user may actually have next.

    A request is a ceiling, not a jump: you get the next one up from what you
    have already seen, never more.
    """
    current = seen or 0
    step = min(current + 1, MAX_LEVEL)
    if requested is None:
        return HintLevel(step)
    return HintLevel(max(1, min(int(requested), step)))


def contains_code(text: str) -> bool:
    """Whether prose has slipped into code."""
    return any(pattern.search(text) for pattern in _CODE_SIGNALS)


@dataclass(frozen=True)
class HintCheck:
    ok: bool
    text: str
    reason: str = ""


def check_hint(level: HintLevel, text: str) -> HintCheck:
    """Validate a hint before it reaches the user.

    Below level 5 a hint containing code is refused outright rather than being
    stripped: a snippet with its lines removed is still a spoiler, and half a
    hint is worse than asking again.
    """
    stripped = text.strip()
    if not stripped:
        return HintCheck(ok=False, text="", reason="The coach returned an empty hint.")

    if level < HintLevel.FULL_APPROACH and contains_code(stripped):
        return HintCheck(
            ok=False,
            text="",
            reason=(
                f"A level {int(level)} hint must not contain code. "
                "Ask again, or step up a level deliberately."
            ),
        )

    return HintCheck(ok=True, text=stripped)


def describe(level: HintLevel) -> str:
    """What the user is about to be shown, said plainly before they see it."""
    return {
        HintLevel.PATTERN_FAMILY: "Which family of technique this is",
        HintLevel.GUIDING_QUESTION: "A question that points at the insight",
        HintLevel.INVARIANT: "The invariant or recurrence",
        HintLevel.APPROACH_OUTLINE: "The approach in outline",
        HintLevel.FULL_APPROACH: "The full approach",
    }[level]
