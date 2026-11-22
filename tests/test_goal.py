"""The goal gate: a run may not declare itself finished on its own word.

Two properties are load-bearing. A mechanical sensor must win over a model opinion, so a
goal the harness can already check never spends a call. And an unreadable evaluator answer
must never read as `met` — the whole point of the gate is that it cannot be talked into
agreeing.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_harness.modules import goal, intent  # noqa: E402


class FakeRun:
    def __init__(self, acceptance=None, files=None) -> None:
        self.prompt = "Build a counter page with a reset button and a dark mode toggle"
        self.acceptance = acceptance or []
        self.files = files or {}
        self.verification = {"ok": True, "issues": [], "errors": 0, "checked": 2}
        self.browser = None
        self.workspace = None

    def file_summary(self) -> list[dict]:
        return [{"path": path, "lines": 10} for path in self.files]


def _criteria() -> list[dict]:
    """Real criteria from the real extractor: `evidence` is a regex, so a hand-written
    fixture would test a shape the harness never produces."""
    return intent.extract("Build a page with a search box and a dark mode toggle")


def _criterion(label: str, found: bool, hard: bool = True) -> dict:
    return {"label": label, "found": found, "hard": hard, "evidence": r"\b" + label, "kind": "control"}


# The tree has to contain the capability's own vocabulary, because that is what the
# sensor searches for. A tree that merely *works* is not what this tier measures.
FULL_TREE = {"index.html": {"content": "<input id='search'><button id='toggle'>toggle dark</button>"}}
EMPTY_TREE = {"index.html": {"content": "<p>hello</p>"}}


def test_the_goal_is_derived_from_the_request_and_its_capabilities():
    run = FakeRun(acceptance=[_criterion("a counter", True), _criterion("a reset button", True)])

    statement = goal.derive(run)

    assert "counter page" in statement
    assert "a counter" in statement and "a reset button" in statement


def test_a_run_with_no_capabilities_still_states_its_goal():
    run = FakeRun()

    assert "Build a counter page" in goal.derive(run)


def test_every_named_capability_present_is_met_by_the_sensor():
    run = FakeRun(acceptance=_criteria(), files=FULL_TREE)

    verdict = goal._sensor_verdict(run, goal.derive(run))  # noqa: SLF001 - the sensor is the unit here

    assert verdict is not None and verdict.status == goal.MET
    assert verdict.by == "sensor"


def test_named_capabilities_with_no_trace_anywhere_is_unmet_by_the_sensor():
    run = FakeRun(acceptance=_criteria(), files=EMPTY_TREE)

    verdict = goal._sensor_verdict(run, goal.derive(run))  # noqa: SLF001

    assert verdict is not None and verdict.status == goal.UNMET
    assert verdict.missing, "the verdict must name what is absent"


def test_partial_coverage_is_left_to_the_reviewer_not_the_gate():
    """A gate that fires on one missing capability blocks runs the verifier passed."""
    run = FakeRun(
        acceptance=_criteria(),
        files={"index.html": {"content": "<input id='search'>"}},  # search present, toggle absent
    )

    assert goal._sensor_verdict(run, goal.derive(run)) is None  # noqa: SLF001


def test_a_sensor_verdict_never_calls_the_evaluator():
    run = FakeRun(acceptance=_criteria(), files=FULL_TREE)
    called: list[str] = []

    async def ask(system: str, user: str) -> str:
        called.append(system)
        return "VERDICT: UNMET"

    verdict = asyncio.run(goal.evaluate(run, None, ask))

    assert verdict.status == goal.MET
    assert called == [], "a mechanical sensor must win over a model opinion"


def test_the_evaluator_is_used_when_no_sensor_covers_the_goal():
    run = FakeRun()

    async def ask(system: str, user: str) -> str:
        return "VERDICT: UNMET\nMISSING: the reset button does nothing\nREASON: the handler is absent"

    verdict = asyncio.run(goal.evaluate(run, None, ask))

    assert verdict.status == goal.UNMET
    assert verdict.by == "evaluator"
    assert verdict.missing == ["the reset button does nothing"]
    assert "handler is absent" in verdict.reason


def test_an_unreadable_answer_is_unknown_never_met():
    run = FakeRun()

    async def ask(system: str, user: str) -> str:
        return "Looks good to me! Ship it."

    verdict = asyncio.run(goal.evaluate(run, None, ask))

    assert verdict.status == goal.UNKNOWN
    assert verdict.met is False


def test_a_failing_evaluator_is_unknown_not_a_refusal():
    run = FakeRun()

    async def ask(system: str, user: str) -> str:
        raise RuntimeError("provider is down")

    verdict = asyncio.run(goal.evaluate(run, None, ask))

    assert verdict.status == goal.UNKNOWN
    assert "provider is down" in verdict.reason


def test_no_evaluator_configured_is_unknown_and_does_not_block():
    run = FakeRun()

    verdict = asyncio.run(goal.evaluate(run, None, None))

    assert verdict.status == goal.UNKNOWN
    assert "no evaluator" in verdict.reason


def test_the_gate_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(goal, "GOAL_ENABLED", False)
    run = FakeRun(acceptance=_criteria(), files=EMPTY_TREE)

    verdict = asyncio.run(goal.evaluate(run, None, None))

    assert verdict.status == goal.UNKNOWN
    assert "off" in verdict.reason


def test_the_goal_is_written_and_read_back(tmp_path: Path):
    class WithWorkspace(FakeRun):
        pass

    run = WithWorkspace(acceptance=_criteria())
    run.workspace = tmp_path

    goal.write(run, "Deliver a counter page with a reset button.")

    assert (tmp_path / goal.FILENAME).is_file()
    assert goal.read(run) == "Deliver a counter page with a reset button."


def test_a_missing_goal_file_derives_one_instead_of_failing(tmp_path: Path):
    run = FakeRun()
    run.workspace = tmp_path

    assert "counter page" in goal.read(run)


def test_the_verdict_serialises_for_the_event_stream():
    verdict = goal.Verdict(status=goal.UNMET, goal="g", reason="r", missing=["a"], by="sensor")

    payload = verdict.public()

    assert payload["status"] == goal.UNMET
    assert payload["missing"] == ["a"]
