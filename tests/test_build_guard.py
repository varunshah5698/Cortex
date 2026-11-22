"""A preview is a verdict on finished work, not a progress bar.

Building a half-written tree is how an empty `package.json` became a failed build while
the team was still thinking. The API refuses that request; the read-drop and daemon
preflight have their own checks below.
"""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_harness import main as api  # noqa: E402
from ai_harness.core.state import RunState  # noqa: E402
from ai_harness.models import Specialist  # noqa: E402
from ai_harness.modules import llm, runner  # noqa: E402


def test_serve_refuses_a_run_that_is_still_writing(tmp_path, monkeypatch):
    run = RunState(run_id="r1", prompt="build a page", specialists=[], done=False)
    monkeypatch.setattr(api, "_run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(api.harness, "get", lambda _run_id: run)

    with pytest.raises(api.HTTPException) as raised:
        asyncio.run(api.serve_start("r1"))

    assert raised.value.status_code == 409
    assert "still writing" in raised.value.detail


def _preview_of_finished_run(tmp_path, monkeypatch, run, auto_preview: bool = True):
    """Call the real /serve route for `run`; return (seen, payload) without spawning."""
    seen: dict[str, bool] = {}

    async def fake_start(run_id, workspace, restart=False, approved=False, on_event=None):  # noqa: ANN001
        seen["approved"] = approved
        return runner.Service(run_id=run_id, workspace=workspace, approved=approved, state="starting")

    monkeypatch.setattr(api, "_run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(api.harness, "get", lambda _run_id: run)
    monkeypatch.setattr(api, "_cloud_mode", lambda: False)
    monkeypatch.setattr(api, "_vercel_mode", lambda: False)
    monkeypatch.setattr(api, "BROWSER_CHECK", False)
    monkeypatch.setattr(api, "AUTO_PREVIEW", auto_preview)
    monkeypatch.setattr(api, "_record_build", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(api, "_service_payload", lambda service, _run_id="", _browser=None: service.as_dict())
    monkeypatch.setattr(api.runner, "start", fake_start)
    return seen, asyncio.run(api.serve_start(run.run_id))


def test_a_finished_run_starts_its_local_preview_without_asking_again(tmp_path, monkeypatch):
    run = RunState(run_id="finished", prompt="build a page", specialists=[], done=True)
    run.events.append({"type": "done"})

    seen, payload = _preview_of_finished_run(tmp_path, monkeypatch, run)

    assert seen["approved"] is True, "a done run's local preview is the click already made"
    assert payload["state"] == "starting"
    assert any(event.get("kind") == "serve" for event in run.events), (
        "the automatic approval must be visible in the run's own transcript"
    )


def test_a_run_closed_out_by_an_error_still_asks_before_its_preview(tmp_path, monkeypatch):
    # A run a restart found unfinished also has `done` True so its files can be read; it
    # is not a verdict on finished work, so it keeps the approval checkpoint.
    run = RunState(run_id="closed-out", prompt="build a page", specialists=[], done=True)
    run.events.append({"type": "error", "message": "This run was interrupted when the harness restarted."})

    seen, payload = _preview_of_finished_run(tmp_path, monkeypatch, run)

    assert seen["approved"] is False
    assert payload["state"] == "starting"


def test_the_auto_preview_can_be_turned_off(tmp_path, monkeypatch):
    run = RunState(run_id="prompt-again", prompt="build a page", specialists=[], done=True)
    run.events.append({"type": "done"})

    seen, _payload = _preview_of_finished_run(tmp_path, monkeypatch, run, auto_preview=False)

    assert seen["approved"] is False, "HARNESS_AUTO_PREVIEW=0 restores the prompt"


def test_an_unreachable_local_provider_is_reported_without_a_model_call():
    spec = Specialist(
        id="local",
        name="Local",
        kind="ollama",
        baseUrl="http://127.0.0.1:9/v1",
        model="missing",
    )
    ok, detail = asyncio.run(llm.reachable(spec))
    assert not ok
    assert detail
