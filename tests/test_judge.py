"""The judge tier: advisory marks over the request, the files and the findings.

Every model call is stubbed. What is pinned here is the rubric's shape (two marks, one
score), the fact that it is asked at temperature 0, that an unreadable answer is recorded
rather than raised — and, above all, that a judge score can never change the exit status.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import ai_harness.core.state as state_mod  # noqa: E402
from ai_harness.core.state import RunState  # noqa: E402
from ai_harness.evals import __main__ as cli  # noqa: E402
from ai_harness.evals import judge  # noqa: E402
from ai_harness.evals.cases import CASES  # noqa: E402
from ai_harness.models import Specialist  # noqa: E402
from ai_harness.modules.usage import Usage  # noqa: E402

CAPTURED: dict = {}
CASE = next(case for case in CASES if case.name == "counter-page")
GOOD = '{"addresses_request": 8, "works_together": 6, "reason": "the counter and its wiring agree"}'
BROKEN = {
    "ok": False,
    "errors": 1,
    "warnings": 0,
    "checked": 1,
    "issues": [
        {
            "file": "app.js",
            "level": "error",
            "code": "missing_id",
            "detail": "queries '#count' but index.html declares no element with that id",
        }
    ],
}


def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RunState:
    monkeypatch.setattr(state_mod, "RUNS_ROOT", tmp_path)
    reviewer = Specialist(id="r1", name="Lyra", role="reviewer", model="gpt-4o-mini")
    run = RunState(run_id="judge", prompt=CASE.prompt, specialists=[reviewer])
    run.files = {
        "index.html": {"agent": "p1", "content": "<html><body><p id='count'>0</p></body></html>\n", "done": True},
        "app.js": {"agent": "p1", "content": "document.getElementById('count');\n", "done": True},
    }
    run.verification = BROKEN
    return run


def _stub(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    async def fake_call(spec, messages, think=False, on_think=None, json_mode=False, temperature=None):  # noqa: ANN001
        CAPTURED.update({"spec": spec, "messages": messages, "temperature": temperature})
        return text, Usage(prompt_tokens=900, completion_tokens=40, calls=1, cost_usd=0.0021)

    monkeypatch.setattr(judge.llm, "call", fake_call)


def test_the_lower_mark_is_the_score(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = _run(tmp_path, monkeypatch)
    _stub(monkeypatch, GOOD)

    result = asyncio.run(judge.score(CASE, run, judge.judge_agent(run)))

    assert result["addresses_request"] == 8
    assert result["works_together"] == 6
    assert result["value"] == 0.6, "the lower mark leads: neither failure may hide behind an average"
    assert result["reason"].startswith("the counter")
    assert result["judged_by"] == "Lyra"
    assert result["usage"]["cost_usd"] == 0.0021
    assert result["error"] == ""


def test_it_asks_at_temperature_zero_with_the_request_files_and_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _run(tmp_path, monkeypatch)
    _stub(monkeypatch, GOOD)

    asyncio.run(judge.score(CASE, run, judge.judge_agent(run)))

    assert CAPTURED["temperature"] == 0, "one artifact must get one verdict"
    system = CAPTURED["messages"][0]["content"]
    assert CASE.prompt in system, "the judge grades against the request"
    assert "index.html" in system and "app.js" in system, "the files go in the prompt"
    assert "missing_id" in system and "queries '#count'" in system, "the findings go in as evidence"
    assert CAPTURED["spec"] is run.specialists[0], "the reviewer grades, not a new specialist"


def test_an_unreadable_answer_is_recorded_not_raised(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = _run(tmp_path, monkeypatch)
    _stub(monkeypatch, "This build is lovely, 10/10, no notes.")

    result = asyncio.run(judge.score(CASE, run, judge.judge_agent(run)))

    assert result["value"] is None
    assert "readable score" in result["error"]
    assert result["usage"]["cost_usd"] == 0.0021, "the spend is still recorded"


def test_parse_clamps_marks_to_the_scale() -> None:
    verdict = judge.parse('{"addresses_request": 42, "works_together": -3, "reason": "wild"}')

    assert verdict is not None
    assert (verdict["addresses_request"], verdict["works_together"]) == (10, 0)
    assert verdict["value"] == 0.0
    assert judge.parse("no json here") is None


def _result(name: str, judged: dict) -> dict:
    return {
        "case": name,
        "note": "",
        "expect_ok": True,
        "live": True,
        "calls": 1,
        "ms": 1,
        "checks": {"reported_done": True, "verification": True, "files": True, "clean": True},
        "passed": 4,
        "total": 4,
        "verification_ok": True,
        "errors": [],
        "build": None,
        "judge": judged,
    }


def _suite(monkeypatch: pytest.MonkeyPatch, results: list[dict], tmp_path: Path) -> None:
    async def fake_suite(live=False, only=None, build=False, judge=False):  # noqa: ANN001
        return results

    monkeypatch.setattr(cli, "run_suite", fake_suite)
    monkeypatch.setattr(cli, "save", lambda _results: tmp_path / "results.json")


def test_a_low_judge_score_never_fails_the_suite(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    judged = {"case": "counter-page", "value": 0.1, "reason": "does not answer the request", "judged_by": "Lyra"}
    _suite(monkeypatch, [_result("counter-page", judged)], tmp_path)

    assert cli.main(["--judge", "--live", "--case", "counter-page"]) == 0, "the judge is advisory, not a gate"


def test_judge_without_live_is_refused() -> None:
    with pytest.raises(SystemExit) as refused:
        cli.main(["--judge", "--case", "counter-page"])

    assert refused.value.code == 2
