"""The run-level gate: a run may not be reported as done on a failing check.

The review node already revises once. This guards the layer above it — the orchestrator
must spend its bounded repair round, and if the deterministic checks still fail it has to
end the run with a terminal `error`, never a `done` that reads as success.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import ai_harness.core.state as state_mod  # noqa: E402
from ai_harness.core import orchestrator as orch  # noqa: E402
from ai_harness.core.state import RunState  # noqa: E402
from ai_harness.models import Specialist, WorkUnit  # noqa: E402

FAILING_REPORT = {
    "ok": False,
    "errors": 1,
    "warnings": 0,
    "checked": 1,
    "issues": [
        {
            "file": "index.html",
            "level": "error",
            "code": "missing_asset",
            "detail": "links ./app.js but no such file was written",
        }
    ],
}


def test_run_refuses_done_when_verification_still_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original_root = state_mod.RUNS_ROOT
    state_mod.RUNS_ROOT = tmp_path
    repair_calls: list[int] = []

    async def fake_plan(run: RunState, agent: Specialist):
        units = [
            WorkUnit(id="u1", title="Markup", role="frontend", file="index.html", objective="markup", agent=agent.id)
        ]
        return units, ["index.html"], []

    async def fake_execute(run: RunState, units: list[WorkUnit], notes: list[str], negotiate_first: bool) -> None:
        run.fm.write("index.html", "<html><body><script src='app.js'></script></body></html>\n")
        run.files["index.html"] = {"agent": units[0].agent, "content": "<html></html>", "done": True}

    async def fake_review(run: RunState, agent: Specialist, units: list[WorkUnit]) -> str:
        run.verification = FAILING_REPORT
        return "two problems"

    async def fake_repair(run: RunState, agent: Specialist, units: list[WorkUnit], rounds: int = 1) -> dict:
        repair_calls.append(rounds)
        return FAILING_REPORT

    monkeypatch.setattr(orch.planning, "run", fake_plan)
    monkeypatch.setattr(orch.execution, "run", fake_execute)
    monkeypatch.setattr(orch.review, "run", fake_review)
    monkeypatch.setattr(orch.review, "repair", fake_repair)

    try:
        planner = Specialist(
            id="p1", name="Ada", role="planner", model="gpt-4o-mini", baseUrl="https://api.openai.com/v1"
        )
        run = RunState(run_id="gate", prompt="build a page", specialists=[planner], depth="quick")
        run.workspace = tmp_path / "gate"
        run.workspace.mkdir(parents=True, exist_ok=True)
        asyncio.run(orch.Orchestrator(run).run_all())

        kinds = [event["type"] for event in run.events]
        assert "done" not in kinds, kinds
        assert kinds[-1] == "error", kinds
        blocked = run.events[-1]
        assert blocked.get("blocked") is True, blocked
        assert "verification error" in blocked["message"], blocked
        assert blocked["verification"]["issues"][0]["code"] == "missing_asset"
        assert repair_calls, "the bounded repair round must run before the run is refused"
        assert run.done is False

        ledger = json.loads((tmp_path / "gate" / ".harness-metrics.json").read_text(encoding="utf-8"))
        assert ledger["done"] is False
        assert ledger["verification"]["ok"] is False
        saved_handoff = tmp_path / "handoffs" / "gate.md"
        assert saved_handoff.is_file(), "a blocked verification result must be available to the next run"
        assert "missing_asset" in saved_handoff.read_text(encoding="utf-8")
    finally:
        state_mod.RUNS_ROOT = original_root


def test_a_leaked_file_marker_is_a_verification_error() -> None:
    from ai_harness.nodes import verify

    report = verify.check(
        {"index.html": {"content": "<!doctype html><html><body>hello</body></html>\n>>>\n"}},
        ["index.html"],
    )
    assert report["ok"] is False
    assert any(issue["code"] == "stray_marker" for issue in report["issues"])


def test_a_next_manifest_on_a_vite_tree_is_a_mismatch() -> None:
    from ai_harness.nodes import verify

    files = {
        "package.json": {
            "content": json.dumps(
                {"scripts": {"dev": "next dev", "build": "next build"}, "dependencies": {"next": "^14.1.0"}}
            )
        },
        "index.html": {"content": '<div id="root"></div><script type="module" src="/src/main.tsx"></script>'},
        "src/main.tsx": {"content": "import App from './App';"},
        "src/App.tsx": {"content": "export default function App() { return <p>hi</p>; }"},
    }
    report = verify.check(files, list(files))
    assert report["ok"] is False
    assert any(issue["code"] == "manifest_mismatch" for issue in report["issues"])


def test_a_vite_manifest_with_an_entry_shell_passes() -> None:
    from ai_harness.nodes import verify

    files = {
        "package.json": {
            "content": json.dumps(
                {"scripts": {"dev": "vite", "build": "vite build"}, "devDependencies": {"vite": "^5.4.0"}}
            )
        },
        "index.html": {"content": '<div id="root"></div><script type="module" src="/src/main.tsx"></script>'},
        "src/main.tsx": {"content": "import App from './App';"},
        "src/App.tsx": {"content": "export default function App() { return <p>hi</p>; }"},
    }
    report = verify.check(files, list(files))
    assert not any(issue["code"] in ("manifest_mismatch", "missing_entry") for issue in report["issues"])


def test_an_entry_that_duplicates_the_root_component_is_an_error() -> None:
    """main.tsx writing its own page leaves App.tsx unreferenced; both look valid alone."""
    from ai_harness.nodes import verify

    files = {
        "src/main.tsx": {"content": "const App = () => <main>hello</main>; render(<App />);"},
        "src/App.tsx": {"content": "export default function App() { return <main>hello</main>; }"},
    }
    report = verify.check(files, list(files))
    codes = [issue["code"] for issue in report["issues"]]
    assert "unreferenced_root" in codes, report["issues"]
    assert not report["ok"]
