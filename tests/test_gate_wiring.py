"""The new gates, wired into a real run.

The unit tests prove each module in isolation. This proves the wiring: the feature list is
seeded before any file is written, the clean-state gate can refuse a `done`, the handoff
lands on disk, and the phase graph observes the real sequence. It drives the real
orchestrator, verifier and review node, stubbing only the seams that reach a provider —
the same shape `test_goal_wiring.py` uses.
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
from ai_harness.modules import cleanstate, cloud_sync, features, handoff, llm, rubric  # noqa: E402
from ai_harness.modules.llm import Usage  # noqa: E402
from ai_harness.nodes import planning, review  # noqa: E402

# A page that passes every structural check and carries the request's own vocabulary.
GOOD_PAGE = (
    "<html><head><style>body { font: 16px/1.5 system-ui; }</style></head>"
    '<body><input id="search"><button id="theme">toggle dark</button></body></html>\n'
)

PROMPT = "Build a page with a search box and a dark mode toggle"


def _stub_providers(monkeypatch, body: str) -> None:
    """Stub only the seams that reach a provider. Everything else runs for real."""

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

    async def stream(spec, messages, think=True, stats=None, **kwargs):  # noqa: ANN001
        stats = stats or llm.StreamStats()
        stats.content.append("APPROVED")
        stats.usage.calls += 1
        yield ("content", "APPROVED")

    async def call_tools(spec, messages, tools, **kwargs):  # noqa: ANN001
        return {"tool": "done", "args": {"summary": "nothing to repair"}}, "", Usage(calls=1)

    monkeypatch.setattr(executor, "compose", compose)
    monkeypatch.setattr(executor, "reason", reason)
    monkeypatch.setattr(executor, "critique", critique)
    monkeypatch.setattr(review.llm, "stream", stream)
    monkeypatch.setattr(review.llm, "call_tools", call_tools)
    monkeypatch.setattr(llm, "call", call)


def _run(tmp_path: Path, monkeypatch, body: str = GOOD_PAGE) -> RunState:
    _stub_providers(monkeypatch, body)
    monkeypatch.setattr(cloud_sync, "SITE_URL", "")
    monkeypatch.setattr(cloud_sync, "SECRET", "")
    monkeypatch.setattr(state_mod, "RUNS_ROOT", tmp_path)

    async def fake_plan(agent, prompt, memory=""):  # noqa: ANN001
        units = [WorkUnit(id="u1", title="page", role="frontend", file="index.html", objective="the page")]
        return "a page", units, ["index.html"], [], Usage()

    monkeypatch.setattr(planning.planner_agent, "plan", fake_plan)

    agent = Specialist(id="vega", name="Vega", role="frontend", model="stub")
    reviewer = Specialist(id="lyra", name="Lyra", role="reviewer", model="stub")
    return RunState(run_id="gate-wiring", prompt=PROMPT, specialists=[agent, reviewer], depth="quick", fresh=False)


def _kinds(run: RunState) -> list[str]:
    return [str(event.get("type")) for event in run.events]


def test_the_feature_list_is_seeded_before_any_file_is_written(tmp_path: Path, monkeypatch) -> None:
    run = _run(tmp_path, monkeypatch)

    asyncio.run(Orchestrator(run).run_all())

    assert "features" in _kinds(run), f"the feature list must be announced: {_kinds(run)}"
    assert (run.workspace / features.FILENAME).is_file()
    seeded = features.load(run)
    assert [feature.id for feature in seeded] == ["u1"]
    # Seeded, not passed: nothing has evidence yet at seed time.
    assert seeded[0].status == features.NOT_STARTED


def test_a_run_that_leaves_an_empty_planned_file_does_not_reach_done(tmp_path: Path, monkeypatch) -> None:
    """An empty planned file is refused, and the refusal is a blocked error.

    The verifier catches this before the clean-state gate gets a turn — which is the right
    order: the cheaper, older sensor fires first. What this test guards is the outcome the
    operator sees, not which gate produced it.
    """
    run = _run(tmp_path, monkeypatch, body="")
    run.planned_files = ["index.html", "style.css"]
    run.files["style.css"] = {"agent": "vega", "content": "", "done": False}

    asyncio.run(Orchestrator(run).run_all())

    kinds = _kinds(run)
    assert "done" not in kinds, f"an empty planned file must not ship: {kinds}"
    blocked = [event for event in run.events if event.get("type") == "error" and event.get("blocked")]
    assert blocked, f"the refusal must be a blocked error: {kinds}"


def test_the_clean_state_gate_refuses_what_the_verifier_cannot_see(tmp_path: Path, monkeypatch) -> None:
    """A feature passing without evidence is invisible to the verifier; the gate catches it."""
    run = _run(tmp_path, monkeypatch)

    asyncio.run(Orchestrator(run).run_all())
    assert "done" in _kinds(run), "the baseline run must finish for this test to mean anything"

    # Forge the claim the way a bug or a hand-edit would, after the build is verified.
    (run.workspace / features.FILENAME).write_text(
        '{"features": [{"id": "u1", "title": "T", "status": "passing", "evidence": []}]}',
        encoding="utf-8",
    )
    report = cleanstate.check(run)

    assert report["ok"] is False
    assert any(item["code"] == "passing_without_evidence" for item in report["issues"])


def test_a_clean_run_reaches_done_and_writes_a_handoff(tmp_path: Path, monkeypatch) -> None:
    run = _run(tmp_path, monkeypatch)

    asyncio.run(Orchestrator(run).run_all())

    kinds = _kinds(run)
    assert "done" in kinds, f"a clean run must finish: {kinds}"
    written = tmp_path / handoff.HANDOFF_DIR / f"{run.run_id}.md"
    assert written.is_file(), f"a finished run must leave the next one a starting point (looked in {written})"
    assert "Session Handoff" in written.read_text(encoding="utf-8")


def test_the_phase_graph_observes_the_real_sequence(tmp_path: Path, monkeypatch) -> None:
    """Every phase event passes through `RunState.emit`, so the graph sees the run."""
    run = _run(tmp_path, monkeypatch)

    asyncio.run(Orchestrator(run).run_all())

    assert run.phase_history, "the graph must have recorded the transitions it observed"
    assert run.phase_seen is True
    # A well-formed run takes only legal edges, so nothing should be flagged.
    violations = [event for event in run.events if event.get("graph_violation")]
    assert not violations, f"the pipeline took an edge the graph forbids: {violations}"


def test_the_rubric_is_off_so_a_run_spends_no_call_on_it(tmp_path: Path, monkeypatch) -> None:
    run = _run(tmp_path, monkeypatch)

    asyncio.run(Orchestrator(run).run_all())

    assert rubric.RUBRIC_ENABLED is False
    assert "rubric" not in _kinds(run)


def test_the_clean_state_gate_can_be_switched_off(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(cleanstate, "CLEAN_STATE", False)
    run = _run(tmp_path, monkeypatch)
    run.planned_files = ["index.html", "style.css"]
    run.files["style.css"] = {"agent": "vega", "content": "", "done": False}

    asyncio.run(Orchestrator(run).run_all())

    # With the gate off the empty file no longer blocks, which is the old behaviour.
    assert not any(event.get("type") == "cleanstate" and event.get("issues") for event in run.events)
