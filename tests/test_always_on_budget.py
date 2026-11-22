"""The always-on prompt budget: what every call pays before it sees a task.

The standing text (CODE, AGENT_REPAIR, DESIGN_CONTRACT, JUDGE, REVIEW) is injected into
every call of its kind, so its size competes with the model's own output budget. These
tests pin that the number is measured, that it is measured on the template rather than on
a formatted call, and that crossing the cap produces a message naming the offender instead
of failing silently.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_harness.modules import prompts  # noqa: E402


def test_every_standing_prompt_is_measured() -> None:
    measured = prompts.always_on()

    for name in prompts.ALWAYS_ON:
        assert measured[name] > 0, f"{name} is in the always-on set but measured empty"
    assert measured["total"] == sum(measured[name] for name in prompts.ALWAYS_ON)
    assert measured["max"] == prompts.ALWAYS_ON_MAX


def test_the_measurement_is_the_template_not_a_formatted_call() -> None:
    """A formatted call carries the request and the files; the budget is the floor."""
    measured = prompts.always_on()

    assert measured["CODE"] == len(prompts.CODE)
    assert measured["CODE"] < 5_000, "a template this size is a prompt that grew without notice"


def test_the_current_standing_text_fits_the_cap() -> None:
    assert prompts.over_always_on_budget() == "", "the standing text is over its own budget"


def test_crossing_the_cap_names_the_offender(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prompts, "ALWAYS_ON_MAX", 100)

    message = prompts.over_always_on_budget()

    assert message, "a cap that is crossed must say so"
    assert "over the 100 cap" in message
    assert "CODE" in message, "the message names which constant is large"
    assert "output budget" in message, "and why the number matters"


def test_the_budget_is_recorded_with_every_eval_result() -> None:
    """The number is only useful if a run's result carries it."""
    from ai_harness.evals import runner

    source = Path(runner.__file__).read_text(encoding="utf-8")

    assert '"always_on": prompts.always_on()' in source
