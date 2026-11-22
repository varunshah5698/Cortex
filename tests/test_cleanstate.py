"""The clean-state gate: the course's end-of-session checklist, answered from disk.

The claim that matters is severity. A feature claiming `passing` with no evidence, and a
planned file nobody wrote, are false claims and must be errors. A missing handoff makes a
run less restartable but not wrong, so it is a warning. A gate that cannot tell those apart
either blocks good runs or approves bad ones.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_harness.models import WorkUnit  # noqa: E402
from ai_harness.modules import cleanstate, features, handoff, progress  # noqa: E402
from ai_harness.tools.file_manager import FileManager  # noqa: E402


class FakeRun:
    def __init__(self, workspace: Path, files: dict[str, str] | None = None) -> None:
        self.workspace = workspace / "runs" / "run-abc"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.run_id = "run-abc"
        self.prompt = "Build a dashboard"
        self.fm = FileManager(self.workspace)
        self.files = {
            path: {"agent": "vega", "content": body, "done": bool(body)}
            for path, body in (files or {"index.html": "<h1>hi</h1>\n"}).items()
        }
        self.planned_files = list(self.files)
        self.verification = {"ok": True, "errors": 0, "checked": len(self.files), "issues": []}


def _clean(run: FakeRun) -> None:
    """Satisfy every item, so a test can then break exactly one."""
    progress.write(run, objective="build it", planned=[])
    handoff.write(run)


def test_a_clean_run_passes(tmp_path: Path):
    run = FakeRun(tmp_path)
    _clean(run)

    report = cleanstate.check(run)

    assert report["ok"] is True
    assert report["errors"] == 0


def test_a_missing_verification_report_is_an_error(tmp_path: Path):
    run = FakeRun(tmp_path)
    _clean(run)
    run.verification = None

    report = cleanstate.check(run)

    assert report["ok"] is False
    assert any(item["code"] == "no_verification" for item in report["issues"])


def test_a_failed_verification_is_an_error(tmp_path: Path):
    run = FakeRun(tmp_path)
    _clean(run)
    run.verification = {"ok": False, "errors": 3, "checked": 1, "issues": []}

    report = cleanstate.check(run)

    assert report["ok"] is False
    assert any(item["code"] == "verification_failed" for item in report["issues"])


def test_a_feature_passing_without_evidence_is_an_error(tmp_path: Path):
    """The pass gate checked from the outside: a false claim is not a warning."""
    run = FakeRun(tmp_path)
    _clean(run)
    features.seed(run, [])
    # Write a passing feature directly, bypassing the gate, as a bug or a hand-edit might.
    (run.workspace / features.FILENAME).write_text(
        '{"features": [{"id": "u1", "title": "T", "status": "passing", "evidence": []}]}',
        encoding="utf-8",
    )

    report = cleanstate.check(run)

    assert report["ok"] is False
    assert any(item["code"] == "passing_without_evidence" for item in report["issues"])


def test_a_feature_passing_with_evidence_is_fine(tmp_path: Path):
    run = FakeRun(tmp_path)
    _clean(run)
    features.seed(run, [WorkUnit(id="u1", title="T", role="backend", file="index.html", objective="o", agent="a")])
    features.pass_feature(run, "u1", ["verifier: clean"])

    report = cleanstate.check(run)

    assert report["ok"] is True


def test_a_planned_file_nobody_wrote_is_an_error(tmp_path: Path):
    run = FakeRun(tmp_path)
    _clean(run)
    run.files["style.css"] = {"agent": "vega", "content": "", "done": False}
    run.planned_files = ["index.html", "style.css"]

    report = cleanstate.check(run)

    assert report["ok"] is False
    assert any(item["code"] == "empty_planned_file" for item in report["issues"])


def test_a_missing_handoff_is_only_a_warning(tmp_path: Path):
    """The run is still usable without one; it is just less restartable."""
    run = FakeRun(tmp_path)
    progress.write(run, objective="build it", planned=[])

    report = cleanstate.check(run)

    assert report["ok"] is True
    assert report["warnings"] >= 1
    assert any(item["code"] == "no_handoff" for item in report["issues"])


def test_missing_progress_is_a_warning_not_an_error(tmp_path: Path):
    """A run that wrote no checklist has no recitation, but it is not a false claim."""
    run = FakeRun(tmp_path)
    run.files = {}
    run.planned_files = []
    handoff.write(run)

    report = cleanstate.check(run)

    assert report["ok"] is True
    assert any(item["code"] == "no_progress" for item in report["issues"])


def test_the_gate_can_be_switched_off(tmp_path: Path, monkeypatch):
    run = FakeRun(tmp_path)
    run.verification = None
    monkeypatch.setattr(cleanstate, "CLEAN_STATE", False)

    report = cleanstate.check(run)

    assert report["ok"] is True
    assert report.get("skipped") is True


def test_findings_are_text_a_prompt_can_carry(tmp_path: Path):
    run = FakeRun(tmp_path)
    run.verification = None

    text = cleanstate.as_findings(cleanstate.check(run))

    assert "Clean-state check" in text
    assert "no_verification" in text


def test_findings_on_a_clean_run_say_so(tmp_path: Path):
    run = FakeRun(tmp_path)
    _clean(run)

    text = cleanstate.as_findings(cleanstate.check(run))

    assert "left nothing open" in text


def test_a_run_with_no_workspace_does_not_crash(tmp_path: Path):
    class Bare:
        files: dict = {}
        planned_files: list = []
        verification = {"ok": True, "errors": 0, "issues": []}

    report = cleanstate.check(Bare())

    assert isinstance(report, dict)
    assert "ok" in report
