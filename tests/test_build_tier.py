"""The build tier: a case's tree must compile and serve, or the case fails.

Every test stubs `modules/runner.start` and `stop`, so this file needs no npm, no pip,
no port and no network — the tier itself runs the same runner the Preview button does.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_harness.evals import __main__ as cli  # noqa: E402
from ai_harness.evals import build  # noqa: E402
from ai_harness.evals.cases import CASES, Case  # noqa: E402


class FakeService:
    """A service already in the state the test cares about."""

    def __init__(
        self,
        state: str,
        detail: str = "",
        logs: list[str] | None = None,
        port: int | None = None,
        kind: str = "static",
    ) -> None:
        self.state = state
        self.detail = detail
        self.kind = kind
        self.port = port
        self.logs = list(logs or [])


def _stub(monkeypatch: pytest.MonkeyPatch, service: FakeService, started: list[dict], stopped: list[str]) -> None:
    async def fake_start(run_id, workspace, restart=False, approved=False):  # noqa: ANN001
        started.append({"run_id": run_id, "workspace": Path(workspace), "approved": approved})
        return service

    async def fake_stop(run_id):  # noqa: ANN001
        stopped.append(run_id)
        return service

    monkeypatch.setattr(build.runner, "start", fake_start)
    monkeypatch.setattr(build.runner, "stop", fake_stop)
    monkeypatch.setattr(build, "POLL_SECONDS", 0.01)


def _case(name: str) -> Case:
    return next(case for case in CASES if case.name == name)


def _run(case: Case, workspace: Path, **kwargs) -> dict:
    return asyncio.run(build.check(case, workspace, **kwargs))


def test_a_compiled_tree_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[dict] = []
    stopped: list[str] = []
    _stub(monkeypatch, FakeService("ready", "serving on :4242", port=4242), started, stopped)

    result = _run(_case("react-tree"), tmp_path)

    assert result["ok"] is True, result
    assert result["state"] == "ready"
    assert result["port"] == 4242
    assert result["declares_build"] is True
    assert result["problem"] == ""
    assert started and started[0]["approved"] is True, "the tier must carry the approval a person gives"
    assert started[0]["workspace"] == tmp_path
    assert stopped == [started[0]["run_id"]], "a served tree must be stopped, not left listening"
    assert (tmp_path / "package.json").is_file(), "the tier writes the case's own files"
    assert (tmp_path / "src" / "App.tsx").is_file()


def test_a_dev_server_does_not_pass_for_a_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`npm run build` failing falls back to `npm run dev` inside the runner.

    A dev server answering is not a compile, so the tier must still fail the case.
    """
    started: list[dict] = []
    stopped: list[str] = []
    service = FakeService(
        "ready",
        "serving on :5199",
        port=5199,
        kind="process",
        logs=["$ npm run build", "npm run build failed (exit 1)", "$ npm run dev -- --port 5199"],
    )
    _stub(monkeypatch, service, started, stopped)

    result = _run(_case("react-tree"), tmp_path)

    assert result["ok"] is False, result
    assert result["state"] == "ready"
    assert "dev server" in result["problem"]
    assert result["port"] is None, "a tree that did not build is not reported as served"


def test_a_tree_without_a_build_script_may_serve_itself(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[dict] = []
    stopped: list[str] = []
    _stub(monkeypatch, FakeService("ready", "serving on :4242", port=4242), started, stopped)

    result = _run(_case("counter-page"), tmp_path)

    assert result["ok"] is True, result
    assert result["declares_build"] is False


def test_a_failed_build_fails_the_tier(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[dict] = []
    stopped: list[str] = []
    service = FakeService(
        "failed", "npm run build failed (exit 2)", logs=["$ npm run build", "error TS2304: Cannot find name"]
    )
    _stub(monkeypatch, service, started, stopped)

    result = _run(_case("react-tree"), tmp_path)

    assert result["ok"] is False
    assert result["state"] == "failed"
    assert "failed" in result["problem"]
    assert "TS2304" in " ".join(result["logs"])
    assert stopped, "a failed build is still stopped"


def test_a_service_that_never_settles_times_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[dict] = []
    stopped: list[str] = []
    _stub(monkeypatch, FakeService("installing", "npm install"), started, stopped)

    result = _run(_case("react-tree"), tmp_path, timeout=0.01)

    assert result["ok"] is False
    assert result["state"] == "timeout"
    assert "no ready service" in result["problem"]
    assert stopped, "a build that never settles is stopped, not left running"


def test_needs_approval_is_a_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[dict] = []
    stopped: list[str] = []
    _stub(monkeypatch, FakeService("needs_approval", "approval required · npm install"), started, stopped)

    result = _run(_case("react-tree"), tmp_path)

    assert result["ok"] is False
    assert result["state"] == "needs_approval"


def test_a_case_that_does_not_serve_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[dict] = []
    stopped: list[str] = []
    _stub(monkeypatch, FakeService("ready"), started, stopped)

    result = _run(_case("python-service"), tmp_path)

    assert result["skipped"] is True
    assert result["ok"] is None, "a skipped case is not a pass"
    assert started == [] and stopped == [], "a tree that is not meant to serve is never started"


def test_an_empty_body_is_not_written(tmp_path: Path) -> None:
    """A declared file nobody wrote stays absent, as the executor leaves it."""
    case = Case(name="empty", prompt="", plan={"files": ["a.py"]}, code={"a.py": "   \n"}, expect_ok=True, note="")

    assert build.write_tree(case, tmp_path) == []
    assert not (tmp_path / "a.py").exists()


def test_the_case_table_says_which_trees_serve() -> None:
    servable = {case.name for case in CASES if case.servable}

    assert servable == {"counter-page", "react-tree"}, servable


def _result(name: str, tier: dict | None) -> dict:
    """One suite result, as run_case returns it: every fact check passing."""
    return {
        "case": name,
        "note": "",
        "expect_ok": True,
        "live": False,
        "calls": 1,
        "ms": 1,
        "checks": {"reported_done": True, "verification": True, "files": True, "clean": True},
        "passed": 4,
        "total": 4,
        "verification_ok": True,
        "errors": [],
        "build": tier,
    }


def _suite(monkeypatch: pytest.MonkeyPatch, results: list[dict], tmp_path: Path) -> None:
    async def fake_suite(live=False, only=None, build=False, judge=False):  # noqa: ANN001
        return results

    monkeypatch.setattr(cli, "run_suite", fake_suite)
    monkeypatch.setattr(cli, "save", lambda _results: tmp_path / "results.json")


def test_a_tree_that_does_not_compile_fails_the_suite(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    tier = {
        "case": "react-tree",
        "ok": False,
        "skipped": False,
        "state": "ready",
        "kind": "process",
        "problem": "a dev server answered, so `npm run build` produced nothing servable",
        "detail": "serving on :5199",
        "ms": 1,
    }
    _suite(monkeypatch, [_result("react-tree", tier)], tmp_path)

    assert cli.main(["--build", "--case", "react-tree"]) == 1, "a tree that does not compile must fail the run"


def test_a_skipped_case_does_not_fail_the_suite(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    tier = {"case": "python-service", "ok": None, "skipped": True, "state": "skipped", "detail": "", "ms": 0}
    _suite(monkeypatch, [_result("python-service", tier)], tmp_path)

    assert cli.main(["--build", "--case", "python-service"]) == 0
