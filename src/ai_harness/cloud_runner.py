"""A per-run cloud runner: receive one workspace, install it, serve it, proxy it.

This is the other half of the harness. The local runner starts a process on this machine;
this service is deployed once, in the cloud, and accepts one run at a time over an
authenticated submission. The run's files are sent as a tar.gz, extracted into a private
directory, installed and served by the same `modules/runner.py` the local preview uses,
and reached through `/runs/<id>/<secret>/…` on this service's own public origin.

It is deliberately not the harness API. There is no `/api`, no roster, no provider keys;
the only credential is `HARNESS_RUNNER_TOKEN`, and the only public path carries a per-run
secret. A run is bounded by `HARNESS_RUNNER_TTL`, removed on stop, and a free instance
holds one run at a time rather than pretending to be a fleet.
"""

from __future__ import annotations

import asyncio
import io
import os
import secrets
import shutil
import tarfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .modules import runner
from .paths import RUN_ID

TOKEN = os.getenv("HARNESS_RUNNER_TOKEN", "").strip()
WORKSPACE = Path(os.getenv("HARNESS_RUNNER_WORKSPACE", "/var/tmp/cortex-runner/runs"))
PUBLIC = os.getenv("HARNESS_RUNNER_PUBLIC_URL", "").strip().rstrip("/") or os.getenv(
    "RENDER_EXTERNAL_URL", ""
).strip().rstrip("/")
TTL = max(60.0, float(os.getenv("HARNESS_RUNNER_TTL", "900")))
MAX_RUNS = max(1, int(os.getenv("HARNESS_RUNNER_MAX_RUNS", "1")))
MAX_BODY = max(1_000_000, int(os.getenv("HARNESS_RUNNER_MAX_BYTES", str(25 * 1024 * 1024))))
MAX_FILES = max(10, int(os.getenv("HARNESS_RUNNER_MAX_FILES", "2000")))
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "transfer-encoding",
    "upgrade",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "host",
    "cookie",
    "authorization",
    "content-encoding",
    "content-length",
}

app = FastAPI(title="Cortex cloud runner", docs_url=None, redoc_url=None, openapi_url=None)
WORKSPACE.mkdir(parents=True, exist_ok=True)


@dataclass
class CloudRun:
    id: str
    workspace: Path
    secret: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    service: runner.Service | None = None
    task: asyncio.Task | None = None
    timer: asyncio.Task | None = None
    created: float = field(default_factory=time.time)

    @property
    def state(self) -> str:
        return self.service.state if self.service is not None else "starting"

    @property
    def detail(self) -> str:
        return self.service.detail if self.service is not None else "extracting the run"

    @property
    def url_path(self) -> str:
        return f"/runs/{self.id}/{self.secret}/"

    def public_url(self, base: str) -> str:
        return f"{base}{self.url_path}"

    def as_dict(self, base: str) -> dict:
        service = self.service
        return {
            "id": self.id,
            "state": self.state,
            "detail": self.detail,
            "url": self.public_url(base),
            "secret": self.secret,
            "logs": (service.logs[-120:] if service is not None else []),
            "exit_code": (service.exit_code if service is not None else None),
            "expires_in": max(0, int(TTL - (time.time() - self.created))),
        }


RUNS: dict[str, CloudRun] = {}


def _base(request: Request) -> str:
    return PUBLIC or str(request.base_url).rstrip("/")


def _token(request: Request) -> None:
    if not TOKEN:
        raise HTTPException(503, "this runner has no HARNESS_RUNNER_TOKEN and will not accept work")
    supplied = request.headers.get("x-runner-token", "")
    if not secrets.compare_digest(supplied, TOKEN):
        raise HTTPException(401, "runner token is wrong")


def _active() -> int:
    return sum(1 for item in RUNS.values() if item.state not in ("stopped", "failed"))


def _extract(body: bytes, target: Path) -> None:
    """Extract a flat tar.gz, refusing links, escapes and oversized trees."""
    total = 0
    count = 0
    try:
        archive = tarfile.open(fileobj=io.BytesIO(body), mode="r:gz")
    except tarfile.TarError as exc:
        raise HTTPException(400, f"the workspace archive is not readable: {exc}") from None
    with archive:
        for member in archive:
            raw = member.name.replace("\\", "/")
            if not raw or raw.startswith("/") or ".." in Path(raw).parts:
                raise HTTPException(400, "the archive contains an unsafe path")
            name = raw[2:] if raw.startswith("./") else raw
            if not name:
                raise HTTPException(400, "the archive contains an empty path")
            if member.issym() or member.islnk() or member.isdev():
                raise HTTPException(400, "the archive contains a link or device")
            count += 1
            if count > MAX_FILES:
                raise HTTPException(413, f"the archive has more than {MAX_FILES} entries")
            destination = (target / name).resolve()
            if target.resolve() not in destination.parents and destination != target.resolve():
                raise HTTPException(400, "the archive escapes the run directory")
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                continue
            total += member.size
            if total > MAX_BODY:
                raise HTTPException(413, f"the extracted tree is larger than {MAX_BODY} bytes")
            destination.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                continue
            with source, destination.open("wb") as handle:
                shutil.copyfileobj(source, handle)


