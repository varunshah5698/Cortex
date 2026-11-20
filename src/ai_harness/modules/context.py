"""Context assembly and compaction: what each model actually sees."""

from __future__ import annotations

from ..config.settings import MAX_FILE_LINES
from . import untrusted

CHARS_PER_TOKEN = 4


def clip(text: str, tokens: int) -> str:
    limit = tokens * CHARS_PER_TOKEN
    if len(text) <= limit:
        return text
    return text[: limit - 40] + "\n…[truncated for context budget]"


def clip_file(content: str) -> str:
    lines = content.split("\n")
    if len(lines) <= MAX_FILE_LINES:
        return content
    return "\n".join(lines[:MAX_FILE_LINES]) + f"\n…[{len(lines) - MAX_FILE_LINES} more lines]"


def peer_context(contracts: dict[str, str]) -> str:
    if not contracts:
        return "No peer contracts were stated yet."
    return "\n".join(f"- {name}: {clip(text, 120)}" for name, text in contracts.items())


def notes_context(notes: list[str]) -> str:
    """Research notes for a writer prompt — fenced, because a page wrote most of them."""
    if not notes:
        return "No research notes."
    fenced = [untrusted.fence(clip(note, 220), "research note", kind="note") for note in notes[:4]]
    return "\n\n".join(item for item in fenced if item) or "No research notes."
