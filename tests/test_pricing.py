"""Pricing coverage: a guessed price is named, never silently charged.

`HARNESS_MAX_USD` is only a cap if every model a run may use has a row in
`modules/budget.py`. These tests pin the three places that must agree: `row_for` names the
row, `/api/health` reports the roster's unpriced models, and the live eval suite refuses
to run at all when one exists.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_harness import main  # noqa: E402
from ai_harness.evals import runner as evals_runner  # noqa: E402
from ai_harness.models import Specialist  # noqa: E402
from ai_harness.modules import budget  # noqa: E402


def _spec(model: str, base: str = "https://api.openai.com/v1", spec_id: str = "p1") -> Specialist:
    return Specialist(id=spec_id, name="Ada", role="planner", model=model, baseUrl=base)


def test_a_known_model_resolves_to_its_own_row() -> None:
    # Longest pattern first: gpt-4o-mini must not be priced as gpt-4o.
    assert budget.row_for(_spec("gpt-4o-mini")) == "gpt-4o-mini"
    assert budget.row_for(_spec("claude-sonnet-4-5")) == "claude-sonnet-4"
    assert (
        budget.row_for(_spec("deepseek-ai/DeepSeek-V4.1-Flash", base="https://api.river.ai/v1"))
        == "deepseek-v4.1-flash"
    )
    assert budget.is_priced(_spec("gpt-4o-mini")) is True


def test_a_local_endpoint_is_free_whatever_the_model_is_called() -> None:
    local = _spec("some-unknown-weights", base="http://127.0.0.1:11434/v1")

    assert budget.row_for(local) == budget.LOCAL_ROW
    assert budget.is_priced(local) is True, "free is a price, not a gap"
    assert budget.price_for(local) == (0.0, 0.0)


def test_an_unlisted_model_is_named_as_unpriced_not_priced_at_the_default() -> None:
    spec = _spec("mystery-1", base="https://api.example.com/v1")

    assert budget.row_for(spec) == budget.DEFAULT_ROW
    assert budget.is_priced(spec) is False
    assert budget.unpriced([spec]) == [{"model": "mystery-1", "baseUrl": "https://api.example.com/v1", "ids": ["p1"]}]
    # The charge still happens, so a run stays bounded — it is just no longer a secret.
    assert budget.price_for(spec) == (budget.DEFAULT_INPUT, budget.DEFAULT_OUTPUT)


def test_the_health_report_tells_a_price_from_a_guess() -> None:
    report = budget.report([_spec("gpt-4o-mini"), _spec("mystery-1", spec_id="p2")])

    rows = {item["model"]: item for item in report["models"]}
    assert rows["gpt-4o-mini"]["priced"] is True
    assert rows["gpt-4o-mini"]["row"] == "gpt-4o-mini"
    assert rows["mystery-1"]["priced"] is False
    assert [item["model"] for item in report["unpriced"]] == ["mystery-1"]
    assert report["default"]["unit"] == "USD per 1M tokens"


def test_api_health_surfaces_the_rosters_unpriced_models(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeRoster:
        def load(self) -> list[Specialist]:
            return [_spec("gpt-4o-mini"), _spec("mystery-1", spec_id="p2")]

    monkeypatch.setattr(main, "ROSTER", FakeRoster())

    health = asyncio.run(main.health())

    assert health["pricing"]["unpriced"][0]["model"] == "mystery-1"
    assert health["pricing"]["unpriced"][0]["ids"] == ["p2"]
    assert [item["priced"] for item in health["pricing"]["models"]] == [True, False]


def test_the_live_suite_refuses_to_run_on_an_unpriced_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(evals_runner, "live_team", lambda: [_spec("mystery-1")])

    with pytest.raises(SystemExit) as refused:
        asyncio.run(evals_runner.run_suite(live=True, only=["counter-page"]))

    message = str(refused.value)
    assert "mystery-1" in message
    assert "HARNESS_MAX_USD" in message
    assert "HARNESS_PRICES" in message


def test_the_offline_suite_is_priced_by_construction() -> None:
    """Its team is local, so the free tier is a price and the suite still runs offline."""
    assert budget.unpriced(evals_runner.scripted_team()) == []
