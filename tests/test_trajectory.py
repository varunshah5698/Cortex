"""The trajectory: one run, one self-contained file, readable without the harness.

The format is derived from `events.jsonl`, so these tests check the projection rather
than the record: the conversation is reconstructed in order, thinking is attached to the
turn it belongs to, and a run that never wrote one reads back as None instead of raising.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_harness.modules import trajectory  # noqa: E402


class FakeRun:
    def __init__(self, workspace: Path, events: list[dict], files: dict[str, str] | None = None) -> None:
        self.workspace = workspace
        self.run_id = "traj-test"
        self.prompt = "Build a counter page"
        self.kind = "build"
        self.depth = "deep"
        self.created = "2026-11-22T00:00:00+00:00"
        self.done = True
        self.answer = "a page"
        self.events = events
        self.files = {path: {"agent": "vega", "content": body, "done": True} for path, body in (files or {}).items()}

    def metrics(self) -> dict:
        return {
            "totals": {"total_tokens": 1200, "cost_usd": 0.04},
            "budget": {"max_usd": 20.0, "usd": 0.04},
            "verification": {"ok": True, "errors": 0},
            "goal": None,
            "agents": [{"id": "vega", "name": "Vega"}],
            "prompts": {"plan": "abc123"},
        }


def test_the_conversation_is_rebuilt_in_order_with_thinking_attached():
    events = [
        {"type": "start", "prompt": "Build a counter page"},
        {"type": "think", "agent": "vega", "delta": "I should "},
        {"type": "think", "agent": "vega", "delta": "read first."},
        {"type": "message", "from": "vega", "kind": "work", "text": "Reading index.html."},
        {"type": "file", "path": "index.html", "delta": "<html>"},
        {"type": "message", "from": "lyra", "kind": "review", "text": "Looks fine."},
    ]
    data = trajectory.build(FakeRun(Path("/tmp"), events, {"index.html": "<html></html>"}), status="submitted")
    assert data["trajectory_format"] == "cortex-trajectory-1"
    assert [turn["agent"] for turn in data["messages"]] == ["vega", "lyra"]
    # Thinking lands on the turn it preceded, not as a turn of its own.
    assert data["messages"][0]["thought"] == "I should read first."
    assert "thought" not in data["messages"][1]
    assert data["info"]["exit_status"] == "submitted"
    assert data["info"]["files"]["index.html"]["lines"] == 1


def test_a_written_trajectory_round_trips_and_a_missing_one_is_none(tmp_path):
    workspace = tmp_path / "traj-test"
    workspace.mkdir()
    run = FakeRun(workspace, [{"type": "message", "from": "vega", "text": "hi"}])
    path = trajectory.write(run, status="submitted", submission="done")
    assert path is not None and path.is_file()
    loaded = trajectory.read("traj-test", root=tmp_path)
    assert loaded is not None
    assert loaded["info"]["prompt"] == "Build a counter page"
    # A run id with no file is a missing artifact, not an exception.
    assert trajectory.read("never-ran", root=tmp_path) is None


def test_summary_is_the_list_view_and_survives_an_empty_trajectory():
    run = FakeRun(Path("/tmp"), [{"type": "message", "from": "vega", "text": "hi"}])
    summary = trajectory.summary(trajectory.build(run, status="submitted"))
    assert summary["run_id"] == "traj-test"
    assert summary["total_tokens"] == 1200
    assert summary["cost_usd"] == 0.04
    assert trajectory.summary(None) == {}


def test_an_oversized_event_is_clipped_rather_than_archived():
    huge = "x" * (trajectory.MAX_EVENT_TEXT * 3)
    run = FakeRun(Path("/tmp"), [{"type": "message", "from": "vega", "text": huge}])
    data = trajectory.build(run, status="submitted")
    assert len(data["messages"][0]["content"]) < len(huge)
    assert "truncated in trajectory" in data["messages"][0]["content"]
    # The file is still valid JSON after clipping.
    assert json.loads(json.dumps(data))["info"]["run_id"] == "traj-test"


def test_a_write_failure_is_not_a_run_failure(tmp_path):
    # The trajectory is derived: a workspace that cannot be written is a missing artifact,
    # never a reason for a finished run to report a failure it did not have.
    class NoWorkspace:
        workspace = None
        run_id = "x"
        events: list = []

    assert trajectory.write(NoWorkspace()) is None
