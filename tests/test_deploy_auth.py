"""The deployed-instance guard: public same-origin, no Basic auth, token for scripts.

The public Render site has no website password. A browser on the service's own origin
may call `/api`; a foreign page is rejected, and a non-browser client must present
`X-Harness-Token`. The login screen is gone, so the public UI is the only way in.
"""

from __future__ import annotations

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


def test_public_site_is_not_basic_auth_gated() -> None:
    client = TestClient(main.app)

    assert client.get("/api/health").status_code == 200
    blocked = client.get("/api/runs")
    assert blocked.status_code == 403
    assert "www-authenticate" not in blocked.headers

    allowed = client.get("/api/runs", headers={"X-Harness-Token": main.api_token()})
    assert allowed.status_code == 200, allowed.text
