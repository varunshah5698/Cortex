"""The session handoff: the next run's starting point, built from facts not prose.

The claim under test is that a handoff costs no model call and cannot contradict the run
it describes, because every line comes from the verification report, the file tree, the
feature list or the goal verdict. The other claim is that reading one back is safe: a
missing or unreadable handoff is context that is absent, never an error that stops work.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_harness.modules import handoff  # noqa: E402
from ai_harness.tools.file_manager import FileManager  # noqa: E402


class FakeRun:
    def __init__(self, workspace: Path, files: dict[str, str] | None = None) -> None:
        # Mirror the production layout (`<root>/runs/<id>`), because the handoff directory
        # is derived from the workspace's parent. A bare tmp_path as the workspace would
        # put handoffs in the shared pytest temp root and leak between tests.
        self.workspace = workspace / "runs" / "run-abc"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.run_id = "run-abc"
        self.prompt = "Build a dashboard with search"
        self.depth = "deep"
        self.fm = FileManager(self.workspace)
        self.files = {
            path: {"agent": "vega", "content": body, "done": True}
            for path, body in (files or {"index.html": "<h1>hi</h1>\n"}).items()
        }
        self.verification = {"ok": True, "errors": 0, "checked": len(self.files), "issues": []}
        self.planned_files = list(self.files)
        self.goal = {"status": "met", "by": "sensor"}
        self.previewed = False
        self.browser = None


def test_a_clean_run_says_what_proved_it(tmp_path: Path):
    run = FakeRun(tmp_path)

    text = handoff.build(run)

    assert "Verified Now" in text
    assert "verifier passed over 1 file(s)" in text
    assert "Goal gate: met" in text


def test_a_failed_run_says_so_rather_than_softening_it(tmp_path: Path):
    run = FakeRun(tmp_path)
    run.verification = {
        "ok": False,
        "errors": 2,
        "checked": 1,
        "issues": [
            {"level": "error", "code": "unresolved_import", "file": "app.js", "detail": "missing ./lib"},
            {"level": "error", "code": "empty", "file": "style.css", "detail": "no rules"},
        ],
    }

    text = handoff.build(run)

    assert "did NOT pass: 2 error(s)" in text
    assert "unresolved_import" in text
    # The next step must name a real failing file, not a generic instruction.
    assert "app.js" in text


def test_a_run_with_no_verification_report_admits_it(tmp_path: Path):
    run = FakeRun(tmp_path)
    run.verification = None

    text = handoff.build(run)

    assert "No verification report was produced" in text
    assert "unverified" in text


def test_an_empty_planned_file_is_named_as_open_work(tmp_path: Path):
    run = FakeRun(tmp_path)
    run.files["style.css"] = {"agent": "vega", "content": "", "done": False}
    run.planned_files = ["index.html", "style.css"]

    text = handoff.build(run)

    assert "Planned but empty" in text
    assert "style.css" in text


def test_the_next_step_prefers_a_real_error_over_a_feature(tmp_path: Path):
    run = FakeRun(tmp_path)
    run.verification = {
        "ok": False,
        "errors": 1,
        "checked": 1,
        "issues": [{"level": "error", "code": "syntax_error", "file": "app.js", "detail": "unexpected token"}],
    }

    text = handoff.build(run)

    assert "Fix `app.js`" in text
    assert "Passing means" in text


def test_warnings_are_counted_but_do_not_become_the_next_step(tmp_path: Path):
    run = FakeRun(tmp_path)
    run.verification = {
        "ok": True,
        "errors": 0,
        "checked": 1,
        "issues": [{"level": "warning", "code": "under_designed", "file": "a.css", "detail": "no type scale"}],
    }

    text = handoff.build(run)

    assert "1 warning(s)" in text
    assert "Fix `a.css`" not in text


def test_write_puts_the_file_beside_the_runs_not_inside_one(tmp_path: Path):
    run = FakeRun(tmp_path)

    path = handoff.write(run)

    assert path is not None
    assert path.is_file()
    assert path.parent.name == handoff.HANDOFF_DIR
    assert path.read_text(encoding="utf-8").startswith("# Session Handoff")


def test_latest_returns_the_newest_handoff_for_the_project(tmp_path: Path):
    first = FakeRun(tmp_path)
    handoff.write(first)
    second = FakeRun(tmp_path)
    second.run_id = "run-newer"
    handoff.write(second)

    text = handoff.latest(second)

    assert "run-newer" in text


def test_latest_with_no_handoffs_is_empty_rather_than_an_error(tmp_path: Path):
    run = FakeRun(tmp_path)

    assert handoff.latest(run) == ""


def test_a_corrupt_handoff_is_treated_as_absent(tmp_path: Path):
    """A handoff is context. Context that cannot be read must not stop work."""
    run = FakeRun(tmp_path)
    root = tmp_path / handoff.HANDOFF_DIR
    root.mkdir(parents=True, exist_ok=True)
    (root / "broken.md").write_text("   \n\n  ", encoding="utf-8")

    assert handoff.latest(run) == ""


def test_index_lists_handoffs_newest_first_with_metadata(tmp_path: Path):
    run = FakeRun(tmp_path)
    handoff.write(run)

    entries = handoff.index(run)

    assert entries
    assert entries[0]["run_id"] == "run-abc"
    assert entries[0]["bytes"] > 0


def test_a_run_with_no_workspace_writes_nothing_and_does_not_crash(tmp_path: Path):
    class Bare:
        run_id = "x"
        prompt = "y"

    assert handoff.write(Bare()) is None
    assert handoff.latest(Bare()) == ""
    assert handoff.index(Bare()) == []


def test_the_handoff_names_the_verification_command_a_human_would_run(tmp_path: Path):
    run = FakeRun(tmp_path)

    text = handoff.build(run)

    assert "bash scripts/verify.sh" in text
