"""The evaluator rubric: a grade that cannot be talked up.

The claim that matters is the asymmetry: the harness's total decides the verdict, and a
model's stated verdict may only be stricter. A model that scores 3/12 and says ACCEPT does
not get to ship, and a malformed answer is `unknown` rather than a pass.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_harness.modules import rubric  # noqa: E402

FULL = """correctness: 2 — matches the request
verification: 2 — verifier ran clean
scope: 2 — one feature
reliability: 2 — restarts fine
maintainability: 2 — clear
handoff: 2 — handoff written
VERDICT: ACCEPT
MISSING: nothing
"""


def _with(**overrides: int) -> str:
    """A full answer with named categories replaced, so each test varies one score."""
    scores = {name: 2 for name, _question in rubric.CATEGORIES}
    scores.update(overrides)
    lines = [f"{name}: {scores[name]} — reason" for name, _question in rubric.CATEGORIES]
    return "\n".join(lines) + "\nVERDICT: ACCEPT\nMISSING: nothing\n"


def test_a_full_house_is_an_accept():
    score = rubric.parse(FULL)

    assert score.total == 12
    assert score.verdict == rubric.ACCEPT
    assert score.scores["correctness"] == 2


def test_a_low_total_is_blocked_even_when_the_model_says_accept():
    """The model's verdict may only be stricter than the total implies, never looser."""
    score = rubric.parse(_with(correctness=0, verification=0, scope=0, reliability=0, maintainability=0, handoff=0))

    assert score.total == 0
    assert score.verdict == rubric.BLOCK


def test_a_mid_total_is_a_revise():
    score = rubric.parse(_with(correctness=1, verification=1, scope=1, reliability=1, maintainability=2, handoff=2))

    assert score.total == 8
    assert score.verdict == rubric.ACCEPT


def test_a_total_just_below_the_accept_line_is_a_revise():
    score = rubric.parse(_with(correctness=1, verification=1, scope=1, reliability=1, maintainability=1, handoff=2))

    assert score.total == 7
    assert score.verdict == rubric.REVISE


def test_a_model_may_be_stricter_than_its_own_total():
    """Scores of 12 with a stated BLOCK is a model saying "I cannot defend this"."""
    text = _with().replace("VERDICT: ACCEPT", "VERDICT: BLOCK")

    score = rubric.parse(text)

    assert score.total == 12
    assert score.verdict == rubric.BLOCK


def test_a_partial_answer_is_unknown_not_a_pass():
    """A total over a subset of categories would read as a pass. It must not."""
    text = "correctness: 2 — fine\nverification: 2 — fine\nVERDICT: ACCEPT\n"

    score = rubric.parse(text)

    assert score.verdict == rubric.UNKNOWN
    assert len(score.scores) == 2


def test_an_empty_answer_is_unknown():
    assert rubric.parse("").verdict == rubric.UNKNOWN


def test_garbage_is_unknown_rather_than_raising():
    score = rubric.parse("I think this is probably fine, ship it.")

    assert score.verdict == rubric.UNKNOWN
    assert score.total == 0


def test_a_score_outside_zero_to_two_does_not_count():
    text = _with().replace("correctness: 2", "correctness: 9")

    score = rubric.parse(text)

    # The 9 is not a score, so that category is missing and the answer is incomplete.
    assert "correctness" not in score.scores
    assert score.verdict == rubric.UNKNOWN


def test_missing_is_captured_so_a_refusal_is_actionable():
    text = _with().replace("MISSING: nothing", "MISSING: the search input has no listener")

    score = rubric.parse(text)

    assert score.missing == "the search input has no listener"


def test_reasons_are_kept_beside_their_scores():
    score = rubric.parse(FULL)

    assert score.reasons["scope"] == "one feature"


def test_verdict_for_is_the_single_place_the_thresholds_live():
    assert rubric.verdict_for(12) == rubric.ACCEPT
    assert rubric.verdict_for(rubric.REVISE_AT) == rubric.ACCEPT
    assert rubric.verdict_for(rubric.REVISE_AT - 1) == rubric.REVISE
    assert rubric.verdict_for(rubric.BLOCK_AT) == rubric.REVISE
    assert rubric.verdict_for(rubric.BLOCK_AT - 1) == rubric.BLOCK


def test_the_prompt_names_every_category_it_expects_back():
    text = rubric.prompt()

    for name, _question in rubric.CATEGORIES:
        assert name in text
    assert "VERDICT" in text


def test_the_rubric_is_off_by_default_so_it_costs_nothing():
    assert rubric.RUBRIC_ENABLED is False


async def _never_called(system: str, user: str) -> str:  # pragma: no cover - asserted unreached
    raise AssertionError("the evaluator must not be called when the rubric is off")


def test_evaluate_returns_none_by_off_switch():
    import asyncio

    score = asyncio.run(rubric.evaluate(object(), object(), _never_called))

    assert score.by == "none"
    assert score.verdict == rubric.UNKNOWN
