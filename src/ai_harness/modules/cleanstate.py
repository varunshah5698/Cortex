"""The clean-state check: a run may not end leaving the next one a mess.

Lecture 12's claim is that the next session's success depends on this session's cleanup,
and the course's answer is a checklist. A checklist a model ticks itself is a suggestion,
so this module turns the six items into questions the harness answers from disk:

* the standard verification path still runs (the report exists and says something),
* progress is recorded (`todo.md` is present and readable),
* feature state reflects reality (no feature claims `passing` without evidence),
* no planned file was left empty without being named,
* the handoff exists, so the next session has somewhere to start.

`check()` returns findings in the same shape `nodes/verify.py` uses, so the orchestrator
can merge them into the run's verification report and the existing repair loop can act on
them without a second code path. Severity is deliberate: an empty planned file and a
feature passing without evidence are **errors** (they are false claims), while a missing
handoff is a **warning** (the run is still usable, it is just less restartable).

`HARNESS_CLEAN_STATE=0` turns the gate off for an operator who wants the old behaviour.
"""

from __future__ import annotations

from ..config.settings import CLEAN_STATE
from . import features, progress


def _issue(bucket: list[dict], level: str, code: str, file: str, detail: str) -> None:
    bucket.append({"level": level, "code": code, "file": file, "detail": detail})


def check(run) -> dict:
    """The clean-state findings for `run`, in the verifier's report shape."""
    if not CLEAN_STATE:
        return {"ok": True, "errors": 0, "warnings": 0, "checked": 0, "issues": [], "skipped": True}

    issues: list[dict] = []
    files = getattr(run, "files", {}) or {}

    # 1. Verification actually ran. A run with no report cannot claim a clean state.
    report = getattr(run, "verification", None)
    if not report:
        _issue(issues, "error", "no_verification", "", "the run produced no verification report")
    elif report.get("ok") is not True:
        _issue(
            issues,
            "error",
            "verification_failed",
            "",
            f"{report.get('errors', 0)} verification error(s) remain",
        )

    # 2. Progress is recorded and readable.
    done, open_files = progress.state(run)
    if not done and not open_files:
        _issue(issues, "warning", "no_progress", progress.FILENAME, "no progress checklist was written")

    # 3. Feature state matches evidence. This is the pass gate, checked from the outside.
    summary = features.summary(run)
    for feature_id in summary.get("unproven", []):
        _issue(
            issues,
            "error",
            "passing_without_evidence",
            features.FILENAME,
            f"feature {feature_id} is marked passing with no recorded evidence",
        )

    # 4. A planned file nobody wrote is open work, and it must be named rather than hidden.
    planned = list(getattr(run, "planned_files", []) or [])
    for path in planned:
        content = str((files.get(path) or {}).get("content") or "")
        if not content.strip():
            _issue(issues, "error", "empty_planned_file", path, "the plan named this file and it was not written")

    # 5. The handoff exists, so the next session has a starting point.
    try:
        from . import handoff

        root = handoff._root(run)  # noqa: SLF001 - the same module owns the layout
        if root is not None and not (root / f"{getattr(run, 'run_id', 'run')}.md").exists():
            _issue(
                issues,
                "warning",
                "no_handoff",
                handoff.FILENAME,
                "no session handoff was written; the next run starts from project memory only",
            )
    except Exception:  # noqa: BLE001 - a missing handoff must not fail the check itself
        pass

    errors = sum(1 for item in issues if item["level"] == "error")
    warnings = sum(1 for item in issues if item["level"] == "warning")
    return {
        "ok": errors == 0,
        "errors": errors,
        "warnings": warnings,
        "checked": len(files),
        "issues": issues,
    }


def as_findings(report: dict, limit: int = 8) -> str:
    """The findings as text for a prompt, matching `nodes/verify.py::as_findings`."""
    issues = report.get("issues") or []
    if not issues:
        return "Clean state: the run left nothing open."
    lines = [f"Clean-state check: {report.get('errors', 0)} error(s), {report.get('warnings', 0)} warning(s)."]
    for item in issues[:limit]:
        where = f" [{item['file']}]" if item.get("file") else ""
        lines.append(f"- {item['level']}: {item['code']}{where} — {item['detail']}")
    if len(issues) > limit:
        lines.append(f"… and {len(issues) - limit} more.")
    return "\n".join(lines)
