"""Flock: one request split across several models, composed back into one answer.

The feature is easy to fake in a UI and hard to make honest in the harness. These tests
hold the three claims that matter: the split is parsed rather than assumed, each part is
answered by a specialist that can actually run, and a part that fails is reported instead
of silently dropped from the answer.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import ai_harness.core.state as state_mod  # noqa: E402
from ai_harness.core import flock  # noqa: E402
from ai_harness.core.state import RunState  # noqa: E402
from ai_harness.models import Specialist  # noqa: E402
from ai_harness.modules import llm  # noqa: E402


def _team() -> list[Specialist]:
    return [
        Specialist(id="a1", name="Ada", role="planner", model="stub-a"),
        Specialist(id="f1", name="Fay", role="frontend", model="stub-f"),
        Specialist(id="b1", name="Bo", role="backend", model="stub-b"),
        Specialist(id="s1", name="Sol", role="synthesizer", model="stub-s"),
    ]


def test_a_plan_is_read_into_one_part_per_task_line():
    raw = (
        "TASK: The page shell | frontend | Write the markup and styles.\n"
        "TASK: The data layer | backend | Write the fetch and state handling.\n"
    )

    parts = flock.parse_plan(raw, "build a dashboard")

    assert [part.role for part in parts] == ["frontend", "backend"]
    assert parts[0].title == "The page shell"
    assert parts[1].instruction == "Write the fetch and state handling."


def test_a_planner_that_ignores_the_format_degrades_to_one_part():
    parts = flock.parse_plan("Sure! Here is my thinking about the request.", "build a dashboard")

    assert len(parts) == 1, "an unparseable plan must not invent work"
    assert parts[0].instruction == "build a dashboard"


def test_a_single_task_line_is_not_a_split():
    # One part is a chat turn wearing a flock's clothes; the caller needs to know that.
    parts = flock.parse_plan("TASK: Everything | general | Do the whole thing.", "build a dashboard")

    assert len(parts) == 1


def test_parts_are_owned_by_the_role_that_fits_and_never_by_nobody():
    parts = [
        flock.Part(id="p1", title="UI", role="frontend", instruction="markup"),
        flock.Part(id="p2", title="API", role="backend", instruction="routes"),
        flock.Part(id="p3", title="Launch", role="marketing", instruction="copy"),
    ]

    flock.assign(parts, _team())

    assert parts[0].agent == "f1"
    assert parts[1].agent == "b1"
    # No marketing specialist on this team: it still gets an owner rather than a dead part.
    assert parts[2].agent in {"a1", "f1", "b1", "s1"}


def test_a_failed_part_is_reported_and_left_out_of_the_synthesis(monkeypatch, tmp_path):
    monkeypatch.setattr(state_mod, "RUNS_ROOT", tmp_path)
    seen: list[str] = []

    async def fake_call(spec, messages, **kwargs):  # noqa: ANN001
        return "TASK: UI | frontend | markup\nTASK: API | backend | routes", llm.Usage()

    async def fake_stream(spec, messages, think=True, stats=None, **kwargs):  # noqa: ANN001
        system = messages[0]["content"]
        if "composing the final answer" in system:
            seen.append(system)
            stats.content.append("The answer.")
            yield ("token", "The answer.")
            return
        if spec.id == "b1":
            raise llm.ProviderError("provider refused")
        stats.content.append("markup done")
        yield ("token", "markup done")

    monkeypatch.setattr(llm, "call", fake_call)
    monkeypatch.setattr(llm, "stream", fake_stream)

    run = RunState(run_id="flock1", prompt="build a dashboard", specialists=_team(), kind="flock")
    asyncio.run(flock.run_flock(run, _team(), [{"role": "user", "content": "build a dashboard"}]))

    synth = seen[0]
    assert "markup done" in synth
    assert "did not come back" in synth, "the synthesizer must be told what is missing"
    assert "API" in synth
    assert run.answer == "The answer."
    assert any(event.get("type") == "flock" and event.get("status") == "error" for event in run.events)


def test_a_team_of_one_answers_instead_of_pretending_to_fan_out(monkeypatch, tmp_path):
    monkeypatch.setattr(state_mod, "RUNS_ROOT", tmp_path)
    called: list[str] = []

    async def fake_answer(run, specialist, messages, depth):  # noqa: ANN001
        called.append(specialist.id)
        run.answer = "single answer"

    monkeypatch.setattr("ai_harness.core.chat.run_chat", fake_answer)

    solo = [Specialist(id="a1", name="Ada", role="general", model="stub")]
    run = RunState(run_id="flock2", prompt="hello", specialists=solo, kind="flock")
    asyncio.run(flock.run_flock(run, solo, [{"role": "user", "content": "hello"}]))

    assert called == ["a1"]
    assert any(event.get("kind") == "flock" for event in run.events)


def test_every_part_is_charged_to_the_run_ledger(monkeypatch, tmp_path):
    monkeypatch.setattr(state_mod, "RUNS_ROOT", tmp_path)

    async def fake_call(spec, messages, **kwargs):  # noqa: ANN001
        return "TASK: UI | frontend | markup\nTASK: API | backend | routes", llm.Usage(prompt_tokens=10)

    async def fake_stream(spec, messages, think=True, stats=None, **kwargs):  # noqa: ANN001
        stats.usage.prompt_tokens = 5
        stats.usage.completion_tokens = 5
        stats.content.append("ok")
        yield ("token", "ok")

    monkeypatch.setattr(llm, "call", fake_call)
    monkeypatch.setattr(llm, "stream", fake_stream)

    run = RunState(run_id="flock3", prompt="build", specialists=_team(), kind="flock")
    asyncio.run(flock.run_flock(run, _team(), [{"role": "user", "content": "build"}]))

    # planner + two parts + synthesis: four agents must appear in the ledger, or the
    # most expensive thing a chat can do is the one thing the budget cannot see.
    assert len(run.usage) == 4
    assert run.metrics()["totals"]["total_tokens"] > 0
