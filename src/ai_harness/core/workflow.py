"""The workflow graph: which phase may follow which.

This is the run's control flow written down (Lecture 14: a loop is a graph with one node;
a graph is what a loop becomes when the work needs explicit structure). It has two jobs:

* **`assert_transition`** is the strict form, for a caller that wants to refuse an illegal
  move outright.
* **`observe`** is the form the pipeline uses. Every `phase` event passes through
  `RunState.emit`, so this is the one place that can see the whole sequence without a node
  changing. It records the transition and reports a violation as an event rather than
  raising: a run that took an unexpected edge is worth knowing about, and is not worth
  killing mid-build over.

Self-transitions are legal. `revising → revising` is a real thing — a repair pass that
finds another file re-enters the same phase — and a graph that forbids re-entry cannot
describe a loop.
"""

from __future__ import annotations

PHASES = [
    "idle",
    "planning",
    "scaffolding",
    "research",
    "contracts",
    "building",
    "reviewing",
    "revising",
    "synthesizing",
    "done",
]

TRANSITIONS: dict[str, set[str]] = {
    "idle": {"planning"},
    "planning": {"scaffolding", "research", "contracts", "building"},
    "scaffolding": {"research", "contracts", "building"},
    "research": {"contracts", "building"},
    "contracts": {"building"},
    "building": {"reviewing"},
    "reviewing": {"revising", "synthesizing"},
    "revising": {"synthesizing"},
    "synthesizing": {"done"},
    "done": set(),
}

# Chat runs its own small graph and never enters the build phases. Naming it here keeps
# one list of legal phases in the project instead of a second one inside chat.py.
CHAT_PHASES = {"thinking", "check", "answering"}


class InvalidTransition(RuntimeError):
    pass


def assert_transition(current: str, following: str) -> None:
    if following == current:
        return
    if following not in TRANSITIONS.get(current, set()):
        raise InvalidTransition(f"{current} → {following} is not allowed")


def legal(current: str, following: str) -> bool:
    """Whether `current → following` is an edge in the graph. Self-edges always are."""
    if following == current:
        return True
    return following in TRANSITIONS.get(current, set())


def observe(run, phase: str) -> str | None:
    """Record a phase transition on `run`. Returns a violation message, or None.

    Called from `RunState.emit` for every `phase` event, so the graph sees the real
    sequence. A run's first observed phase is always legal: it starts at `idle`, and a
    resumed run starts wherever its disk state left it, which is not an edge the graph can
    judge. A chat phase is tracked separately and never compared against the build graph.
    """
    phase = str(phase or "").strip()
    if not phase:
        return None
    if phase in CHAT_PHASES:
        run.phase = phase
        return None

    previous = getattr(run, "phase", "") or "idle"
    # `seen` is what distinguishes "the run is at idle" from "this is the first phase this
    # run has ever reported". Without it a resumed run entering `building` from `reviewing`
    # would be reported as an illegal edge on its very first event.
    first = not getattr(run, "phase_seen", False)
    run.phase = phase
    run.phase_seen = True
    history = getattr(run, "phase_history", None)
    if history is not None:
        history.append({"from": previous, "to": phase})
        # A long run must not grow this list without bound; the event log has the rest.
        if len(history) > 200:
            del history[: len(history) - 200]
    if first:
        return None
    if legal(previous, phase):
        return None
    return f"{previous} → {phase} is not an edge in the workflow graph"


def next_phases(phase: str) -> list[str]:
    return sorted(TRANSITIONS.get(phase, set()))