async def _stop(run: CloudRun, reason: str) -> None:
    current = RUNS.pop(run.id, None)
    if current is None:
        return
    if current.timer is not None and not current.timer.done():
        current.timer.cancel()
    if current.service is not None:
        await current.service.stop()
    if current.task is not None and not current.task.done():
        current.task.cancel()
        await asyncio.gather(current.task, return_exceptions=True)
    shutil.rmtree(current.workspace, ignore_errors=True)
    runner.SERVICES.pop(run.id, None)
    del reason  # kept in the signature for a future event log


async def _expire(run: CloudRun) -> None:
    await asyncio.sleep(TTL)
    await _stop(run, "ttl")


@app.get("/health")
async def health() -> dict:
    return {
        "ok": True,
        "active": _active(),
        "capacity": MAX_RUNS,
        "configured": bool(TOKEN),
        "ttl": int(TTL),
        "workspace": str(WORKSPACE),
    }


@app.post("/runs")
async def create_run(request: Request) -> JSONResponse:
    _token(request)
    if _active() >= MAX_RUNS:
        raise HTTPException(429, "the cloud runner is full; stop a run first")
    run_id = request.headers.get("x-run-id", "").strip() or uuid.uuid4().hex[:12]
    if not RUN_ID.match(run_id):
        raise HTTPException(400, "invalid run id")
    if run_id in RUNS:
        raise HTTPException(409, "that run id is already here")
    body = await request.body()
    if not body or len(body) > MAX_BODY:
        raise HTTPException(413, f"the archive must be between 1 and {MAX_BODY} bytes")
    workspace = WORKSPACE / run_id
    if workspace.exists():
        shutil.rmtree(workspace, ignore_errors=True)
    workspace.mkdir(parents=True, exist_ok=True)
    try:
        _extract(body, workspace)
    except HTTPException:
        shutil.rmtree(workspace, ignore_errors=True)
        raise

    run = CloudRun(id=run_id, workspace=workspace)
    service = runner.Service(run_id=run_id, workspace=workspace, approved=True)
    service.state = "detecting"
    service.detail = "the runner is looking at what the team wrote"
    run.service = service
    runner.SERVICES[run_id] = service
    RUNS[run_id] = run
    run.task = asyncio.create_task(service.start(), name=f"cloud-run-{run_id}")
    run.timer = asyncio.create_task(_expire(run), name=f"cloud-ttl-{run_id}")
    return JSONResponse(run.as_dict(_base(request)), status_code=202)


@app.get("/runs/{run_id}")
async def run_status(run_id: str, request: Request) -> dict:
    _token(request)
    run = RUNS.get(run_id)
    if run is None:
        raise HTTPException(404, "unknown run")
    return run.as_dict(_base(request))


@app.delete("/runs/{run_id}")
async def run_delete(run_id: str, request: Request) -> dict:
    _token(request)
    run = RUNS.get(run_id)
    if run is None:
        raise HTTPException(404, "unknown run")
    await _stop(run, "stopped")
    return {"id": run_id, "state": "stopped"}


@app.api_route(
    "/runs/{run_id}/{secret}/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
async def run_proxy(run_id: str, secret: str, path: str, request: Request):
    run = RUNS.get(run_id)
    if run is None or not secrets.compare_digest(secret, run.secret):
        raise HTTPException(404, "unknown run")
    service = run.service
    if service is None or service.state != "ready" or not service.port:
        raise HTTPException(409, f"the run is {run.state}, not ready")
    target = f"http://127.0.0.1:{service.port}{service.prefix}/{path}"
    if request.url.query:
        target = f"{target}?{request.url.query}"
    forward = {key: value for key, value in request.headers.items() if key.lower() not in HOP_BY_HOP}
    client = httpx.AsyncClient(timeout=None)
    try:
        upstream = await client.send(
            client.build_request(request.method, target, headers=forward, content=await request.body()),
            stream=True,
        )
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(502, f"the run's server did not answer: {exc}") from None

    async def body():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    headers = {
        "cache-control": "no-store",
        "x-content-type-options": "nosniff",
        **{k: v for k, v in upstream.headers.items() if k.lower() not in HOP_BY_HOP},
    }
    return StreamingResponse(
        body(),
        status_code=upstream.status_code,
        headers=headers,
        media_type=upstream.headers.get("content-type"),
    )


@app.on_event("shutdown")
async def shutdown() -> None:
    for run in list(RUNS.values()):
        await _stop(run, "shutdown")


__all__ = ["app"]
