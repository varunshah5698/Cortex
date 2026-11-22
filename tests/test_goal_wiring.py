"""The goal gate and the task graph, wired into a real run.

The unit tests prove each module in isolation. This one proves the wiring: a run whose
build ignores the request must not reach `done`, and the graph and the goal must be on disk
before the first file is written. It drives the real orchestrator, the real verifier and the
real review node, stubbing only the two seams that reach a provider (`executor.compose` for
the code pass, `llm.call` for everything else), which is the same shape `test_review.py`
uses.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import ai_harness.core.state as state_mod  # noqa: E402
from ai_harness.agents import executor  # noqa: E402
from ai_harness.core.orchestrator import Orchestrator  # noqa: E402
from ai_harness.core.state import RunState  # noqa: E402
from ai_harness.models import Specialist, WorkUnit  # noqa: E402
from ai_harness.modules import cloud_sync, goal, llm, tasks  # noqa: E402
from ai_harness.modules.llm import Usage  # noqa: E402
from ai_harness.nodes import planning, review  # noqa: E402

# A page that runs and passes every structural check, and contains none of the two
# capabilities the request named. This is the failure mode the goal gate exists for.
IGNORES_THE_REQUEST = "<html><body><p>hello</p></body></html>\n"

# The same page, carrying the request's own vocabulary and a stylesheet, because the
# design sensor is an error the verifier already refuses and this test is about the gate.
HONOURS_THE_REQUEST = (
    "<html><head><style>body { font: 16px/1.5 system-ui; }</style></head>"
    '<body><input id="search"><button id="theme">toggle dark</button></body></html>\n'
)

PROMPT = "Build a page with a search box and a dark mode toggle"


def _stub_providers(monkeypatch, body: str) -> None:
    """Stub the two seams that reach a provider. Everything else runs for real."""

    async def compose(agent, messages, on_think, on_code, on_marker, default_path=""):  # noqa: ANN001
        return [(default_path or "index.html", body)], Usage(prompt_tokens=20, completion_tokens=9, calls=1)

    async def reason(agent, messages, on_think):  # noqa: ANN001
        return "restate: the page. decision: one file.", Usage(calls=1)

    async def critique(agent, messages, on_think):  # noqa: ANN001
        return "VERDICT: SOUND\nFINDINGS: 1. Keep the ids real.", Usage(calls=1)

    async def call(spec, messages, **kwargs):  # noqa: ANN001
        system = str((messages[0] if messages else {}).get("content", ""))
        if "Reply with 3-5 numbered findings" in system:
            return "APPROVED", Usage(calls=1)
        if "reporting a finished build" in system or "what the team built" in system:
            return "Built the page.", Usage(calls=1)
        if "The planned files:" in system:
            return "Publishes the page.", Usage(calls=1)
        return "", Usage(calls=1)

    # The review node runs for real (it is what records `run.verification`, which the gate
    # reads); only its provider stream is stubbed, which is the seam `test_review.py` uses.
    async def stream(spec, messages, think=True, stats=None, **kwargs):  # noqa: ANN001
        stats = stats or llm.StreamStats()
        stats.content.append("APPROVED")
        stats.usage.calls += 1
        yield ("content", "APPROVED")

    # The repair loop reaches the provider through the native-tool seam, not `call`. Left
    # unstubbed it makes a real request and retries it, which is how this test used to leak
    # a live model's answer into `test_review.py`'s assertions.
    async def call_tools(spec, messages, tools, **kwargs):  # noqa: ANN001
        return {"tool": "done", "args": {"summary": "nothing to repair"}}, "", Usage(calls=1)

    monkeypatch.setattr(executor, "compose", compose)
    monkeypatch.setattr(executor, "reason", reason)
    monkeypatch.setattr(executor, "critique", critique)
    monkeypatch.setattr(review.llm, "stream", stream)
    monkeypatch.setattr(review.llm, "call_tools", call_tools)
    monkeypatch.setattr(llm, "call", call)


def _run(tmp_path: Path, monkeypatch, body: str) -> RunState:
    _stub_providers(monkeypatch, body)
    # The mirror is best-effort in production and a network call in a test.
    monkeypatch.setattr(cloud_sync, "SITE_URL", "")
    monkeypatch.setattr(cloud_sync, "SECRET", "")
    monkeypatch.setattr(state_mod, "RUNS_ROOT", tmp_path)

    async def fake_plan(agent, prompt, memory=""):  # noqa: ANN001
        units = [WorkUnit(id="u1", title="page", role="frontend", file="index.html", objective="the page")]
        return "a page", units, ["index.html"], [], Usage()

    monkeypatch.setattr(planning.planner_agent, "plan", fake_plan)

    agent = Specialist(id="vega", name="Vega", role="frontend", model="stub")
    reviewer = Specialist(id="lyra", name="Lyra", role="reviewer", model="stub")
    return RunState(
        run_id="goal-wiring",
        prompt=PROMPT,
        specialists=[agent, reviewer],
        depth="quick",
        fresh=False,
    )


def _kinds(run: RunState) -> list[str]:
    return [str(event.get("type")) for event in run.events]


def test_a_build_that_ignores_the_request_does_not_reach_done(tmp_path: Path, monkeypatch) -> None:
    run = _run(tmp_path, monkeypatch, IGNORES_THE_REQUEST)

    asyncio.run(Orchestrator(run).run_all())

    kinds = _kinds(run)
    assert "done" not in kinds, f"a run that built none of the request must not report done: {kinds}"
    blocked = [event for event in run.events if event.get("type") == "error" and event.get("blocked")]
    assert blocked, f"the refusal must be a blocked error, not a silent stop: {kinds}"


def test_a_build_that_honours_the_request_reaches_done(tmp_path: Path, monkeypatch) -> None:
    run = _run(tmp_path, monkeypatch, HONOURS_THE_REQUEST)

    asyncio.run(Orchestrator(run).run_all())

    kinds = _kinds(run)
    assert "done" in kinds, f"a run that built what was asked must finish: {kinds}"
    verdicts = [event for event in run.events if event.get("type") == "goal"]
    assert verdicts, "the gate must record its verdict on every run"


def test_the_task_graph_and_the_goal_are_on_disk(tmp_path: Path, monkeypatch) -> None:
    run = _run(tmp_path, monkeypatch, HONOURS_THE_REQUEST)

    asyncio.run(Orchestrator(run).run_all())

    assert (run.workspace / tasks.FILENAME).is_file()
    assert (run.workspace / goal.FILENAME).is_file()
    seeded = [event for event in run.events if event.get("type") == "tasks"]
    assert seeded and seeded[0]["tasks"], "the plan's units must become tasks"
    assert seeded[0]["tasks"][0]["file"] == "index.html"


def test_the_goal_gate_can_be_switched_off(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(goal, "GOAL_ENABLED", False)
    run = _run(tmp_path, monkeypatch, IGNORES_THE_REQUEST)

    asyncio.run(Orchestrator(run).run_all())

    # With the gate off, the goal verdict is never recorded and the verifier alone decides.
    assert not [event for event in run.events if event.get("type") == "goal"]
