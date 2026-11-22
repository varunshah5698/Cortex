"""Context assembly and compaction: what each model actually sees."""

from __future__ import annotations

from ..config.settings import MAX_FILE_LINES, PEER_FILES, PEER_TOKENS
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


def files_context(files: dict[str, dict], exclude: str = "", max_files: int = 0, tokens: int = 0) -> str:
    """What the peers actually wrote, not only what they promised.

    A specialist that sees the real markup and selectors keeps them; one that sees only
    a contract invents a second copy. Bodies are clipped, so this is context, not a dump.

    The most recently written files come first. Taking the alphabetically first four was
    how an author late in the plan saw ``About.tsx`` and ``App.tsx`` but not the component
    written immediately before it — every file was written against the wrong neighbours.
    The budget is a setting (``PEER_FILES``/``PEER_TOKENS``); ``HARNESS_PEER_FILES=0``
    includes every peer file, which is the default because a tidy prompt that omits the
    file being imported is the failure this function exists to prevent.
    """
    limit_files = max_files or PEER_FILES
    limit_tokens = tokens or PEER_TOKENS
    written = [
        path
        for path in files
        if path != exclude
        and path not in ("todo.md", "RECEIPT.md")
        and str((files.get(path) or {}).get("content") or "").strip()
    ]
    recent = written[-limit_files:] if limit_files > 0 else written
    chosen = sorted(recent)
    sections = [f"--- {path} ---\n{clip(str(files[path]['content']).strip(), limit_tokens)}" for path in chosen]
    return "\n\n".join(sections) or "No peer file has been written yet."


def notes_context(notes: list[str]) -> str:
    """Research notes for a writer prompt — fenced, because a page wrote most of them."""
    if not notes:
        return "No research notes."
    fenced = [untrusted.fence(clip(note, 220), "research note", kind="note") for note in notes[:4]]
    return "\n\n".join(item for item in fenced if item) or "No research notes."
