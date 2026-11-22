"""Context compaction: make room in a long conversation instead of overflowing it.

Every node in this harness already keeps its prompt small — the repair loop rebuilds a
flat state block from disk, the code pass clips its evidence, the planner gets the tail of
project memory. That is why the pipeline survives on a small model. It is also why the
one place that *does* grow without bound is the one place nobody looks: a long chat
(`core/chat.py`) and a long repair loop's observation history can both outrun a 200K
window, and the failure is not graceful. The provider answers 400, the run dies, and the
transcript says nothing useful.

So compaction is tiered, and the tiers run cheapest-first:

1. **Snip** — drop the middle of oversized tool results. They are the bulk of a long
   conversation and the least precious: a 40KB file read is evidence for one turn, not
   for the rest of the session.
2. **Micro** — drop whole older turns, keeping the newest `COMPACT_KEEP_TURNS` and the
   system prompt. A turn that has been superseded by three later ones is not context.
3. **Summarise** — one bounded model call over what was dropped, so the shape of the
   conversation survives even when its text does not.

The order matters: summarising first would spend a model call to compress text that
snip and micro would have removed for free. Each tier re-measures and stops as soon as
the request is under budget.

Nothing here mutates the caller's list in place: `compact()` returns a new list, so a
node can decide whether to accept the result. The dropped material is reported, never
silently discarded — a compaction that hides what it removed is a memory bug wearing a
budget's clothes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config.settings import (
    COMPACT_AT,
    COMPACT_ENABLED,
    COMPACT_KEEP_TURNS,
    COMPACT_SUMMARY_TOKENS,
    COMPACT_TOOL_CHARS,
)
from .usage import estimate_tokens

# A marker that says a message was shortened, so a model reading it knows the text is
# not the whole of what was said. Without it a clipped result reads as a complete one.
SNIPPED = "…[compacted: {dropped} characters removed]"
SUMMARY_PREFIX = "Earlier in this conversation, compacted to save room:\n"


@dataclass
class Result:
    """What compaction did, and what it cost."""

    messages: list[dict]
    before_tokens: int
    after_tokens: int
    snipped: int = 0
    dropped_turns: int = 0
    summarised: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.snipped > 0 or self.dropped_turns > 0 or self.summarised

    def public(self) -> dict:
        return {
            "before_tokens": self.before_tokens,
            "after_tokens": self.after_tokens,
            "snipped": self.snipped,
            "dropped_turns": self.dropped_turns,
            "summarised": self.summarised,
            "notes": self.notes,
        }


def _text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
    return str(content or "")


def _tokens(messages: list[dict]) -> int:
    return sum(estimate_tokens(_text(message)) for message in messages)


def over_budget(messages: list[dict], window: int, at: float = COMPACT_AT) -> bool:
    """Whether this request needs compaction before it is sent."""
    if not COMPACT_ENABLED or window <= 0:
        return False
    return _tokens(messages) > int(window * at)


def _snip(messages: list[dict], limit: int) -> tuple[list[dict], int]:
    """Tier 1: shorten oversized bodies, keeping each one's head and its tail.

    The head carries what the tool was asked and the tail carries the end of the output,
    which is where an error usually is. The middle is what a model re-reads least.

    The newest `COMPACT_KEEP_TURNS` messages are exempt. They are the turn being
    answered, and a question clipped to its first 60% is a different question — the
    protected tail is protected from every tier, not only from the one that drops turns.
    """
    protected = len(messages) - COMPACT_KEEP_TURNS
    out: list[dict] = []
    removed = 0
    for index, message in enumerate(messages):
        if index >= protected:
            out.append(message)
            continue
        content = message.get("content")
        if not isinstance(content, str) or len(content) <= limit:
            out.append(message)
            continue
        head = content[: int(limit * 0.6)]
        tail = content[-int(limit * 0.3) :]
        dropped = len(content) - len(head) - len(tail)
        removed += dropped
        out.append({**message, "content": head + "\n" + SNIPPED.format(dropped=dropped) + "\n" + tail})
    return out, removed


def _split(messages: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """`(system, older, newest)` — the system prompt and the protected tail are kept."""
    system: list[dict] = []
    body: list[dict] = []
    for message in messages:
        if message.get("role") == "system":
            system.append(message)
        else:
            body.append(message)
    if len(body) <= COMPACT_KEEP_TURNS:
        return system, [], body
    return system, body[:-COMPACT_KEEP_TURNS], body[-COMPACT_KEEP_TURNS:]


def _micro(messages: list[dict]) -> tuple[list[dict], list[dict], int]:
    """Tier 2: separate the droppable turns from the protected ones."""
    system, older, newest = _split(messages)
    return system, older, len(older)


def compact(
    messages: list[dict],
    window: int,
    at: float = COMPACT_AT,
    summarise=None,
) -> Result:
    """Compact `messages` until they fit `window`, cheapest tier first.

    `summarise` is an optional `callable(dropped_messages) -> str`. It is only invoked
    when the first two tiers were not enough, and its answer is inserted as one system
    message. When it is None or returns nothing, the dropped turns are simply gone and
    the result says so in `notes` — an honest loss, not a silent one.
    """
    before = _tokens(messages)
    result = Result(messages=list(messages), before_tokens=before, after_tokens=before)
    if not COMPACT_ENABLED or window <= 0 or before <= int(window * at):
        return result

    # Tier 1 — clip oversized bodies.
    snipped_messages, removed = _snip(list(messages), COMPACT_TOOL_CHARS)
    if removed:
        result.messages = snipped_messages
        result.snipped = removed
        result.after_tokens = _tokens(snipped_messages)
        result.notes.append(f"clipped {removed} characters from oversized results")
        if result.after_tokens <= int(window * at):
            return result

    # Tier 2 — drop the oldest turns, keeping the system prompt and the newest tail.
    system, older, dropped = _micro(result.messages)
    if dropped:
        result.messages = [*system, *result.messages[-COMPACT_KEEP_TURNS:]]
        result.dropped_turns = dropped
        result.after_tokens = _tokens(result.messages)
        result.notes.append(f"dropped {dropped} older turn(s)")
        if result.after_tokens <= int(window * at):
            return result

    # Tier 3 — summarise what was dropped, so the conversation keeps its shape.
    if summarise is not None and older:
        try:
            summary = summarise(older)
        except Exception as exc:  # noqa: BLE001 - a failed summary must not fail the call
            result.notes.append(f"summary failed: {type(exc).__name__}: {exc}")
            summary = ""
        if summary and str(summary).strip():
            body = str(summary).strip()[: COMPACT_SUMMARY_TOKENS * 4]
            result.messages = [
                *system,
                {"role": "system", "content": SUMMARY_PREFIX + body},
                *result.messages[len(system) :],
            ]
            result.summarised = True
            result.after_tokens = _tokens(result.messages)
            result.notes.append("summarised the dropped turns")
    return result


def describe(result: Result) -> str:
    """One line for the transcript: what compaction cost and what it bought."""
    if not result.changed:
        return "context fit; nothing compacted"
    saved = result.before_tokens - result.after_tokens
    return (
        f"context compacted: {result.before_tokens} → {result.after_tokens} tokens "
        f"({saved} saved; {result.snipped} chars clipped, {result.dropped_turns} turn(s) dropped"
        f"{', summarised' if result.summarised else ''})"
    )
