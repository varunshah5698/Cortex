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

from ..config.settings import ROOT

PATH = Path(ROOT) / ".harness-memory.md"
LESSONS = Path(ROOT) / ".harness-lessons.md"
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


# A failure code is a durable rule, not a sentence about one file. These are the
# lessons a future planner/reasoner can act on; they are injected into the prompts.
LESSON_RULES = {
    "missing_id": "Declare every id in the markup before any script queries it.",
    "missing_asset": "Only link or import assets the tree actually writes.",
    "empty": "Write every file the plan declares; an empty scaffold is unfinished.",
    "invalid_json": "Every JSON file must parse before the run can be done.",
    "unbalanced_braces": "Balance braces; the file must parse.",
    "unresolved_import": "Every relative import must resolve to a file this run wrote.",
    "manifest_mismatch": "The manifest's scripts and dependencies must match the tree.",
    "missing_entry": "A Vite tree needs index.html that loads its entry module.",
    "stray_marker": "Never let a file marker or fence leak into the file body.",
    "syntax_error": "The file must parse under its own language before it ships.",
    "placeholder": "Ship real content, not a TODO/placeholder.",
    "unloaded_script": "Load every script that drives the markup.",
    "not_html": "A page needs a real html/body document.",
    "browser": "The served page must mount, load its assets, and have no console error.",
    "intent_unmet": "Build the capabilities the request names, not a page that merely runs.",
    "intent_static": "An interactive request needs real controls and a script behind them.",
    "intent_gap": "Every capability the request names must leave a trace in the tree.",
    "intent_partial": "Cover the whole request; half of it is not a smaller version of it.",
}


def learn(report: dict | None) -> list[str]:
    """Turn the failures of one run into durable rules. Idempotent, never raises."""
    issues = [item for item in (report or {}).get("issues") or [] if isinstance(item, dict)]
    if not issues:
        return []
    wanted: list[str] = []
    for issue in issues:
        rule = LESSON_RULES.get(str(issue.get("code") or ""))
        if rule and rule not in wanted:
            wanted.append(rule)
    if not wanted:
        return []
    try:
        existing = LESSONS.read_text(encoding="utf-8") if LESSONS.is_file() else "# Lessons\n"
        lines = [line for line in existing.splitlines() if line.startswith("- ")]
        for rule in wanted:
            if rule not in lines:
                lines.append(rule)
        body = "# Lessons\n\n" + "\n".join(lines[-40:]) + "\n"
        LESSONS.parent.mkdir(parents=True, exist_ok=True)
        LESSONS.write_text(body, encoding="utf-8")
        return wanted
    except OSError:
        return []


def lessons(chars: int = 1200) -> str:
    """The newest durable rules, for a planning or reasoning prompt."""
    try:
        text = LESSONS.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if not text or text == "# Lessons":
        return ""
    return text[-chars:]


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
