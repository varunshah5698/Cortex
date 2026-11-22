"""The eval journal: a suite that dies keeps every case that already finished.

The aggregate `save` writes is only reachable once the last case is in, so before the
journal a crash at case five cost the whole suite — and in live mode, the whole spend.
What is pinned here is that guarantee, not the file format: a case is on disk before the
next one starts, a torn line is the crash and not a result, and `--from-journal` rebuilds
the aggregate without asking a provider anything.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_harness.evals import __main__ as cli  # noqa: E402
from ai_harness.evals import runner  # noqa: E402


def _result(name: str) -> dict:
    return {
        "case": name,
        "note": "",
        "expect_ok": True,
        "live": False,
        "calls": 0,
        "ms": 1,
        "checks": {"reported_done": True, "verification": True, "files": True, "clean": True, "flagged": True},
        "passed": 5,
        "total": 5,
        "verification_ok": True,
        "errors": [],
        "build": None,
        "judge": None,
    }


def test_a_case_is_on_disk_before_the_next_one_starts(tmp_path: Path) -> None:
    """The interruption is the test: case three raises, cases one and two are still there."""
    journal = tmp_path / "suite.jsonl"
    seen: list[int] = []

    def record(result: dict) -> None:
        runner.append_journal(journal, result)
        # Read the file back at the moment the next case would begin.
        seen.append(len(runner.load_journal(journal)))

    record(_result("one"))
    record(_result("two"))

    assert seen == [1, 2], "each finished case must be readable before the next one runs"
    assert [item["case"] for item in runner.load_journal(journal)] == ["one", "two"]


def test_a_torn_final_line_is_dropped_not_raised(tmp_path: Path) -> None:
    """A half-written line is the crash itself, and it belongs to a case that never finished."""
    journal = tmp_path / "suite.jsonl"
    runner.append_journal(journal, _result("one"))
    with journal.open("a", encoding="utf-8") as handle:
        handle.write('{"case": "two", "passed": 5, "tot')  # killed mid-write

    results = runner.load_journal(journal)

    assert [item["case"] for item in results] == ["one"], "the torn case has nothing to report"


def test_a_missing_journal_is_empty_not_an_error(tmp_path: Path) -> None:
    assert runner.load_journal(tmp_path / "never-written.jsonl") == []


def test_the_aggregate_is_named_after_its_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(runner, "LAST_STAMP", "20260101T000000Z")

    path = runner.save([_result("one")])

    assert path.name == "20260101T000000Z.json"
    assert runner.journal_path("20260101T000000Z").name == "20260101T000000Z.jsonl"
    assert json.loads(path.read_text(encoding="utf-8"))["created"] == "20260101T000000Z"


def test_from_journal_rebuilds_the_aggregate_without_running_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ai_harness.evals.cases import CASES

    monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path)
    journal = runner.journal_path("20260101T000000Z")
    for case in CASES:
        runner.append_journal(journal, _result(case.name))

    async def refuse(**kwargs):  # noqa: ANN003 - the point is that it is never called
        raise AssertionError("--from-journal must not run a suite")

    monkeypatch.setattr(cli, "run_suite", refuse)

    assert cli.main(["--from-journal", "latest"]) == 0

    aggregate = json.loads((tmp_path / "20260101T000000Z.json").read_text(encoding="utf-8"))
    assert [item["case"] for item in aggregate["results"]] == [case.name for case in CASES]


def test_from_journal_refuses_to_also_spend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path)

    with pytest.raises(SystemExit) as refused:
        cli.main(["--from-journal", "latest", "--live"])

    assert refused.value.code == 2


def test_a_partial_journal_names_the_cases_that_never_ran(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point: four green rows must not read as a clean five-case suite."""
    monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path)
    journal = runner.journal_path("20260101T000000Z")
    runner.append_journal(journal, _result("one"))
    runner.append_journal(journal, _result("two"))

    absent = runner.missing_cases(runner.load_journal(journal))

    assert "one" not in absent and "two" not in absent
    assert absent, "a two-case journal of a larger suite is missing something"


def test_a_complete_journal_leaves_nothing_absent(tmp_path: Path) -> None:
    from ai_harness.evals.cases import CASES

    results = [_result(case.name) for case in CASES]

    assert runner.missing_cases(results) == []


def test_a_partial_recovery_exits_non_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path)
    journal = runner.journal_path("20260101T000000Z")
    runner.append_journal(journal, _result("one"))

    assert cli.main(["--from-journal", "latest"]) == 1, "a prefix of a suite cannot gate a change"


def test_a_case_filter_is_checked_against_the_journal(tmp_path: Path) -> None:
    """`--case` narrows what was asked for, so only those names can be absent."""
    results = [_result("one")]

    assert runner.journal_matches(results, ["one", "two"]) == ["two"]
    assert runner.journal_matches(results, None) == []
    assert runner.journal_matches(results, ["one"]) == []
