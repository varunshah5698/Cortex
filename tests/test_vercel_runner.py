"""Vercel mode: per-run static/Node hosting, with an explicit unsupported case."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_harness.modules import vercel_runner  # noqa: E402


def test_a_static_or_node_tree_is_supported(tmp_path: Path) -> None:
    assert vercel_runner.supported(tmp_path) is False
    (tmp_path / "index.html").write_text("ok", encoding="utf-8")
    assert vercel_runner.supported(tmp_path) is True
    (tmp_path / "index.html").unlink()
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    assert vercel_runner.supported(tmp_path) is True


def test_the_project_name_is_lowercase_and_stable() -> None:
    assert vercel_runner._project("AbC123") == "cortex-abc123"  # noqa: SLF001


def test_a_vercel_service_payload_is_remote(tmp_path: Path) -> None:
    service = vercel_runner.VercelService(run_id="r1", project="cortex-r1", workdir=tmp_path)
    service.url = "https://cortex-r1.vercel.app"
    service.state = "ready"
    payload = service.as_dict()
    assert payload["remote"] is True
    assert payload["kind"] == "vercel"
    assert payload["url"] == "https://cortex-r1.vercel.app"
    assert payload["port"] is None
    assert payload["browser"] is None


def test_a_failed_deployment_reports_the_cli_reason() -> None:
    lines = ["Vercel CLI 59.10.0", "Error: Not authorized", ""]
    assert vercel_runner._reason(lines) == "Error: Not authorized"  # noqa: SLF001


def test_a_failed_vercel_deployment_falls_back_to_the_cloud_runner(tmp_path: Path, monkeypatch) -> None:
    import asyncio

    from ai_harness import main as api

    failed = vercel_runner.VercelService(run_id="r1", project="cortex-r1")
    failed.state = "failed"
    failed.detail = "Vercel exited 1: Error: Not authorized"

    async def deploy(_run_id: str, _workspace: Path) -> vercel_runner.VercelService:
        return failed

    async def start(_run_id: str, _workspace: Path) -> object:
        return api.remote_runner.RemoteService(run_id="r1", url="https://cortex-runner.test/runs/r1/token/")

    monkeypatch.setattr(api, "_run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(api.harness, "get", lambda _run_id: None)
    monkeypatch.setattr(api, "REQUIRE_APPROVAL", False)
    monkeypatch.setattr(api, "_vercel_mode", lambda: True)
    monkeypatch.setattr(vercel_runner, "supported", lambda _workspace: True)
    monkeypatch.setattr(vercel_runner, "deploy", deploy)
    monkeypatch.setattr(api.remote_runner, "available", lambda: True)
    monkeypatch.setattr(api.remote_runner, "start", start)

    try:
        payload = asyncio.run(api.serve_start("r1"))
        assert payload["kind"] == "cloud"
        assert payload["url"] == "https://cortex-runner.test/runs/r1/token/"
        assert "r1" in api._remote_runs  # noqa: SLF001
    finally:
        api._remote_runs.pop("r1", None)  # noqa: SLF001


def test_a_failed_vercel_deployment_without_a_runner_names_its_reason(tmp_path: Path, monkeypatch) -> None:
    import asyncio

    from ai_harness import main as api

    failed = vercel_runner.VercelService(run_id="r1", project="cortex-r1")
    failed.state = "failed"
    failed.detail = "Vercel exited 1: Error: Not authorized"

    async def deploy(_run_id: str, _workspace: Path) -> vercel_runner.VercelService:
        return failed

    monkeypatch.setattr(api, "_run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(api.harness, "get", lambda _run_id: None)
    monkeypatch.setattr(api, "REQUIRE_APPROVAL", False)
    monkeypatch.setattr(api, "_vercel_mode", lambda: True)
    monkeypatch.setattr(vercel_runner, "supported", lambda _workspace: True)
    monkeypatch.setattr(vercel_runner, "deploy", deploy)
    monkeypatch.setattr(api.remote_runner, "available", lambda: False)

    try:
        payload = asyncio.run(api.serve_start("r1"))
        assert payload["state"] == "failed"
        assert "Not authorized" in payload["detail"]
    finally:
        api._vercel_runs.pop("r1", None)  # noqa: SLF001


@pytest.mark.skipif(
    os.getenv("HARNESS_LIVE_VERCEL") != "1" or shutil.which("vercel") is None,
    reason="live Vercel deployment test is opt-in (HARNESS_LIVE_VERCEL=1)",
)
def test_a_live_vercel_deployment_serves_and_is_removed(tmp_path: Path) -> None:
    import asyncio

    import httpx

    (tmp_path / "index.html").write_text(
        "<!doctype html><title>Vercel live</title><body>live vercel check</body>", encoding="utf-8"
    )

    async def run() -> None:
        service = await vercel_runner.deploy("live-vercel-check", tmp_path)
        try:
            assert service.state == "ready", service.logs[-20:]
            assert service.url
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                response = await client.get(service.url)
                for _ in range(6):
                    if response.status_code == 200:
                        break
                    await asyncio.sleep(1)
                    response = await client.get(service.url)
            assert response.status_code == 200
            assert "live vercel check" in response.text
        finally:
            await service.stop()

    asyncio.run(run())
