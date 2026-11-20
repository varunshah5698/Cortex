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


def files_context(files: dict[str, dict], exclude: str = "", max_files: int = 4, tokens: int = 320) -> str:
    """What the peers actually wrote, not only what they promised.

    A specialist that sees the real markup and selectors keeps them; one that sees only
    a contract invents a second copy. Bodies are clipped, so this is context, not a dump.
    """
    chosen: list[str] = []
    for path in sorted(files):
        if path == exclude or path in ("todo.md", "RECEIPT.md"):
            continue
        content = str((files.get(path) or {}).get("content") or "").strip()
        if not content:
            continue
        chosen.append(f"--- {path} ---\n{clip(content, tokens)}")
        if len(chosen) >= max_files:
            break
    return "\n\n".join(chosen) or "No peer file has been written yet."


def notes_context(notes: list[str]) -> str:
    """Research notes for a writer prompt — fenced, because a page wrote most of them."""
    if not notes:
        return "No research notes."
    fenced = [untrusted.fence(clip(note, 220), "research note", kind="note") for note in notes[:4]]
    return "\n\n".join(item for item in fenced if item) or "No research notes."
