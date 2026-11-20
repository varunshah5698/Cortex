"""A run that is out of wall-clock time stops, and says what it has on disk.

`MAX_RUN_SECONDS` was a setting with a comment claiming the run was bounded in time and
nothing reading it. This pins the guardrail end to end: `modules/runtime.check_deadline`
refuses the next provider call, `core/orchestrator.run_all` ends the run with a
`timed_out` error that lists the files, and no `done` is ever emitted.

`asyncio.wait_for` is the bound used around the run, not `asyncio.timeout`: the project
targets py310, where the latter does not exist — and a guardrail that hangs is exactly
the bug under test.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import ai_harness.core.state as state_mod  # noqa: E402
from ai_harness.core import orchestrator as orch  # noqa: E402
from ai_harness.core.state import RunState  # noqa: E402
from ai_harness.models import Specialist  # noqa: E402
from ai_harness.modules import llm, runtime  # noqa: E402

CALLS: list[str] = []


def test_a_run_past_its_deadline_ends_timed_out_without_done(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original_root = state_mod.RUNS_ROOT
    monkeypatch.setattr(state_mod, "RUNS_ROOT", tmp_path)
    # Pinned here rather than read from the environment, so a developer's .env cannot
    # turn this test off by setting HARNESS_MAX_SECONDS=0.
    monkeypatch.setattr(runtime, "MAX_RUN_SECONDS", 60.0)

    planner = Specialist(id="p1", name="Ada", role="planner", model="gpt-4o-mini", baseUrl="https://api.openai.com/v1")

    CALLS.clear()

    async def planning(run: RunState, agent: Specialist):
        """The first stage asks the provider, exactly as every stage does."""
        async for _chunk in llm.stream(agent, [{"role": "user", "content": "plan the page"}]):
            CALLS.append("provider answered")
        return [], [], []

    async def never(*_args, **_kwargs):
        raise AssertionError("no stage may run after the deadline")

    monkeypatch.setattr(orch.planning, "run", planning)
    monkeypatch.setattr(orch.execution, "run", never)
    monkeypatch.setattr(orch.review, "run", never)

    try:
        run = RunState(run_id="deadline", prompt="build a page", specialists=[planner], depth="quick")
        # A file the run already wrote: the error has to say what survives on disk.
        run.fm.write("index.html", "<html><body>half a page</body></html>\n")
        run.files["index.html"] = {
            "agent": "p1",
            "content": "<html><body>half a page</body></html>\n",
            "done": True,
        }
        run.started_at = time.monotonic() - (runtime.MAX_RUN_SECONDS + 1)

        async def bounded() -> None:
            await asyncio.wait_for(orch.Orchestrator(run).run_all(), 5.0)

        asyncio.run(bounded())

        kinds = [event["type"] for event in run.events]
        assert "done" not in kinds, kinds
        assert kinds[-1] == "error", kinds
        assert CALLS == [], "the provider must not be called once the run is out of time"

        terminal = run.events[-1]
        assert terminal["timed_out"] is True
        assert terminal["blocked"] is True
        assert [item["path"] for item in terminal["files"]] == ["index.html"]
        assert "index.html" in terminal["message"], terminal["message"]
        assert "HARNESS_MAX_SECONDS" in terminal["message"], terminal["message"]
        assert run.done is False

        ledger = json.loads((tmp_path / "deadline" / ".harness-metrics.json").read_text(encoding="utf-8"))
        assert ledger["done"] is False
        assert (tmp_path / "deadline" / "index.html").is_file(), "the file is still on disk, as the error says"
    finally:
        state_mod.RUNS_ROOT = original_root


def test_the_deadline_is_checked_before_the_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both bounds sit at the same boundary, and time is checked even for a cache hit."""
    monkeypatch.setattr(runtime, "MAX_RUN_SECONDS", 1.0)
    monkeypatch.setattr(state_mod, "RUNS_ROOT", tmp_path)
    planner = Specialist(id="p1", name="Ada", role="planner", model="gpt-4o-mini")
    run = RunState(run_id="deadline-unit", prompt="hi", specialists=[planner])
    runtime.bind(run)
    try:
        runtime.check_deadline()  # a fresh run is not over its limit
        run.started_at = time.monotonic() - 5.0
        with pytest.raises(runtime.RunDeadlineExceeded):
            asyncio.run(llm.call(planner, [{"role": "user", "content": "hi"}]))
    finally:
        runtime.bind(None)
