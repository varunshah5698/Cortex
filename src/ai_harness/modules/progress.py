"""`todo.md`: the run's checklist on disk, rewritten as work lands.

Long runs drift from the objective: a repair pass fixes a symptom, a rewrite forgets a
file the plan named, and by the end nobody — model or human — is looking at the whole
request. The guide's answer is a progress file plus recitation: keep the plan visible as
checkboxes, re-read it at the start of every step, and let what is *not* ticked steer the
next pass.

The file lives in the run's own workspace, so it survives a restart, shows up in the file
tab, and can be read back with nothing but the disk. It is written at scaffolding time and
ticked on every `file_end`; `orient()` returns the block a prompt re-injects.
"""

from __future__ import annotations

from pathlib import Path

FILENAME = "todo.md"
CHECKED = "- [x]"


def _path(run) -> Path | None:
    """The checklist's path, or None for a run object with no workspace (a focused test)."""
    workspace = getattr(run, "workspace", None)
    return Path(workspace) / FILENAME if workspace is not None else None


def write(run, objective: str = "", planned=None) -> None:
    """The initial checklist: one line per planned file, nobody ticked."""
    units = list(planned or getattr(run, "units", []) or [])
    lines = [f"# Todo — {run.prompt[:100]}", ""]
    if objective:
        lines += [objective.strip()[:400], ""]
    if units:
        for unit in units:
            lines.append(f"- [ ] {unit.file} — {unit.title} (owner: {unit.agent or 'unassigned'})")
    else:
        for path in run.files:
            lines.append(f"- [ ] {path}")
    lines += ["", "A checked line means the file was written by its owner. Unticked lines are open work."]
    _save(run, lines)
    run._todo_open = len(units) or len(run.files)  # noqa: SLF001 - cheap cache, rebuilt from disk


def _save(run, lines: list[str]) -> None:
    path = _path(run)
    if path is None:
        return
    try:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


def tick(run, path: str) -> None:
    """Mark one file done, in place. Unknown lines are added rather than dropped."""
    file = _path(run)
    if file is None:
        return
    try:
        lines = file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    found = False
    for index, line in enumerate(lines):
        if line.startswith("- [ ] ") and line.split(" — ")[0].endswith(path):
            lines[index] = line.replace("- [ ]", CHECKED, 1)
            found = True
    if not found:
        lines.insert(len(lines), f"{CHECKED} {path}")
    _save(run, lines)


def state(run) -> tuple[list[str], list[str]]:
    """`(done, open)` file names, read from disk each time so a restart cannot lie."""
    file = _path(run)
    if file is None:
        return [], list(getattr(run, "files", {}) or {})
    try:
        lines = file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return [], list(getattr(run, "files", {}) or {})
    done: list[str] = []
    open_: list[str] = []
    for line in lines:
        if not line.startswith("- ["):
            continue
        label = line[6:].split(" — ")[0].strip()
        (done if line.startswith(CHECKED) else open_).append(label)
    return done, open_


def orient(run) -> str:
    """The recitation block for a prompt: what is done, what is still open."""
    done, open_ = state(run)
    if not done and not open_:
        return ""
    total = len(done) + len(open_)
    head = f"PROGRESS ({FILENAME}): {len(done)}/{total} planned files are written."
    if not open_:
        return head + " Every planned file exists — check the request as a whole before adding anything."
    return head + " Still open: " + ", ".join(open_[:8]) + ". Do not rewrite work that is already done."
