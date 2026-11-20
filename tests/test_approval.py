"""The human checkpoint: nothing the model wrote runs until a person approves it.

`npm install`, `npm run build` and `pip install` execute code from manifests a model
produced, so the runner plans the whole pipeline first and refuses to spawn any of it
without an explicit approval. These tests stub the spawn, so no `npm` runs and no network
is used while the decision itself is checked.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_harness.modules import runner  # noqa: E402


def _node_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "node-run"
    workspace.mkdir()
    (workspace / "package.json").write_text(
        json.dumps({"scripts": {"build": "vite build", "dev": "vite"}}), encoding="utf-8"
    )
    (workspace / "index.html").write_text("<html><body>hi</body></html>", encoding="utf-8")
    return workspace


def _python_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "python-run"
    workspace.mkdir()
    (workspace / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    (workspace / "app.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n", encoding="utf-8")
    return workspace


def test_node_pipeline_is_planned_before_anything_runs(tmp_path: Path) -> None:
    service = runner.Service(run_id="plan-node", workspace=_node_workspace(tmp_path))
    plan = service.plan()

    assert plan[0].startswith("npm install"), plan
    assert "npm run build" in plan, plan
    assert any(step.startswith("npm run dev") for step in plan), plan
    assert all(service._needs_approval(step) for step in plan), plan


def test_python_pipeline_is_planned_before_anything_runs(tmp_path: Path) -> None:
    plan = runner.Service(run_id="plan-py", workspace=_python_workspace(tmp_path)).plan()

    assert any("pip install" in step for step in plan), plan
    assert any("uvicorn" in step for step in plan), plan


def test_install_and_build_refuse_to_start_without_approval(tmp_path: Path, monkeypatch) -> None:
    spawned: list[list[str]] = []

    async def fake_spawn(self, command, cwd, env=None):  # noqa: ANN001
        spawned.append(command)
        raise runner.RunnerError("stopped by the test after recording the command")

    monkeypatch.setattr(runner.Service, "_spawn", fake_spawn)
    service = runner.Service(run_id="no-approval", workspace=_node_workspace(tmp_path))
    asyncio.run(service.start())

    assert spawned == [], f"nothing may spawn before approval, got {spawned}"
    assert service.state == "needs_approval", service.as_dict()
    assert any("npm install" in command for command in service.pending), service.pending
    assert any("npm run build" in command for command in service.pending), service.pending
    assert service.exit_code is None


def test_the_same_pipeline_runs_once_approved(tmp_path: Path, monkeypatch) -> None:
    spawned: list[list[str]] = []

    async def fake_spawn(self, command, cwd, env=None):  # noqa: ANN001
        spawned.append(command)
        raise runner.RunnerError("stopped by the test after recording the command")

    monkeypatch.setattr(runner.Service, "_spawn", fake_spawn)
    service = runner.Service(run_id="approved", workspace=_node_workspace(tmp_path), approved=True)
    asyncio.run(service.start())

    assert spawned, "an approved run must actually reach the install step"
    assert spawned[0][:2] == ["npm", "install"], spawned
    assert service.state == "failed" and "stopped by the test" in service.detail


def test_a_static_page_needs_no_approval(tmp_path: Path) -> None:
    workspace = tmp_path / "static"
    workspace.mkdir()
    (workspace / "index.html").write_text("<html><body>hi</body></html>", encoding="utf-8")

    service = runner.Service(run_id="static", workspace=workspace)

    plan = service.plan()
    assert plan, "a static tree still has to be served by something"
    assert not any(service._needs_approval(step) for step in plan), (
        "serving files with the harness's own static server is not an `ask` step"
    )
