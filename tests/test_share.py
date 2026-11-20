"""The public share is one run, one token, and no `/api`."""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_harness import share  # noqa: E402
from ai_harness.config.settings import TUNNEL_COMMAND  # noqa: E402
from ai_harness.modules import cloud, runner  # noqa: E402


def test_a_public_app_serves_only_the_token_path(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "index.html").write_text("<!doctype html><body>hello share</body>", encoding="utf-8")
    monkeypatch.setattr(share, "run_dir", lambda _run_id: tmp_path)
    client = TestClient(share.create_app("run1", "tok123"))

    assert client.get("/", follow_redirects=False).status_code == 307
    assert client.get("/tok123/").status_code == 200
    assert "hello share" in client.get("/tok123/").text
    assert client.get("/wrong/").status_code == 404
    assert client.get("/tok123/%2e%2e/secret").status_code == 404


def test_the_url_parser_ignores_the_providers_own_links() -> None:
    text = (
        "INF https://www.cloudflare.com/website-terms/ https://github.com/cloudflare/cloudflared "
        "https://quiet-forest.trycloudflare.com"
    )
    assert cloud._public_url(text) == "https://quiet-forest.trycloudflare.com"  # noqa: SLF001
    assert cloud._public_url("https://www.cloudflare.com/website-terms/") is None  # noqa: SLF001


@pytest.mark.skipif(
    not shutil.which("cloudflared") and not TUNNEL_COMMAND,
    reason="no tunnel provider installed",
)
def test_a_share_waits_for_approval_before_it_publishes(tmp_path: Path, monkeypatch) -> None:
    run_id = "share-approval"
    monkeypatch.setattr(cloud, "REQUIRE_APPROVAL", True)
    cloud.SHARES.clear()
    service = runner.Service(run_id=run_id, workspace=tmp_path, state="ready", kind="static", port=1, root=tmp_path)
    runner.SERVICES[run_id] = service
    try:
        shared = asyncio.run(cloud.start(run_id, approved=False))
        assert shared.state == "needs_approval"
        assert shared.process is None
        assert shared.url is None
        assert shared.command
    finally:
        asyncio.run(cloud.stop(run_id, reason="test"))
        runner.SERVICES.pop(run_id, None)
