"""The workflow graph: the run's control flow, observed at the one choke point.

The graph was decorative before — `assert_transition` existed and nothing called it. The
claim under test now is that every `phase` event passes through `RunState.emit`, so the
graph sees the real sequence without any node knowing it exists, and an illegal edge is
reported rather than silently taken.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_harness.core import workflow  # noqa: E402


class FakeRun:
    def __init__(self) -> None:
        self.phase = "idle"
        self.phase_history: list[dict] = []


def test_the_happy_path_is_all_legal():
    run = FakeRun()

    for phase in ("planning", "scaffolding", "building", "reviewing", "revising", "synthesizing", "done"):
        assert workflow.observe(run, phase) is None

    assert run.phase == "done"
    assert [item["to"] for item in run.phase_history][-1] == "done"


def test_a_self_transition_is_legal_because_a_repair_pass_re_enters_its_own_phase():
    run = FakeRun()
    workflow.observe(run, "planning")
    workflow.observe(run, "building")

    assert workflow.observe(run, "building") is None


def test_an_illegal_edge_is_reported_and_the_phase_still_moves():
    """The run is not killed mid-build over an unexpected edge, but it is not silent either."""
    run = FakeRun()
    workflow.observe(run, "planning")
    workflow.observe(run, "building")

    violation = workflow.observe(run, "planning")

    assert violation is not None
    assert "building → planning" in violation
    assert run.phase == "planning"


def test_the_first_phase_is_always_legal():
    """A run starts at idle; a resumed run starts wherever its disk state left it."""
    run = FakeRun()
    run.phase = "reviewing"

    assert workflow.observe(run, "building") is None


def test_a_chat_phase_is_tracked_but_never_judged_against_the_build_graph():
    run = FakeRun()
    workflow.observe(run, "planning")

    assert workflow.observe(run, "thinking") is None
    assert workflow.observe(run, "check") is None
    assert workflow.observe(run, "answering") is None
    assert run.phase == "answering"


def test_an_empty_phase_is_ignored():
    run = FakeRun()
    workflow.observe(run, "planning")

    assert workflow.observe(run, "") is None
    assert run.phase == "planning"


def test_history_is_bounded_so_a_long_run_cannot_grow_it_without_limit():
    run = FakeRun()
    for _ in range(300):
        workflow.observe(run, "building")

    assert len(run.phase_history) <= 200


def test_assert_transition_refuses_what_the_graph_forbids():
    with pytest.raises(workflow.InvalidTransition):
        workflow.assert_transition("building", "planning")


def test_assert_transition_allows_a_self_edge():
    workflow.assert_transition("revising", "revising")


def test_legal_agrees_with_the_transition_table():
    assert workflow.legal("building", "reviewing") is True
    assert workflow.legal("building", "done") is False
    assert workflow.legal("done", "planning") is False


def test_every_phase_named_in_the_table_is_a_known_phase():
    """A transition to a phase that is not in PHASES is a typo the graph would enforce."""
    known = set(workflow.PHASES)
    for source, targets in workflow.TRANSITIONS.items():
        assert source in known, source
        assert targets <= known, targets - known


def test_a_run_with_no_phase_history_attribute_still_observes():
    """A focused test object may not carry the history field; observation must not require it."""

    class Bare:
        phase = "idle"

    bare = Bare()
    assert workflow.observe(bare, "planning") is None
    assert bare.phase == "planning"
    assert workflow.observe(bare, "building") is None
    assert bare.phase == "building"
