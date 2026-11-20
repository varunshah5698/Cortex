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
