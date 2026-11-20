"""The failure log, and the case it becomes.

The guide's Step 4 is a habit: when a run fails, write down what broke, which component
owned it, and turn the fix into a check. Habits decay, so both halves are machinery here.

* ``record`` appends one row to `failures.md` at the project root — date, run, component,
  the first fact that failed, and the files involved. It is a table a human reads, not a
  log file: one row per blocked run, append-only.
* ``promote`` appends the run's *artifacts* to `evals/regressions.jsonl`. `evals/runner`
  loads them as cases that must fail verification, so the next change to the verifier is
  tested against the exact tree that once slipped through.

Nothing here is clever about cause: a failure is classified by the check that reported it,
with a fallback to the message, so the table stays honest about what it actually knows.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ..config.settings import ROOT

LOG = Path(ROOT) / "failures.md"
REGRESSIONS = Path(ROOT) / "evals" / "regressions.jsonl"

COMPONENTS = (
    ("syntax_error", "code"),
    ("type_error", "code"),
    ("invalid_json", "code"),
    ("not_html", "code"),
    ("unbalanced_braces", "code"),
    ("unresolved_import", "wiring"),
    ("missing_id", "wiring"),
    ("unloaded_script", "wiring"),
    ("missing_asset", "wiring"),
    ("empty", "plan"),
    ("placeholder", "code"),
)

HEADER = """# Failures

One row per run that stopped short or could not be called done, appended by
`modules/failures.py`. The component column is the part of the harness that owned the
failure: `plan` (a file the plan named and nobody wrote), `wiring` (two files that do not
agree on a name), `code` (the file itself does not compile or parse), `budget`, `timeout`,
`provider`, or `unknown` when the message does not say.

The point of the table is the third question in Step 4: which component keeps failing, and
what check would have caught it last time. A promoted regression lives in
`evals/regressions.jsonl` and is replayed by the eval suite.

| date | run | component | first failure | files |
| --- | --- | --- | --- | --- |
"""


def classify(report: dict | None, message: str = "") -> str:
    """Which component owned this failure, from the check codes or the message."""
    codes = {str(item.get("code") or "") for item in (report or {}).get("issues") or []}
    for code, component in COMPONENTS:
        if code in codes:
            return component
    text = (message or "").lower()
    for word, component in (
        ("budget", "budget"),
        ("time limit", "timeout"),
        ("timed out", "timeout"),
        ("provider", "provider"),
        ("cancelled", "cancelled"),
        ("empty", "plan"),
    ):
        if word in text:
            return component
    return "unknown"


def symptom(report: dict | None, message: str = "") -> str:
    """One line, the first fact rather than the summary of facts."""
    issues = [item for item in (report or {}).get("issues") or [] if item.get("level") == "error"]
    if issues:
        item = issues[0]
        detail = str(item.get("detail") or "").replace("|", "/")
        return f"{item.get('file')}: {detail}"[:180]
    return (message or "stopped without a reported cause").replace("|", "/")[:180]


def _files(report: dict | None, limit: int = 4) -> str:
    names = sorted({str(item.get("file") or "") for item in (report or {}).get("issues") or [] if item.get("file")})
    return ", ".join(names[:limit]) or "—"


def record(run, report: dict | None = None, message: str = "") -> bool:
    """Append one row. Returns False when the log could not be written."""
    component = classify(report, message)
    row = (
        f"| {datetime.now(timezone.utc).strftime('%Y-%m-%d')} | `{getattr(run, 'run_id', '?')}` | "
        f"{component} | {symptom(report, message)} | {_files(report)} |"
    )
    try:
        if not LOG.exists() or LOG.stat().st_size == 0:
            LOG.write_text(HEADER, encoding="utf-8")
        lines = LOG.read_text(encoding="utf-8").splitlines()
        # Rows belong inside the table, not after the prose sections below it. Appending
        # to the file made the log grow a second table under "Still open".
        header = next(
            (index for index, line in enumerate(lines) if line.startswith("| date | run |")),
            None,
        )
        if header is None:
            lines.extend([""] + HEADER.splitlines() + [row])
        else:
            last = header
            while last + 1 < len(lines) and lines[last + 1].startswith("|"):
                last += 1
            lines.insert(last + 1, row)
        LOG.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return True
    except OSError:
        return False


def promote(run, report: dict | None = None) -> bool:
    """Snapshot the run's tree as an eval case that must keep failing verification."""
    files = {path: str(meta.get("content") or "") for path, meta in (getattr(run, "files", {}) or {}).items()}
    if not files:
        return False
    entry = {
        "name": f"regression-{getattr(run, 'run_id', 'unknown')}",
        "created": datetime.now(timezone.utc).isoformat(),
        "prompt": getattr(run, "prompt", ""),
        "note": f"promoted from a blocked run: {symptom(report)}",
        "files": {path: body[:8000] for path, body in files.items()},
    }
    try:
        REGRESSIONS.parent.mkdir(parents=True, exist_ok=True)
        with REGRESSIONS.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def load_regressions() -> list[dict]:
    """Every promoted case, skipping lines that are not readable objects."""
    if not REGRESSIONS.is_file():
        return []
    entries: list[dict] = []
    try:
        for line in REGRESSIONS.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict) and entry.get("files"):
                entries.append(entry)
    except OSError:
        return []
    return entries
