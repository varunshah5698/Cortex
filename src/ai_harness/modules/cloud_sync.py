"""Push a run's facts to Convex, without ever becoming a dependency of the run.

The harness is the source of truth: it executes, it verifies, it decides `done`. Convex is
a durable mirror so a run can be read after the process is gone and from another machine.

That ordering is the whole design. Every call here is best-effort and time-bounded: a
Convex outage, a wrong secret or a slow network must never fail a build, because the build
already succeeded on disk. `enabled()` is false unless both the site URL and the shared
secret are configured, so an unconfigured checkout behaves exactly as it did before.

The secret is read from the environment (`.env` locally, the service's env on Render) and
is never logged, never returned to a client and never written into a run workspace.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

SITE_URL = os.getenv("CONVEX_SITE_URL", "").strip().rstrip("/")
SECRET = os.getenv("CONVEX_HARNESS_SECRET", "").strip()


def _timeout() -> float:
    """A malformed timeout must not stop the harness from importing.

    This is read at import time because `orchestrator` imports this module, so a bare
    `float(...)` here turned a typo in an env var into a process that will not start —
    the mirror breaking the thing it is only supposed to observe.
    """
    try:
        return max(0.5, float(os.getenv("CONVEX_SYNC_TIMEOUT", "4")))
    except (TypeError, ValueError):
        return 4.0


TIMEOUT = _timeout()


def enabled() -> bool:
    """Both halves must be present. A URL with no secret is not a configured mirror."""
    return bool(SITE_URL and SECRET)


def _post(path: str, body: dict[str, Any]) -> dict | None:
    if not enabled():
        return None
    try:
        response = httpx.post(
            f"{SITE_URL}{path}",
            json=body,
            headers={"Authorization": f"Bearer {SECRET}"},
            timeout=TIMEOUT,
        )
        if response.status_code >= 400:
            return None
        return response.json()
    except Exception:  # noqa: BLE001 - a mirror must never break the thing it mirrors
        return None


def start(run_id: str, prompt: str, depth: str) -> None:
    _post("/harness/event", {"runId": run_id, "type": "start", "payload": {"prompt": prompt, "depth": depth}})


def event(run_id: str, kind: str, payload: dict[str, Any] | None = None) -> None:
    _post("/harness/event", {"runId": run_id, "type": kind, "payload": payload or {}})


def finish(run_id: str, status: str, message: str = "", errors: int = 0) -> None:
    _post("/harness/finish", {"runId": run_id, "status": status, "message": message, "errors": errors})


def lesson(text: str, code: str = "", file: str = "", run_id: str = "") -> None:
    if not text.strip():
        return
    _post("/harness/lesson", {"text": text, "code": code, "file": file, "runId": run_id})


def status() -> dict[str, Any]:
    """What the health endpoint can honestly report about the mirror."""
    return {
        "enabled": enabled(),
        "site": SITE_URL or None,
        "secret_set": bool(SECRET),
    }
