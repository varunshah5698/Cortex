"""Per-tool-call logging: name, arguments, result and elapsed time land in events.jsonl.

The run log used to record thinking, file deltas and spend, so "the search returned
nothing" and "the search never ran" looked identical. These tests hold the new `tool`
event to its contract at each place a tool is actually called.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import ai_harness.core.state as state_mod  # noqa: E402
from ai_harness.core.state import RunState  # noqa: E402
from ai_harness.models import Specialist  # noqa: E402
from ai_harness.modules import runner, tool_log  # noqa: E402
from ai_harness.nodes.execution import FileSink  # noqa: E402
from ai_harness.tools import web_search  # noqa: E402


@pytest.fixture()
def run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(state_mod, "RUNS_ROOT", tmp_path)
    spec = Specialist(id="a1", name="Ada", role="frontend", model="stub")
    return RunState(run_id="tools", prompt="build a page", specialists=[spec]), spec


def _tool_events(run_id: str = "tools") -> list[dict]:
    """The events as they were written to disk, not as they live in memory."""
    return [event for event in state_mod.read_events(run_id) if event.get("type") == "tool"]


def test_a_tool_call_is_logged_with_name_args_result_and_elapsed(run) -> None:  # noqa: ANN001
    state, _spec = run

    async def check() -> None:
        async with tool_log.call(state, "web_search.search", {"query": "vite"}, agent="a1", phase="research") as call:
            await asyncio.sleep(0.01)
            call.set_result("3 result(s)")

    asyncio.run(check())

    events = _tool_events()
    assert len(events) == 1, events
    event = events[0]
    assert event["tool"] == "web_search.search"
    assert event["args"] == {"query": "vite"}
    assert event["result"] == "3 result(s)"
    assert event["agent"] == "a1" and event["phase"] == "research"
    assert event["ok"] is True
    assert event["ms"] >= 10, event  # measured, not a placeholder


def test_a_failing_tool_is_logged_and_the_error_still_propagates(run) -> None:  # noqa: ANN001
    state, _spec = run

    async def check() -> None:
        with pytest.raises(RuntimeError):
            async with tool_log.call(state, "terminal.run", {"command": "rm -rf /"}):
                raise RuntimeError("refused by policy")

    asyncio.run(check())

    event = _tool_events()[0]
    assert event["ok"] is False
    assert "refused by policy" in event["error"]
    assert event["args"] == {"command": "rm -rf /"}


def test_file_writes_are_logged_as_tool_calls(run) -> None:  # noqa: ANN001
    state, spec = run

    async def check() -> None:
        sink = FileSink(state, spec)
        await sink.start("index.html")
        await sink.finish([("index.html", "<html><body>hi</body></html>\n")])

    asyncio.run(check())

    events = _tool_events()
    assert [event["tool"] for event in events] == ["file_manager.open", "file_manager.write"], events
    written = events[1]
    assert written["args"]["path"] == "index.html"
    assert written["args"]["bytes"] > 0
    assert "lines written" in written["result"]
    assert written["phase"] == "building" and written["agent"] == spec.id


def test_a_search_that_finds_nothing_says_so(run, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    """The case the old log could not distinguish from 'never called'."""
    state, _spec = run

    async def empty_search(query: str, limit: int) -> list[dict]:
        return []

    monkeypatch.setattr(web_search, "_search", empty_search)
    hits = asyncio.run(web_search.search("no such thing", log=tool_log.recorder(state, "a1", "research")))

    assert hits == []
    event = _tool_events()[0]
    assert event["tool"] == "web_search.search"
    assert event["args"] == {"query": "no such thing", "limit": 4}
    assert event["result"] == "0 results"


def test_the_runner_logs_the_serve_it_started(tmp_path: Path) -> None:
    workspace = tmp_path / "served"
    workspace.mkdir()
    (workspace / "index.html").write_text("<html><body>served</body></html>", encoding="utf-8")
    captured: list[dict] = []

    async def on_event(event: dict) -> None:
        captured.append(event)

    async def check() -> None:
        service = runner.Service(run_id="logged-serve", workspace=workspace, approved=True, on_event=on_event)
        await service.start()
        try:
            assert service.state == "ready", service.as_dict()
        finally:
            await service.stop()

    asyncio.run(check())

    served = [event for event in captured if event.get("tool") == "process.serve"]
    assert served, captured
    assert served[0]["ok"] is True
    assert served[0]["result"].startswith("ready on :")
    assert served[0]["ms"] >= 0
    assert "static_server.py" in served[0]["args"]["command"]
