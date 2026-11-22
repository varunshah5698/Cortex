"""Typed exit statuses: a run ends with a name, and the name is the reason.

The point of these tests is the distinction the harness did not have before: `done` is a
boolean and a boolean cannot tell a run that submitted from one that ran out of money.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_harness.modules import exit as ex  # noqa: E402


class BudgetExceeded(RuntimeError):
    """Stands in for `modules.budget.BudgetExceeded` without importing the cycle."""


class RunDeadlineExceeded(RuntimeError):
    """Stands in for `modules.runtime.RunDeadlineExceeded`."""


class ProviderError(RuntimeError):
    """Stands in for `modules.adapters.ProviderError`."""


def test_a_run_that_emitted_done_without_a_clean_check_did_not_submit():
    # The whole reason this module exists: `done` is an event, `submitted` is a claim the
    # checks agreed with, and the two are not the same thing.
    assert ex.of(True, {"ok": False, "errors": 3}) == ex.UNVERIFIED
    assert ex.of(True, {"ok": True, "errors": 0}) == ex.SUBMITTED
    # No report at all is not a pass either — nothing vouched for the tree.
    assert ex.of(True, None) == ex.SUBMITTED
    assert ex.of(False, {"ok": True}) == ex.UNKNOWN


def test_only_submitted_counts_as_success():
    assert ex.SUCCESS == {ex.SUBMITTED}
    assert ex.UNVERIFIED not in ex.SUCCESS


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (BudgetExceeded("spend budget exhausted: $20.00/$20.00 used"), ex.LIMITS),
        (RunDeadlineExceeded("the run used 7200s of its 7200s wall-clock limit"), ex.TIME),
        (ProviderError("anthropic: connection reset"), ex.PROVIDER),
        (RuntimeError("RepeatedFormatError"), ex.FORMAT),
        (RuntimeError("Run cancelled"), ex.CANCELLED),
        (ValueError("something else entirely"), ex.UNKNOWN),
    ],
)
def test_each_stop_is_classified_by_its_own_name(error, expected):
    assert ex.classify(error) == expected


def test_money_wins_over_the_provider_error_it_caused():
    # A provider error that arrives after the budget is gone is a symptom of the stop,
    # not its cause, so the precedence list puts the budget first.
    assert ex.classify(RuntimeError("provider failed: budget exhausted")) == ex.LIMITS


def test_every_status_has_a_sentence_and_an_unknown_does_not_crash():
    for status in ex.ALL:
        assert ex.describe(status)
    assert "unrecognised" in ex.describe("nonsense")


def test_run_exit_carries_the_words_that_explain_it():
    stopped = ex.RunExit(ex.LIMITS, "out of money", submission="wrote 3 files")
    assert stopped.as_dict() == {"exit_status": ex.LIMITS, "submission": "wrote 3 files"}
    assert str(stopped) == "out of money"
    # An unrecognised status is normalised rather than passed through as a lie.
    assert ex.RunExit("invented").status == ex.UNKNOWN
