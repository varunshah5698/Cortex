"""Client for the per-run cloud runner.

The harness sends one workspace to a separately deployed runner, gets back a public URL,
and polls it like a local service. The runner owns the install/build/serve process and
the public proxy; the harness never exposes its own `/api` or its keys.

A run is a tar.gz because that keeps the contract one request and no shared filesystem.
The archive excludes dependency and VCS trees; the runner installs from the manifest the
run wrote, which is the same thing the local preview does.
"""

from __future__ import annotations

import io
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from ..config.settings import CLOUD_RUNNER_TIMEOUT, CLOUD_RUNNER_TOKEN, CLOUD_RUNNER_URL

EXCLUDE_DIRS = {"node_modules", ".venv", "venv", ".git", "__pycache__", ".pytest_cache", ".ruff_cache"}
EXCLUDE_FILES = {".DS_Store"}


class CloudRunnerError(RuntimeError):
    """The runner refused or failed the submission."""


def available() -> bool:
    return bool(CLOUD_RUNNER_URL and CLOUD_RUNNER_TOKEN)


def _headers() -> dict[str, str]:
    return {"x-runner-token": CLOUD_RUNNER_TOKEN}


def archive(workspace: Path, limit: int = 25 * 1024 * 1024) -> bytes:
    """Tar the run without dependency trees, symlinks or harness-only files."""
    buffer = io.BytesIO()
    total = 0
    with tarfile.open(fileobj=buffer, mode="w:gz") as handle:
        for path in sorted(workspace.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(workspace)
            if any(part in EXCLUDE_DIRS for part in relative.parts) or path.name in EXCLUDE_FILES:
                continue
            if path.name.startswith(".harness-"):
                continue
            total += path.stat().st_size
            if total > limit:
                raise CloudRunnerError(f"the run is larger than the {limit // (1024 * 1024)} MB runner limit")
            handle.add(str(path), arcname=str(relative))
    return buffer.getvalue()


@dataclass
class RemoteService:
    """A duck-typed service payload: the API and UI read the same fields as a local one."""

    run_id: str
    url: str
    state: str = "starting"
    detail: str = "the cloud runner is extracting and starting the run"
    logs: list[str] = field(default_factory=list)
    exit_code: int | None = None
    expires_in: int = 0
    remote: bool = True

    @property
    def port(self) -> None:
        return None

    @property
    def token(self) -> None:
        return None

    @property
    def prefix(self) -> str:
        return ""

    @property
    def pending(self) -> list[str]:
        return []

    @property
    def detail_lines(self) -> list[str]:
        return []

    @property
    def sandbox(self) -> str:
        return "the cloud runner's host; its own log says what containment it has"

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "state": self.state,
            "kind": "cloud",
            "detail": self.detail,
            "url": self.url,
            "port": None,
            "token": None,
            "exit_code": self.exit_code,
            "pending": [],
            "detail_lines": [],
            "sandbox": self.sandbox,
            "browser": None,
            "remote": True,
            "expires_in": self.expires_in,
            "logs": self.logs[-400:],
        }

    def apply(self, payload: dict) -> RemoteService:
        self.state = str(payload.get("state") or self.state)
        self.detail = str(payload.get("detail") or self.detail)
        self.url = str(payload.get("url") or self.url)
        self.logs = [str(line) for line in (payload.get("logs") or [])]
        self.exit_code = payload.get("exit_code")
        self.expires_in = int(payload.get("expires_in") or 0)
        return self

    async def refresh(self) -> RemoteService:
        async with httpx.AsyncClient(timeout=CLOUD_RUNNER_TIMEOUT) as client:
            response = await client.get(f"{CLOUD_RUNNER_URL}/runs/{self.run_id}", headers=_headers())
        if response.status_code == 404:
            self.state = "stopped"
            self.detail = "the cloud runner no longer has this run"
            return self
        if response.status_code >= 400:
            raise CloudRunnerError(f"the cloud runner answered {response.status_code}: {response.text[:200]}")
        return self.apply(response.json())

    async def stop(self) -> None:
        try:
            async with httpx.AsyncClient(timeout=CLOUD_RUNNER_TIMEOUT) as client:
                await client.delete(f"{CLOUD_RUNNER_URL}/runs/{self.run_id}", headers=_headers())
        finally:
            self.state = "stopped"
            self.detail = "stopped on the cloud runner"


async def start(run_id: str, workspace: Path) -> RemoteService:
    if not available():
        raise CloudRunnerError("no cloud runner is configured (HARNESS_CLOUD_RUNNER_URL/TOKEN)")
    body = archive(workspace)
    headers = {**_headers(), "content-type": "application/gzip", "x-run-id": run_id}
    try:
        async with httpx.AsyncClient(timeout=CLOUD_RUNNER_TIMEOUT) as client:
            response = await client.post(f"{CLOUD_RUNNER_URL}/runs", content=body, headers=headers)
    except httpx.HTTPError as exc:
        raise CloudRunnerError(f"the cloud runner did not answer: {type(exc).__name__}: {exc}") from exc
    if response.status_code >= 400:
        raise CloudRunnerError(f"the cloud runner refused the run ({response.status_code}): {response.text[:300]}")
    payload = response.json()
    remote = RemoteService(run_id=run_id, url=str(payload.get("url") or ""))
    return remote.apply(payload)


async def status(run_id: str) -> RemoteService | None:
    if not available():
        return None
    remote = RemoteService(run_id=run_id, url="")
    try:
        return await remote.refresh()
    except CloudRunnerError:
        return None


async def stop(run_id: str) -> None:
    if not available():
        return
    async with httpx.AsyncClient(timeout=CLOUD_RUNNER_TIMEOUT) as client:
        await client.delete(f"{CLOUD_RUNNER_URL}/runs/{run_id}", headers=_headers())


async def stop_all(services: dict[str, RemoteService]) -> None:
    for run_id in list(services):
        try:
            await services[run_id].stop()
        except Exception:  # noqa: BLE001 - shutdown must not stop on one dead runner
            pass
        services.pop(run_id, None)


__all__ = ["CloudRunnerError", "RemoteService", "archive", "available", "start", "status", "stop", "stop_all"]
