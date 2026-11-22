"""Model roulette: vary the model behind a role, per call, and say which one answered.

The properties worth testing are the boring ones — off means identical, a pool never
invents a model that has no key, and a seeded draw replays. The interesting one is that
the specialist keeps its identity while the connection details change, because that is
what lets the transcript still read as one agent working.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_harness.models import Specialist  # noqa: E402
from ai_harness.modules import roulette  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    roulette.reset()
    yield
    roulette.reset()


def _spec(model: str, agent_id: str = "vega", key: str = "sk-test") -> Specialist:
    return Specialist(
        id=agent_id,
        name=agent_id.title(),
        role="coder",
        model=model,
        baseUrl="https://api.example.com/v1",
        apiKey=key,
    )


def test_off_by_default_so_a_run_keeps_the_model_it_was_priced_for(monkeypatch):
    monkeypatch.setattr(roulette, "ROULETTE_ENABLED", False)
    monkeypatch.setattr(roulette, "ROULETTE_MODELS", ["other-model"])
    spec = _spec("primary")
    assert roulette.enabled() is False
    assert roulette.pick(spec, [_spec("other-model", "lyra")], "run-1") is spec
    assert roulette.record(spec, spec) is None


def test_the_pool_is_drawn_from_the_roster_and_never_invents_a_model(monkeypatch):
    monkeypatch.setattr(roulette, "ROULETTE_ENABLED", True)
    monkeypatch.setattr(roulette, "ROULETTE_MODELS", ["other-model", "not-configured"])
    spec = _spec("primary")
    roster = [_spec("other-model", "lyra"), _spec("not-configured", "nova", key="")]
    members = roulette.pool(spec, roster)
    # The specialist itself, plus only the roster row that is named *and* ready.
    assert [member.model for member in members] == ["primary", "other-model"]
    assert roulette.variants(spec, roster) == ["primary", "other-model"]


def test_a_seeded_draw_replays_exactly(monkeypatch):
    monkeypatch.setattr(roulette, "ROULETTE_ENABLED", True)
    monkeypatch.setattr(roulette, "ROULETTE_MODELS", ["b", "c"])
    monkeypatch.setattr(roulette, "ROULETTE_SEED", 1234)
    spec = _spec("a")
    roster = [_spec("b", "lyra"), _spec("c", "nova")]

    roulette.reset()
    first = [roulette.pick(spec, roster, "run-1").model for _ in range(12)]
    roulette.reset()
    second = [roulette.pick(spec, roster, "run-1").model for _ in range(12)]
    assert first == second
    # And it really does vary — a "roulette" that always returns the same model is a bug
    # that would pass every other test in this file.
    assert len(set(first)) > 1


def test_the_agent_keeps_its_identity_while_the_connection_changes(monkeypatch):
    monkeypatch.setattr(roulette, "ROULETTE_ENABLED", True)
    monkeypatch.setattr(roulette, "ROULETTE_MODELS", ["other-model"])
    spec = _spec("primary")
    other = Specialist(
        id="lyra",
        name="Lyra",
        role="reviewer",
        model="other-model",
        baseUrl="https://other.example.com/v1",
        apiKey="sk-other",
    )
    swapped = roulette.replace(spec, other)
    # Same agent, different model — the transcript still reads as Vega working.
    assert (swapped.id, swapped.name, swapped.role) == ("vega", "Vega", "coder")
    assert (swapped.model, swapped.baseUrl, swapped.apiKey) == (
        "other-model",
        "https://other.example.com/v1",
        "sk-other",
    )
    # And the original is untouched: `replace` must not mutate the roster row.
    assert spec.model == "primary"


def test_the_draw_is_reported_with_both_models(monkeypatch):
    monkeypatch.setattr(roulette, "ROULETTE_ENABLED", True)
    monkeypatch.setattr(roulette, "ROULETTE_MODELS", ["other-model"])
    event = roulette.record(_spec("other-model", "lyra"), _spec("primary"))
    assert event is not None
    assert event["type"] == "roulette"
    assert (event["from"], event["model"]) == ("primary", "other-model")
    assert "drew other-model" in event["message"]
