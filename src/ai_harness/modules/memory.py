"""Memory that survives the run, because most decisions outlive it.

`RunState` holds everything a run learned and then forgets it: the next run re-derives the
same choices, sometimes differently, and the owner cannot see a project's history anywhere
but a transcript. The guide is blunt that a harness without cross-session memory makes the
same mistakes twice.

So each run *reads* a project memory file before planning and *appends* one section when it
finishes: what was asked, what shipped, the verifier's verdict, and how much it cost. The
planner gets the last few sections as context — a persistent, human-editable sentence per
run, not a vector store. The file is plain Markdown at `workspace/memory.md`, stays small
(older sections are trimmed past a bound), and a person can delete a line to forget it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ..config.settings import WORKSPACE_ROOT

PATH = Path(WORKSPACE_ROOT) / "memory.md"
MAX_CHARS = 20_000
KEEP_SECTIONS = 40
PROMPT_CHARS = 1600


def load(chars: int = PROMPT_CHARS) -> str:
    """The tail of the memory file, for a planning prompt."""
    try:
        text = PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if not text:
        return ""
    if len(text) <= chars:
        return text
    # The newest sections are at the bottom; cut on a section boundary where possible.
    tail = text[-chars:]
    cut = tail.find("\n## ")
    return tail[cut + 1 :] if cut != -1 else tail


def append(run, verdict: str) -> bool:
    """One section per finished run. Never raises: memory must not break a run."""
    files = ", ".join(sorted(run.files)) or "no files"
    cost = run.metrics().get("totals", {}).get("cost_usd", 0.0)
    section = "\n".join(
        [
            f"## {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} · `{run.run_id}`",
            "",
            f"{run.prompt.strip()[:400]}",
            "",
            f"- shipped: {files[:600]}",
            f"- verdict: {verdict}",
            f"- cost: ${cost:.4f}",
        ]
    )
    try:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        existing = PATH.read_text(encoding="utf-8") if PATH.is_file() else "# Project memory\n"
        body = existing.rstrip() + "\n\n" + section + "\n"
        body = _trim(body)
        PATH.write_text(body, encoding="utf-8")
        return True
    except OSError:
        return False


def _trim(body: str) -> str:
    """Keep the header and the newest sections, so the file stays a page, not an archive."""
    parts = body.split("\n## ")
    if len(parts) <= KEEP_SECTIONS + 1:
        return body
    head = parts[0]
    kept = parts[-KEEP_SECTIONS:]
    trimmed = head + "\n## " + "\n## ".join(kept)
    if len(trimmed) > MAX_CHARS:
        trimmed = trimmed[-MAX_CHARS:]
    return trimmed
