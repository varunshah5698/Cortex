"""A finished run must not replay its whole transcript to every new subscriber.

Leaving the build room and coming back re-delivered the terminal `done`, which the client
treated as fresh work and re-ran install/build/serve — the "it builds the thing again"
report. The stream now answers `?replay=0` with the verdict only.

These drive the real route through FastAPI's TestClient, so they pin the contract the
browser depends on rather than a helper's internals.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import ai_harness.core.state as state_mod  # noqa: E402
from ai_harness.core.state import RunState  # noqa: E402
from ai_harness.models import Specialist  # noqa: E402


def _frames(text: str) -> list[dict]:
    out: list[dict] = []
    for line in text.splitlines():
        if line.startswith("data: "):
            out.append(json.loads(line[len("data: ") :]))
    return out


@pytest.fixture()
def client_and_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A finished run with a long transcript, registered on the live app."""
    monkeypatch.setattr(state_mod, "RUNS_ROOT", tmp_path)
    agent = Specialist(id="a1", name="A", role="planner", model="m")
    run = RunState(run_id="replay1", prompt="build a page", specialists=[agent], depth="quick")
    run.events.append({"type": "start", "prompt": "build a page", "created": "2026-11-20T10:00:00Z"})
    for index in range(40):
        run.events.append({"type": "think", "agent": "a1", "delta": f"step {index}"})
    run.events.append({"type": "done", "files": [], "metrics": {}, "finished": "2026-11-20T10:01:00Z"})
    run.done = True

    from ai_harness import main as app_mod

    app_mod.harness.runs[run.run_id] = run
    # The `/api` guard allows a loopback page talking to a loopback API, which is exactly
    # the local-development case these tests exercise.
    # `local_browser` in the guard requires a loopback Origin as well as a loopback Host.
    client = TestClient(
        app_mod.app,
        base_url="http://127.0.0.1:8000",
        headers={"origin": "http://127.0.0.1:5178"},
    )
    try:
        yield client, run
    finally:
        app_mod.harness.runs.pop(run.run_id, None)


def test_the_full_transcript_is_still_available_by_default(client_and_run):
    """Other callers rely on the whole log; the fix must not remove it."""
    client, run = client_and_run

    response = client.get(f"/api/run/{run.run_id}/events")

    assert response.status_code == 200
    assert len(_frames(response.text)) == len(run.events)


def test_replay_zero_returns_the_verdict_not_the_transcript(client_and_run):
    """The room only needs to know the run finished, not to watch it again."""
    client, run = client_and_run

    response = client.get(f"/api/run/{run.run_id}/events?replay=0")

    assert response.status_code == 200
    frames = _frames(response.text)
    assert [frame["type"] for frame in frames] == ["start", "done"]
    # The 40 `think` frames are the work, and they are exactly what must not come back.
    assert not any(frame["type"] == "think" for frame in frames)


def test_replay_zero_accepts_the_other_false_spellings(client_and_run):
    client, run = client_and_run

    for value in ("false", "no"):
        frames = _frames(client.get(f"/api/run/{run.run_id}/events?replay={value}").text)
        assert [frame["type"] for frame in frames] == ["start", "done"], value


def test_a_live_run_still_streams_every_event(client_and_run):
    """An unfinished run has no history to skip: the flag must not truncate live work."""
    client, run = client_and_run
    run.done = False

    response = client.get(f"/api/run/{run.run_id}/events?replay=0", timeout=5)

    # The generator blocks waiting for more events, so the body is what it flushed first:
    # every event so far, not just the verdict.
    assert response.status_code == 200
    assert len(_frames(response.text)) == len(run.events)
