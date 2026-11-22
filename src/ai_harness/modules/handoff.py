"""`session-handoff.md`: the next run's starting point, written by this one.

`docs/harness.md` lists this as a known gap: "a new run starts from project memory, not
from the last run's open work." Project memory is a list of decisions; it is not a
statement of what is half-finished, what was verified, and what the next step is. So a
second run on the same project re-derives all of that from scratch, or worse, guesses.

The course's answer (Lectures 05 and 12) is a handoff artefact with four sections, each
answering a question the next session will actually ask:

* **Verified now** — what works, and what check proved it. Not "the app is fine".
* **Changed this session** — what moved, so the next session does not redo it.
* **Broken or unverified** — the honest list. This is the section a model is most tempted
  to leave empty, so it is built from the verifier's open issues rather than from prose.
* **Next best step** — one step, with the passing condition named.

Everything here is derived from facts already on disk (the verification report, the file
tree, the feature list, the metrics ledger), so writing a handoff costs no model call and
cannot contradict the run it describes. `read()` returns the newest handoff for a project
so a new run can start from it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

FILENAME = "session-handoff.md"
# Handoffs live beside the runs, not inside one: they are the seam between runs.
HANDOFF_DIR = "handoffs"


def _root(run) -> Path | None:
    """Where handoffs for this run's project live, or None for a focused test.

    A run's workspace is always `<runs_root>/<id>`, so the handoff directory is its
    sibling inside the runs root. Anchoring on the workspace's immediate parent — rather
    than on a module-level constant — keeps the answer correct when the runs root is
    relocated (a test, a second workspace) instead of writing into the real project tree.
    """
    workspace = getattr(run, "workspace", None)
    if workspace is None:
        return None
    return Path(workspace).parent / HANDOFF_DIR


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build(run) -> str:
    """The handoff body, from the run's own facts. No model call, no invention."""
    report = getattr(run, "verification", None) or {}
    issues = [item for item in (report.get("issues") or []) if item.get("level") == "error"]
    warnings = [item for item in (report.get("issues") or []) if item.get("level") == "warning"]
    files = getattr(run, "files", {}) or {}
    written = sorted(path for path, meta in files.items() if str(meta.get("content") or "").strip())
    empty = sorted(path for path, meta in files.items() if not str(meta.get("content") or "").strip())

    goal = getattr(run, "goal", None) or {}
    features: dict = {}
    try:
        from . import features as feature_list

        features = feature_list.summary(run)
    except Exception:  # noqa: BLE001 - a handoff must never fail the run it describes
        features = {}

    lines = [
        "# Session Handoff",
        "",
        f"- Run: `{getattr(run, 'run_id', 'unknown')}`",
        f"- Written: {_now()}",
        f"- Request: {str(getattr(run, 'prompt', '') or '')[:300]}",
        f"- Depth: {getattr(run, 'depth', 'unknown')}",
        "",
        "## Verified Now",
        "",
    ]
    if report.get("ok") is True:
        lines.append(f"- The deterministic verifier passed over {report.get('checked', len(files))} file(s).")
    elif report:
        lines.append(
            f"- The verifier did NOT pass: {report.get('errors', len(issues))} error(s) over "
            f"{report.get('checked', len(files))} file(s)."
        )
    else:
        lines.append("- No verification report was produced. Treat every claim below as unverified.")
    if getattr(run, "previewed", False):
        lines.append("- A preview was served and answered.")
    if getattr(run, "browser", None):
        browser = run.browser or {}
        lines.append(f"- Browser check: {'clean' if browser.get('ok') else 'found issues'}.")
    if goal:
        lines.append(f"- Goal gate: {goal.get('status', 'unknown')} (by {goal.get('by', 'none')}).")

    lines += ["", "## Changed This Session", ""]
    if written:
        for path in written[:40]:
            lines.append(f"- `{path}` — written")
        if len(written) > 40:
            lines.append(f"- … and {len(written) - 40} more file(s)")
    else:
        lines.append("- No file was written.")

    lines += ["", "## Broken Or Unverified", ""]
    if issues:
        for item in issues[:20]:
            lines.append(f"- `{item.get('file')}` · {item.get('code')}: {item.get('detail')}")
    if empty:
        lines.append(f"- Planned but empty: {', '.join(empty[:20])}")
    if warnings:
        lines.append(f"- {len(warnings)} warning(s) were recorded and are not blocking.")
    if not issues and not empty and not warnings:
        lines.append("- Nothing known to be broken. That is a claim about the checks that ran, not proof.")

    lines += ["", "## Next Best Step", ""]
    if issues:
        first = issues[0]
        lines.append(f"- Fix `{first.get('file')}`: {first.get('detail')}")
        lines.append(f"- Passing means: the verifier reports no error for `{first.get('file')}`.")
    elif features.get("active"):
        lines.append(f"- Finish feature `{features['active']}` and record its evidence.")
        lines.append("- Passing means: the feature reaches `passing` through the evidence gate.")
    elif empty:
        lines.append(f"- Write `{empty[0]}`, which the plan named and nobody produced.")
        lines.append("- Passing means: the file exists and the verifier accepts it.")
    else:
        lines.append("- Nothing is open. Start from the request again before adding scope.")

    lines += [
        "",
        "## Commands",
        "",
        "- Verification: `bash scripts/verify.sh`",
        "- Resume this run: `POST /api/run/{id}/resume`",
        "",
    ]
    return "\n".join(lines)


def write(run) -> Path | None:
    """Write the handoff for `run`. Returns its path, or None with no workspace."""
    root = _root(run)
    if root is None:
        return None
    try:
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{getattr(run, 'run_id', 'run')}.md"
        path.write_text(build(run), encoding="utf-8")
        return path
    except OSError:
        return None


def latest(run) -> str:
    """The newest handoff for this project, or "" when there is none.

    Read by a new run so it starts from the last run's open work instead of from project
    memory alone. A malformed or unreadable file is treated as absent: a handoff is
    context, and context that cannot be read must not stop work.
    """
    root = _root(run)
    if root is None or not root.exists():
        return ""
    try:
        candidates = sorted(root.glob("*.md"), key=lambda item: item.stat().st_mtime, reverse=True)
    except OSError:
        return ""
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            return text[:4000]
    return ""


def index(run) -> list[dict]:
    """Every handoff for this project as data, newest first, for the API and the UI."""
    root = _root(run)
    if root is None or not root.exists():
        return []
    try:
        paths = sorted(root.glob("*.md"), key=lambda item: item.stat().st_mtime, reverse=True)
    except OSError:
        return []
    out: list[dict] = []
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        out.append(
            {
                "run_id": path.stem,
                "path": str(path),
                "bytes": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(timespec="seconds"),
            }
        )
    return out


def as_json(run) -> str:
    """The handoff's facts as JSON, for a caller that wants structure over prose."""
    return json.dumps(
        {
            "run_id": getattr(run, "run_id", ""),
            "prompt": str(getattr(run, "prompt", "") or "")[:300],
            "verification": getattr(run, "verification", None) or {},
            "goal": getattr(run, "goal", None) or {},
            "files": sorted((getattr(run, "files", {}) or {}).keys()),
        },
        indent=2,
    )
