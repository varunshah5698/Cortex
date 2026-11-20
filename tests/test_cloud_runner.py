"""The per-run cloud runner: authenticated submission, isolated workspace, public proxy."""

from __future__ import annotations

import io
import sys
import tarfile
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_harness import cloud_runner  # noqa: E402
from ai_harness.modules import cloud_runner as client  # noqa: E402
from ai_harness.modules import runner  # noqa: E402


def _archive(files: dict[str, str], extra: str | None = None) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as handle:
        for name, body in files.items():
            data = body.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            handle.addfile(info, io.BytesIO(data))
        if extra:
            data = b"escape"
            info = tarfile.TarInfo(extra)
            info.size = len(data)
            handle.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def test_a_static_run_is_accepted_served_and_removed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(cloud_runner, "TOKEN", "test-token")
    monkeypatch.setattr(cloud_runner, "WORKSPACE", tmp_path / "runs")
    monkeypatch.setattr(cloud_runner, "PUBLIC", "")
    cloud_runner.WORKSPACE.mkdir(parents=True, exist_ok=True)
    cloud_runner.RUNS.clear()
    runner.SERVICES.clear()

    with TestClient(cloud_runner.app) as api:
        response = api.post(
            "/runs",
            content=_archive({"index.html": "<!doctype html><title>Cloud</title><body>Hello cloud</body>"}),
            headers={"x-runner-token": "test-token", "x-run-id": "cloud-test"},
        )
        assert response.status_code == 202, response.text
        payload = response.json()
        assert payload["state"] in ("starting", "detecting", "ready")

        deadline = time.time() + 15
        status = payload
        while time.time() < deadline:
            status = api.get("/runs/cloud-test", headers={"x-runner-token": "test-token"}).json()
            if status["state"] in ("ready", "failed"):
                break
            time.sleep(0.2)

        assert status["state"] == "ready", status
        public = api.get(f"/runs/cloud-test/{payload['secret']}/")
        assert public.status_code == 200, public.text
        assert "Hello cloud" in public.text

        stopped = api.delete("/runs/cloud-test", headers={"x-runner-token": "test-token"})
        assert stopped.status_code == 200
        assert not (tmp_path / "runs" / "cloud-test").exists()

    assert "cloud-test" not in runner.SERVICES


def test_bad_token_and_escaping_archive_are_refused(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(cloud_runner, "TOKEN", "test-token")
    monkeypatch.setattr(cloud_runner, "WORKSPACE", tmp_path / "runs")
    cloud_runner.WORKSPACE.mkdir(parents=True, exist_ok=True)
    with TestClient(cloud_runner.app) as api:
        assert api.post("/runs", content=b"x", headers={"x-runner-token": "wrong"}).status_code == 401
        escaped = api.post(
            "/runs",
            content=_archive({"index.html": "ok"}, extra="../outside.txt"),
            headers={"x-runner-token": "test-token", "x-run-id": "escape"},
        )
        assert escaped.status_code == 400, escaped.text
        assert not (tmp_path / "outside.txt").exists()


def test_the_client_archive_skips_dependency_trees(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("ok", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.js").write_text("junk", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=1", encoding="utf-8")

    body = client.archive(tmp_path)
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as handle:
        names = set(handle.getnames())

    assert "index.html" in names
    assert ".env" in names  # the run's own files are uploaded; no host keys exist in a run
    assert not any(name.startswith("node_modules") for name in names)


def test_a_remote_service_reads_like_a_local_one() -> None:
    remote = client.RemoteService(run_id="r1", url="https://runner.example/runs/r1/tok/")
    remote.apply(
        {
            "state": "ready",
            "detail": "serving",
            "url": "https://runner.example/runs/r1/tok/",
            "logs": ["ready"],
            "exit_code": None,
        }
    )
    payload = remote.as_dict()
    assert payload["remote"] is True
    assert payload["url"].endswith("/runs/r1/tok/")
    assert payload["state"] == "ready"
    assert payload["port"] is None
    assert payload["logs"] == ["ready"]


@pytest.mark.skipif(not client.available(), reason="no cloud runner configured in this environment")
def test_the_configured_cloud_runner_answers_health() -> None:
    import httpx

    response = httpx.get(f"{client.CLOUD_RUNNER_URL}/health", timeout=30)
    assert response.status_code == 200
