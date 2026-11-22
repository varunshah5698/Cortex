"""A finished run serves itself, without bypassing the approval gate.

The pipeline verifies and never compiles, so a run used to end at `done` with
`previewed: false` and nothing on a port: the files existed and the person who asked for
them had no way to look at them. `_auto_preview` closes that gap for a tree that needs no
install, and still stops at `needs_approval` for one that does.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_harness.core import orchestrator  # noqa: E402
from ai_harness.core.state import RunState  # noqa: E402
from ai_harness.models import Specialist  # noqa: E402
from ai_harness.modules import runner  # noqa: E402


class _Service:
    def __init__(self, state: str, detail: str = "") -> None:
        self.state = state
        self.detail = detail


def _run(tmp_path: Path) -> RunState:
    (tmp_path / "index.html").write_text("<!doctype html><h1>hi</h1>", encoding="utf-8")
    run = RunState(
        run_id="auto-preview-test",
        prompt="a page",
        specialists=[Specialist(id="v", name="Vega", role="frontend", model="stub")],
    )
    run.workspace = tmp_path
    run.files = {"index.html": {"agent": "v", "content": "<!doctype html><h1>hi</h1>", "done": True}}
    # A run is only served once the harness has vouched for the tree; a blocked run must
    # not be put on a port.
    run.verification = {"ok": True, "errors": 0, "warnings": 0, "checked": 1, "issues": []}
    return run


def test_a_finished_run_starts_its_own_preview(tmp_path, monkeypatch) -> None:
    calls: list[tuple[str, bool]] = []

    async def fake_start(run_id, workspace, restart=False, approved=False, on_event=None):  # noqa: ANN001
        calls.append((run_id, approved))
        return _Service("ready", "a static page")

    monkeypatch.setattr(runner, "start", fake_start)
    monkeypatch.setattr(orchestrator, "AUTO_PREVIEW", True)
    run = _run(tmp_path)

    asyncio.run(orchestrator.Orchestrator(run)._auto_preview())

    assert calls == [("auto-preview-test", False)], "the run did not start its own preview"
    assert any("Preview ready" in str(event.get("text", "")) for event in run.events)


def test_auto_preview_still_reports_needs_approval(tmp_path, monkeypatch) -> None:
    """The gate is not bypassed: a tree that needs an install still waits for a human."""

    async def fake_start(run_id, workspace, restart=False, approved=False, on_event=None):  # noqa: ANN001
        return _Service("needs_approval", "npm install")

    monkeypatch.setattr(runner, "start", fake_start)
    monkeypatch.setattr(orchestrator, "AUTO_PREVIEW", True)
    run = _run(tmp_path)

    asyncio.run(orchestrator.Orchestrator(run)._auto_preview())

    assert any("needs your approval" in str(event.get("text", "")) for event in run.events)


def test_auto_preview_can_be_switched_off(tmp_path, monkeypatch) -> None:
    calls: list[str] = []

    async def fake_start(*args, **kwargs):  # noqa: ANN002, ANN003
        calls.append("started")
        return _Service("ready")

    monkeypatch.setattr(runner, "start", fake_start)
    monkeypatch.setattr(orchestrator, "AUTO_PREVIEW", False)
    run = _run(tmp_path)

    asyncio.run(orchestrator.Orchestrator(run)._auto_preview())

    assert calls == []


def test_a_preview_failure_never_fails_the_run(tmp_path, monkeypatch) -> None:
    """A preview that cannot start is a notice, not a failed build.

    `runner.start` is patched here rather than called: a real call leaves a server holding
    a port and a Chrome process behind, which starved the browser tests in the same suite
    and made them fail on a timeout that had nothing to do with them.
    """

    async def boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("no port")

    monkeypatch.setattr(runner, "start", boom)
    monkeypatch.setattr(orchestrator, "AUTO_PREVIEW", True)
    run = _run(tmp_path)

    asyncio.run(orchestrator.Orchestrator(run)._auto_preview())

    assert any("could not start" in str(event.get("text", "")) for event in run.events)
