"""`done` is a verification verdict, not a build report.

Nothing in the pipeline installs or compiles a run's files, so a `done` that only says
"done" reads as "checked and built". Both halves are carried explicitly — `build` and
`previewed` — in the event, in `metrics()` and in `.harness-metrics.json`, and serving is
the only thing that can change the second half.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import ai_harness.core.state as state_mod  # noqa: E402
from ai_harness import main  # noqa: E402
from ai_harness.core import orchestrator as orch  # noqa: E402
from ai_harness.core.state import RunState  # noqa: E402
from ai_harness.models import Specialist, WorkUnit  # noqa: E402
from ai_harness.modules.llm import Usage  # noqa: E402

CLEAN_REPORT = {"ok": True, "errors": 0, "warnings": 0, "checked": 1, "issues": []}


def test_a_finished_run_says_verified_and_not_compiled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original_root = state_mod.RUNS_ROOT
    state_mod.RUNS_ROOT = tmp_path

    async def fake_plan(run: RunState, agent: Specialist):
        units = [
            WorkUnit(id="u1", title="Markup", role="frontend", file="index.html", objective="markup", agent=agent.id)
        ]
        return units, ["index.html"], []

    async def fake_execute(run: RunState, units: list[WorkUnit], notes: list[str], negotiate_first: bool) -> None:
        run.fm.write("index.html", "<!doctype html><title>ok</title>\n")
        run.files["index.html"] = {"agent": units[0].agent, "content": "<!doctype html><title>ok</title>", "done": True}

    async def fake_review(run: RunState, agent: Specialist, units: list[WorkUnit]) -> str:
        run.verification = CLEAN_REPORT
        return "clean"

    async def fake_call(agent: Specialist, messages: list[dict], **kwargs):  # noqa: ANN001
        return "wrote index.html", Usage(prompt_tokens=10, completion_tokens=4, calls=1)

    monkeypatch.setattr(orch.planning, "run", fake_plan)
    monkeypatch.setattr(orch.execution, "run", fake_execute)
    monkeypatch.setattr(orch.review, "run", fake_review)
    monkeypatch.setattr(orch.llm, "call", fake_call)

    try:
        planner = Specialist(
            id="p1", name="Ada", role="planner", model="gpt-4o-mini", baseUrl="https://api.openai.com/v1"
        )
        run = RunState(run_id="verdict", prompt="build a page", specialists=[planner], depth="quick")
        asyncio.run(orch.Orchestrator(run).run_all())

        assert run.events[-1]["type"] == "done", [event["type"] for event in run.events]
        done = run.events[-1]
        # The two keys the UI needs, present and honest on a run nobody compiled.
        assert done["previewed"] is False
        assert done["build"] is None
        assert done["metrics"]["previewed"] is False
        assert done["metrics"]["build"] is None

        ledger = json.loads((tmp_path / "verdict" / ".harness-metrics.json").read_text(encoding="utf-8"))
        assert ledger["done"] is True
        assert ledger["verification"]["ok"] is True
        assert ledger["previewed"] is False
        assert ledger["build"] is None
    finally:
        state_mod.RUNS_ROOT = original_root


def test_serving_is_what_makes_a_run_previewed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original_root = state_mod.RUNS_ROOT
    state_mod.RUNS_ROOT = tmp_path

    class FakeService:
        """Only `as_dict` is read, and it is the same dict the HTTP endpoint returns."""

        def as_dict(self) -> dict:
            return {
                "run_id": "served",
                "state": "ready",
                "kind": "static",
                "detail": "built with npm run build, served from dist/",
                "url": "http://127.0.0.1:5199/",
                "port": 5199,
                "token": "secret",
                "exit_code": None,
                "pending": [],
                "logs": [],
            }

    try:
        planner = Specialist(
            id="p1", name="Ada", role="planner", model="gpt-4o-mini", baseUrl="https://api.openai.com/v1"
        )
        run = RunState(run_id="served", prompt="build a page", specialists=[planner], depth="quick")
        run.done = True
        run.save_metrics()
        before = json.loads((tmp_path / "served" / ".harness-metrics.json").read_text(encoding="utf-8"))
        assert before["previewed"] is False and before["build"] is None

        main._record_build(run, FakeService())  # type: ignore[arg-type]

        assert run.previewed is True
        assert run.build is not None and run.build["state"] == "ready"
        ledger = json.loads((tmp_path / "served" / ".harness-metrics.json").read_text(encoding="utf-8"))
        assert ledger["previewed"] is True
        assert ledger["build"]["state"] == "ready"
        assert ledger["build"]["kind"] == "static"
        # The ledger is what /api/run/{id}/metrics serves after a reload.
        assert ledger["build"]["detail"].startswith("built with npm run build")
    finally:
        state_mod.RUNS_ROOT = original_root


def test_a_restored_run_keeps_the_preview_it_had(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The preview happens after `done`, so only the ledger can say it happened."""
    original_root = state_mod.RUNS_ROOT
    state_mod.RUNS_ROOT = tmp_path
    workspace = tmp_path / "restored"
    workspace.mkdir()
    events = [
        {"type": "start", "prompt": "build a page", "kind": "build"},
        {"type": "done", "build": None, "previewed": False},
    ]
    (workspace / state_mod.EVENT_LOG).write_text(
        "\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8"
    )
    (workspace / ".harness-metrics.json").write_text(
        json.dumps(
            {
                "done": True,
                "previewed": True,
                "build": {"state": "ready", "kind": "static", "detail": "serving dist/", "at": "2026-01-01T00:00:00Z"},
            }
        ),
        encoding="utf-8",
    )
    try:
        run = state_mod.restore_run("restored")

        assert run is not None
        assert run.previewed is True
        assert run.build is not None and run.build["state"] == "ready"
        assert run.metrics()["previewed"] is True
        assert run.metrics()["build"]["kind"] == "static"
    finally:
        state_mod.RUNS_ROOT = original_root
