"""The workflow graph: which phase may follow which."""

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


class InvalidTransition(RuntimeError):
    pass


def assert_transition(current: str, following: str) -> None:
    if following not in TRANSITIONS.get(current, set()):
        raise InvalidTransition(f"{current} → {following} is not allowed")


def next_phases(phase: str) -> list[str]:
    return sorted(TRANSITIONS.get(phase, set()))
