"""Deterministic checks on what a model hands back as its *answer*.

Prompt wording is a request, not a guarantee. A model can obey "do not restate your
reasoning" and still append a planning reflection or a reasoning summary to the end of
the answer. This module cuts such a trailing section off, using structure rather than
wording: a heading line the model uses for its own self-talk, at the end of the text.

Two entry points:

* :func:`strip_reflection` — one shot, returns the clean text and what was removed.
* :class:`ReflectionGuard` — the same rule applied to a token stream, holding back a
  bounded tail so a heading split across deltas is still caught before it is emitted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Headings a model uses for its own planning/self-talk. Deliberately qualified names:
# a bare "Assumptions" or "Notes" section can be legitimate answer content, so those do
# not match on their own.
_REFLECTION_HEADINGS = (
    r"planning reflection\b",
    r"reasoning summary\b",
    r"reasoning (?:notes?|trace)\b",
    r"decision rationale\b",
    r"self[- ]check\b",
    r"self[- ]critique\b",
    r"planning notes?\b",
    r"(?:my|internal|private) reasoning\b",
    r"assumptions (?:made|i made)\b",
    r"uncertainties(?: ?[/&] ?| and )trade-?offs\b",
    r"why i answered (?:it )?this way\b",
    r"reflection on (?:my|this|the) answer\b",
    r"notes? to self\b",
    r"how i approached (?:it|this)\b",
)

_HEADING = re.compile(r"^(?:" + "|".join(_REFLECTION_HEADINGS) + r")", re.IGNORECASE)
_SEPARATOR = re.compile(r"^\s{0,3}(?:(?:[-*_]\s*){3,}|={3,})\s*$")


@dataclass(frozen=True)
class CleanedAnswer:
    """The answer that should be stored and returned."""

    text: str
    stripped: bool = False
    heading: str | None = None
    removed: str = ""


@dataclass(frozen=True)
class Cut:
    index: int
    heading: str
    removed: str


def _payload(line: str) -> str:
    """The words of a line with markdown decoration and trailing punctuation removed."""
    text = line.strip()
    text = re.sub(r"^#{1,6}\s*", "", text)
    text = text.strip("*_`> ")
    text = text.rstrip("*_` ")
    return text.rstrip(":").strip()


def _heading_of(line: str) -> str | None:
    payload = _payload(line)
    if not payload:
        return None
    return payload if _HEADING.match(payload) else None


def find_reflection(text: str, final: bool = True) -> Cut | None:
    """Locate a trailing self-talk section, if any.

    `final=False` ignores an unterminated last line: mid-stream it is not yet a
    heading, and matching its prefix would report a truncated name.
    """
    lines = text.splitlines(keepends=True)
    fences = 0
    offset = 0
    for index, line in enumerate(lines):
        if not final and index == len(lines) - 1 and not line.endswith(("\n", "\r")):
            break
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            fences += 1
            offset += len(line)
            continue
        heading = _heading_of(line) if fences % 2 == 0 else None
        if heading and any(_payload(earlier) for earlier in lines[:index]):
            trim = offset
            # the model usually separates the answer from its reflection with a rule
            back = index - 1
            while back >= 0 and (not lines[back].strip() or _SEPARATOR.match(lines[back])):
                trim -= len(lines[back])
                back -= 1
            # end on the last content character, not the run of whitespace before the cut
            while trim > 0 and text[trim - 1].isspace():
                trim -= 1
            if trim > 0:
                return Cut(trim, heading, text[trim:])
        offset += len(line)
    return None


def strip_reflection(text: str) -> CleanedAnswer:
    """The answer with any trailing planning/reflection section removed."""
    cut = find_reflection(text)
    if cut is None:
        return CleanedAnswer(text.strip())
    return CleanedAnswer(text[: cut.index].strip(), True, cut.heading, cut.removed)


class ReflectionGuard:
    """Applies :func:`strip_reflection` to a token stream.

    A bounded tail is held back until the next delta, so a heading that arrives in
    pieces is recognised before any of it reaches the client.
    """

    def __init__(self, hold: int = 96) -> None:
        self._hold = max(1, hold)
        self._text = ""
        self._emitted = 0
        self._cut: Cut | None = None

    @property
    def heading(self) -> str | None:
        return self._cut.heading if self._cut else None

    @property
    def stripped(self) -> bool:
        return self._cut is not None

    def feed(self, delta: str) -> str:
        """Safe text to emit for this delta (may be empty, may lag by a few tokens)."""
        if not delta:
            return ""
        self._text += delta
        if self._cut is None:
            self._cut = find_reflection(self._text, final=False)
        limit = self._cut.index if self._cut else len(self._text) - self._hold
        return self._take(limit)

    def finish(self) -> str:
        """The rest of the safe text, once the model has stopped."""
        if self._cut is None:
            self._cut = find_reflection(self._text, final=True)
        limit = self._cut.index if self._cut else len(self._text)
        return self._take(limit)

    def _take(self, limit: int) -> str:
        if limit <= self._emitted:
            return ""
        chunk = self._text[self._emitted : limit]
        self._emitted = limit
        return chunk
