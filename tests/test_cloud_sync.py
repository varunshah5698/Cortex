"""The Convex mirror must never be able to break the run it mirrors.

The harness is the source of truth: it executes and it decides `done`. Convex is a durable
copy. These tests pin that ordering — an unconfigured mirror is inert, a failing mirror
swallows its own error, and the secret never appears in anything a client can read.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_harness.modules import cloud_sync  # noqa: E402


def test_an_unconfigured_mirror_is_inert(monkeypatch):
    monkeypatch.setattr(cloud_sync, "SITE_URL", "")
    monkeypatch.setattr(cloud_sync, "SECRET", "")

    assert cloud_sync.enabled() is False
    # Every call is a no-op rather than an exception.
    assert cloud_sync._post("/harness/event", {"runId": "r"}) is None


def test_a_url_without_a_secret_is_not_configured(monkeypatch):
    monkeypatch.setattr(cloud_sync, "SITE_URL", "https://example.convex.site")
    monkeypatch.setattr(cloud_sync, "SECRET", "")

    assert cloud_sync.enabled() is False


def test_a_failing_mirror_swallows_its_own_error(monkeypatch):
    """A Convex outage must not surface as a failed build."""
    monkeypatch.setattr(cloud_sync, "SITE_URL", "https://example.convex.site")
    monkeypatch.setattr(cloud_sync, "SECRET", "s3cret")

    def explode(*args, **kwargs):
        raise RuntimeError("network is down")

    monkeypatch.setattr(cloud_sync.httpx, "post", explode)

    # None of these may raise.
    cloud_sync.start("run-1", "build a page", "deep")
    cloud_sync.event("run-1", "plan", {"files": 3})
    cloud_sync.finish("run-1", "done", "ok")
    cloud_sync.lesson("a lesson")


def test_an_error_response_is_not_treated_as_success(monkeypatch):
    monkeypatch.setattr(cloud_sync, "SITE_URL", "https://example.convex.site")
    monkeypatch.setattr(cloud_sync, "SECRET", "s3cret")

    class Response:
        status_code = 401

        def json(self):
            return {"error": "unauthorized"}

    monkeypatch.setattr(cloud_sync.httpx, "post", lambda *a, **k: Response())

    assert cloud_sync._post("/harness/event", {"runId": "r"}) is None


def test_the_secret_is_sent_as_a_bearer_and_never_in_the_body(monkeypatch):
    monkeypatch.setattr(cloud_sync, "SITE_URL", "https://example.convex.site")
    monkeypatch.setattr(cloud_sync, "SECRET", "s3cret")

    seen = {}

    class Response:
        status_code = 200

        def json(self):
            return {"ok": True}

    def capture(url, json=None, headers=None, timeout=None):  # noqa: A002 - httpx's own name
        seen["url"] = url
        seen["json"] = json
        seen["headers"] = headers
        return Response()

    monkeypatch.setattr(cloud_sync.httpx, "post", capture)
    cloud_sync.event("run-1", "plan", {"files": 3})

    assert seen["headers"]["Authorization"] == "Bearer s3cret"
    assert "s3cret" not in str(seen["json"])
    assert seen["url"].endswith("/harness/event")


def test_status_reports_configuredness_without_the_secret(monkeypatch):
    monkeypatch.setattr(cloud_sync, "SITE_URL", "https://example.convex.site")
    monkeypatch.setattr(cloud_sync, "SECRET", "s3cret")

    status = cloud_sync.status()

    assert status == {"enabled": True, "site": "https://example.convex.site", "secret_set": True}
    assert "s3cret" not in str(status)
