"""The deployed-instance guard: public same-origin plus one Basic auth password.

A local checkout keeps the original loopback rules. Render adds a public origin, so the
UI is same-origin with `/api` without being loopback, and a password keeps every other
visitor out.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from ai_harness import main  # noqa: E402


def test_public_origin_is_same_origin_when_the_host_matches(monkeypatch) -> None:
    monkeypatch.setattr(main, "PUBLIC_HOST", "cortex.onrender.com")
    monkeypatch.setattr(main, "ORIGINS", ["http://127.0.0.1:5178"])

    assert main._public_host("cortex.onrender.com")
    assert main._origin_allowed("https://cortex.onrender.com", "cortex.onrender.com")
    assert not main._origin_allowed("https://evil.example", "cortex.onrender.com")
    assert not main._origin_allowed("https://cortex.onrender.com", "evil.example")


def test_basic_auth_blocks_everything_but_health(monkeypatch) -> None:
    monkeypatch.setattr(main, "BASIC_AUTH", "secret")
    client = TestClient(main.app)

    assert client.get("/api/health").status_code == 200
    blocked = client.get("/api/runs")
    assert blocked.status_code == 401
    assert "Basic" in blocked.headers["www-authenticate"]

    token = base64.b64encode(b"cortex:secret").decode()
    headers = {"Authorization": f"Basic {token}", "X-Harness-Token": main.api_token()}
    allowed = client.get("/api/runs", headers=headers)
    assert allowed.status_code == 200, allowed.text

    wrong = base64.b64encode(b"cortex:wrong").decode()
    assert client.get("/api/runs", headers={**headers, "Authorization": f"Basic {wrong}"}).status_code == 401


def test_basic_auth_accepts_a_bare_password_as_the_cortex_user(monkeypatch) -> None:
    monkeypatch.setattr(main, "BASIC_AUTH", "only-a-password")
    client = TestClient(main.app)
    token = base64.b64encode(b"cortex:only-a-password").decode()

    headers = {"Authorization": f"Basic {token}", "X-Harness-Token": main.api_token()}
    assert client.get("/api/runs", headers=headers).status_code == 200
