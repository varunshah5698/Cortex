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
from ai_harness.modules import llm  # noqa: E402


def test_serve_refuses_a_run_that_is_still_writing(tmp_path, monkeypatch):
    run = RunState(run_id="r1", prompt="build a page", specialists=[], done=False)
    monkeypatch.setattr(api, "_run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(api.harness, "get", lambda _run_id: run)

    with pytest.raises(api.HTTPException) as raised:
        asyncio.run(api.serve_start("r1"))

    assert raised.value.status_code == 409
    assert "still writing" in raised.value.detail


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
