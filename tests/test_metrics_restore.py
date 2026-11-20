"""A restored run keeps the ledger it earned.

Resuming used to replace the usage map with the calls made after the resume, so a build
that spent 78k tokens displayed as a 1k chat. The event log is the source of truth.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import ai_harness.core.state as state  # noqa: E402


def test_restoring_a_run_rebuilds_usage_agents_and_files(monkeypatch, tmp_path):
    monkeypatch.setattr(state, "RUNS_ROOT", tmp_path)
    run_dir = tmp_path / "r1"
    (run_dir / "src").mkdir(parents=True)
    (run_dir / "src" / "App.tsx").write_text("export default function App() { return <p>hi</p>; }\n")
    events = [
        {"type": "start", "prompt": "build a portfolio", "created": "2026-11-20T00:00:00Z"},
        {
            "type": "usage",
            "agent": "writer",
            "name": "Writer",
            "role": "frontend",
            "model": "gemma4:e4b",
            "prompt_tokens": 1000,
            "completion_tokens": 500,
            "ms": 2000,
            "calls": 2,
        },
        {
            "type": "usage",
            "agent": "planner",
            "name": "Scout",
            "role": "planner",
            "model": "gemma4:e4b",
            "prompt_tokens": 200,
            "completion_tokens": 100,
            "ms": 1000,
            "calls": 1,
        },
        {"type": "done", "files": []},
    ]
    (run_dir / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")

    restored = state.restore_run("r1")
    assert restored is not None
    metrics = restored.metrics()
    assert metrics["totals"]["total_tokens"] == 1800
    assert {agent["id"] for agent in metrics["agents"]} == {"writer", "planner"}
    assert metrics["files"] == 1
