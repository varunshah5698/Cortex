"""FastAPI surface: runs, SSE stream, files, metrics, provider checks.

Generated artifacts are *not* served here — see `preview.py`, which runs on its own
loopback origin so model-written code is never same-origin with `/api`.

Two rules this module enforces rather than documents:

* The browser never holds a provider key. Keys are written once to the server-side
  roster (`modules/roster.py`, a 0600 file), merged in when a run starts, and never
  returned — `/api/roster` reports `has_key`, not the key.
* Nothing on `/api` answers a foreign page. A browser request must come from an allowed
  origin, and a non-browser client must present the harness token.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import paths, preview
from .config.settings import (
    AGENT_LOOP,
    AGENT_STEPS,
    CACHE_ENABLED,
    DIST_DIR,
    KEYS_FILE,
    MAX_RUN_TOKENS,
    MAX_RUN_USD,
    ORIGINS,
    RETRY_ATTEMPTS,
    ROSTER_FILE,
    api_token,
    provider_key,
)
from .core.harness import Harness, HarnessBusy
from .core.state import RunState, now_iso
from .models import Specialist
from .modules import budget, llm, permissions, probe, receipt, runner
from .modules.cache import CACHE
from .modules.roster import RosterStore
from .utils.validators import UnsafePath

app = FastAPI(title="Cortex Harness", version="0.3.0")

# Only the UI's own origins may call the API from a browser. A wildcard let any page on
# the web drive this harness — and spend whatever keys it held — through the user's tab.
app.add_middleware(
    CORSMiddleware,
    allow_origins=ORIGINS,
    allow_methods=["GET", "POST", "PUT", "OPTIONS"],
    allow_headers=["Content-Type", "X-Harness-Token"],
)

harness = Harness()
ROSTER = RosterStore(ROSTER_FILE, KEYS_FILE)

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def _host_port(value: str) -> tuple[str, int | None] | None:
    """`(hostname, port)` for `host[:port]` or `scheme://host[:port]`, or None.

    `.port` is read deliberately: `urlparse` reports the hostname of
    `127.0.0.1:8000.evil.example` as `127.0.0.1` and only raises when the port is
    fetched, so a helper that skipped this would accept an attacker-shaped host.
    """
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        parsed = urlparse(raw if "//" in raw else "//" + raw)
        port = parsed.port  # raises ValueError on a non-numeric port
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    return (host, port) if host else None


def _loopback_host(value: str) -> bool:
    """True for a literal loopback host:port. A name is not trusted here.

    DNS rebinding makes an attacker's name resolve to 127.0.0.1, so the browser's
    `Origin` and `Host` then agree. Requiring the host itself to *be* loopback is what
    separates "this process" from "a name that points at it today".
    """
    parts = _host_port(value)
    return bool(parts and parts[0] in LOOPBACK)


def _origin_allowed(origin: str, host: str) -> bool:
    clean = (origin or "").strip().rstrip("/")
    if not clean:
        return False
    if clean in ORIGINS:
        return True
    # Same-origin only when both sides are loopback and agree on host *and* port;
    # equal netlocs alone would accept a rebound name that resolves to 127.0.0.1.
    origin_parts, host_parts = _host_port(clean), _host_port(host)
    if not origin_parts or not host_parts:
        return False
    if origin_parts[0] not in LOOPBACK or host_parts[0] not in LOOPBACK:
        return False
    return origin_parts == host_parts


@app.middleware("http")
async def guard(request: Request, call_next):
    """The UI, or the token. Nothing else reaches `/api`.

    A page on another origin always sends `Origin`, and `Sec-Fetch-Site` is a forbidden
    header a script cannot forge, so the two together tell our UI from some other tab.
    Non-browser clients (curl, a script) carry neither and must present the token.
    """
    path = request.url.path
    if path == "/api/health" or not path.startswith("/api"):
        return await call_next(request)

    host = request.headers.get("host", "")
    host_ok = _loopback_host(host)

    origin = request.headers.get("origin", "")
    if origin:
        if not _origin_allowed(origin, host):
            return JSONResponse({"detail": "origin not allowed"}, status_code=403)
        return await call_next(request)

    # `Sec-Fetch-Site` is unforgeable, but a rebound page talking to its own origin also
    # reports `same-origin`, so the host must be loopback too.
    if host_ok and request.headers.get("sec-fetch-site", "") == "same-origin":
        return await call_next(request)

    supplied = request.headers.get("x-harness-token", "")
    if not supplied or not secrets.compare_digest(supplied, api_token()):
        return JSONResponse({"detail": "missing or invalid harness token"}, status_code=403)
    return await call_next(request)


def _run_dir(run_id: str) -> Path:
    """Resolve a run id under RUNS_ROOT, refusing anything that is not one path segment."""
    try:
        return paths.run_dir(run_id)
    except UnsafePath:
        raise HTTPException(404, "unknown run") from None


def _contained(workspace: Path, path: str) -> Path:
    """Resolve `path` inside `workspace` by real path, not by string prefix."""
    try:
        return paths.contained(workspace, path)
    except UnsafePath:
        raise HTTPException(404, "file not found") from None


def _record_build(run: RunState, service: runner.Service) -> None:
    """Write what the preview actually did into the run and its ledger.

    The pipeline verifies and never compiles, so a `done` run says `previewed: false`,
    `build: null` until this is called. Serving is the only thing that can change that
    answer, and it changes it in both places the UI reads: the streamed `done`/metrics and
    `.harness-metrics.json`, which is what a reload and the metrics page show.
    """
    payload = service.as_dict()
    run.previewed = True
    run.build = {
        "state": payload["state"],
        "kind": payload["kind"],
        "detail": payload["detail"],
        "at": now_iso(),
    }
    run.save_metrics()


def _service_payload(service: runner.Service | None, run_id: str = "") -> dict:
    if service is None:
        return {
            "run_id": run_id,
            "state": "idle",
            "kind": "none",
            "detail": "not started",
            "url": None,
            "port": None,
            "exit_code": None,
            "pending": [],
            "detail_lines": [],
            "logs": [],
        }
    return service.as_dict()


class SpecialistIn(BaseModel):
    """A specialist as the browser knows it. The key is deliberately not part of this."""

    id: str
    name: str
    role: str = "general"
    kind: str = "openai"
    baseUrl: str = ""
    model: str = ""


class RosterEntry(SpecialistIn):
    """A key may be written once; it is stored server-side and never read back.

    A blank `apiKey` means "unchanged", so deleting the stored key is an explicit
    request: `clearKey` on an entry whose key is left blank.
    """

    apiKey: str = ""
    clearKey: bool = False


class RosterIn(BaseModel):
    specialists: list[RosterEntry] = Field(default_factory=list)
    # Additive by default: a browser that starts a run must not be able to delete the
    # specialists it does not know about. Only the workshop's explicit save replaces.
    replace: bool = False


class RunRequest(BaseModel):
    prompt: str
    # Preferred: ids resolved against the server-side roster. `specialists` stays for
    # CLI callers; keys are attached server-side in either case.
    specialist_ids: list[str] = Field(default_factory=list)
    specialists: list[SpecialistIn] = Field(default_factory=list)
    depth: str = "deep"


class ResumeRequest(BaseModel):
    """Resume keeps the run's plan and files; only the team can change."""

    specialist_ids: list[str] = Field(default_factory=list)
    depth: str = ""


class ProviderCheck(BaseModel):
    """A test needs the endpoint and whatever key we can find, in that order:
    the one just typed, the stored one, then the env fallback."""

    id: str = ""
    baseUrl: str = ""
    kind: str = ""
    apiKey: str = ""


class ChatTurn(BaseModel):
    role: str = "user"
    content: str = ""


class ChatRequest(BaseModel):
    specialist_id: str = ""
    # `Optional[...]`, not `X | None`: pydantic evaluates field annotations at import
    # time, and this project runs on Python 3.9 where the pipe form raises there.
    specialist: Optional[SpecialistIn] = None
    messages: list[ChatTurn] = Field(default_factory=list)
    # Reasoning budget: off answers directly, brief reasons then answers, deep also
    # checks its own direction. Each step is a model call.
    depth: str = "brief"


def _stored_key(spec_id: str, kind: str) -> str:
    stored = ROSTER.get(spec_id) if spec_id else None
    if stored is not None and stored.apiKey:
        return stored.apiKey
    return provider_key(kind)


def _specialist(item: SpecialistIn) -> Specialist:
    """Attach the server-held key; a run never receives one from the browser."""
    return Specialist(**item.model_dump(), apiKey=_stored_key(item.id, item.kind))


def _team(ids: list[str], inline: list[SpecialistIn]) -> list[Specialist]:
    """Resolve a run's roster by id, or fall back to descriptors for CLI callers."""
    if ids:
        missing = [spec_id for spec_id in ids if ROSTER.get(spec_id) is None]
        if missing:
            raise HTTPException(400, f"unknown specialist(s): {', '.join(missing)} — save the roster first")
        team = [spec for spec in (ROSTER.get(spec_id) for spec_id in ids) if spec and spec.ready]
    else:
        team = [_specialist(item) for item in inline if item.model]
    if not team:
        raise HTTPException(400, "no specialist with a model, base URL and key configured")
    return team


@app.get("/api/health")
async def health() -> dict:
    """Liveness plus the harness's own guardrails, so a UI can show them honestly."""
    return {
        "ok": True,
        "runs": len(harness.runs),
        "cache": CACHE.stats() if CACHE_ENABLED else {"enabled": False},
        "queue": {
            "running": harness.occupying(),
            "queued": harness.pending(),
            "concurrency": harness.concurrency,
        },
        "limits": {
            "max_tokens": MAX_RUN_TOKENS,
            "max_usd": MAX_RUN_USD,
            "retry_attempts": RETRY_ATTEMPTS,
            "agent_loop": {"enabled": AGENT_LOOP, "steps": AGENT_STEPS},
        },
        # What the dollar cap is actually computed from: a roster model with no row would
        # make `max_usd` a guess, so it is named here rather than discovered on an invoice.
        "pricing": budget.report(ROSTER.load()),
        # The policy that is actually loaded, so it can be inspected, not assumed.
        "permissions": permissions.table(),
    }


@app.get("/api/runs")
async def runs() -> dict:
    return {"runs": harness.history()}


@app.get("/api/roster")
async def roster_get() -> dict:
    """The roster with every key stripped: `has_key` only."""
    return {"specialists": ROSTER.public()}


@app.put("/api/roster")
async def roster_put(payload: RosterIn) -> dict:
    """Upsert the roster. A blank key means "unchanged"; `clearKey` deletes a stored one."""
    ROSTER.save(
        [Specialist(**item.model_dump(exclude={"clearKey"})) for item in payload.specialists],
        replace=payload.replace,
        clear_keys=[item.id for item in payload.specialists if item.clearKey],
    )
    return {"specialists": ROSTER.public()}


@app.post("/api/providers/test")
async def providers_test(payload: ProviderCheck) -> JSONResponse:
    base = (payload.baseUrl or "").strip().rstrip("/")
    if not base:
        return JSONResponse({"ok": False, "detail": "Base URL is required"})

    stored = ROSTER.get(payload.id) if payload.id else None
    kind = payload.kind or (stored.kind if stored else "custom")
    # A key typed but not yet saved wins, then the stored one, then the env fallback.
    key = payload.apiKey.strip() or _stored_key(payload.id, kind)
    headers = {"Authorization": f"Bearer {key}"} if key else {}

    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            response = await client.get(f"{base}/models", headers=headers)
        if response.status_code == 401:
            return JSONResponse({"ok": False, "detail": "401 — check the API key"})
        return JSONResponse({"ok": response.status_code < 500, "detail": f"HTTP {response.status_code}"})
    except httpx.HTTPError as exc:
        return JSONResponse({"ok": False, "detail": str(exc)[:160]})


@app.post("/api/chat")
async def chat(payload: ChatRequest) -> dict:
    """Start a chat as a harness run, then stream it through `/api/run/{id}/events`.

    Same registry, same event bus, same ledger as a build — so the chat page and the
    build room read one Python harness, and chat calls land in the metrics.
    """
    if payload.specialist_id:
        stored = ROSTER.get(payload.specialist_id)
        if stored is None:
            raise HTTPException(400, f"unknown specialist {payload.specialist_id!r} — save the roster first")
        spec = stored
    elif payload.specialist is not None:
        spec = _specialist(payload.specialist)
    else:
        raise HTTPException(400, "send specialist_id or a specialist")
    if not spec.model or not spec.baseUrl:
        raise HTTPException(400, f"{spec.name} has no model configured")

    messages = [item.model_dump() for item in payload.messages]
    if not any(m["role"] == "user" and m["content"].strip() for m in messages):
        raise HTTPException(400, "Nothing to answer — send a message first")

    try:
        run = harness.start_chat(spec, messages, payload.depth)
    except HarnessBusy as exc:
        raise HTTPException(429, str(exc), headers={"Retry-After": "5"}) from exc
    return {"run_id": run.run_id}


@app.post("/api/run")
async def create_run(payload: RunRequest) -> dict:
    team = _team(payload.specialist_ids, payload.specialists)
    try:
        run = harness.start(payload.prompt, team, payload.depth)
    except HarnessBusy as exc:
        raise HTTPException(429, str(exc), headers={"Retry-After": "5"}) from exc
    return {"run_id": run.run_id}


@app.post("/api/run/{run_id}/stop")
async def stop_run(run_id: str) -> dict:
    if not harness.get(run_id):
        raise HTTPException(404, "unknown run")
    return {"stopped": harness.stop(run_id)}


@app.get("/api/run/{run_id}/events")
async def run_events(run_id: str) -> StreamingResponse:
    run = harness.get(run_id)
    if not run:
        raise HTTPException(404, "unknown run")

    async def generator():
        queue: asyncio.Queue = asyncio.Queue(maxsize=10000)
        # Subscribe before snapshotting: every event is then either replayed or queued,
        # exactly once, even if the run emitted something between the two.
        run.subscribers.append(queue)
        try:
            for event in list(run.events):
                yield f"data: {json.dumps(event)}\n\n"
            if run.done:
                return
            while True:
                event = await queue.get()
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("type") in ("done", "error"):
                    break
        finally:
            if queue in run.subscribers:
                run.subscribers.remove(queue)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@app.get("/api/run/{run_id}/files")
async def run_files(run_id: str) -> dict:
    run = harness.get(run_id)
    # A run replayed from disk has its files on disk, not in memory.
    if run and run.files:
        return {"files": run.file_summary()}
    workspace = _run_dir(run_id)
    if not workspace.exists():
        raise HTTPException(404, "unknown run")
    items = []
    for path in sorted(workspace.rglob("*")):
        if path.is_file() and not path.name.startswith("."):
            items.append(
                {
                    "path": str(path.relative_to(workspace)),
                    "agent": None,
                    "lines": path.read_text(encoding="utf-8", errors="replace").count("\n") + 1,
                    "bytes": path.stat().st_size,
                    "done": True,
                    "revised": False,
                }
            )
    return {"files": items}


@app.get("/api/run/{run_id}/file")
async def run_file(run_id: str, path: str) -> dict:
    workspace = _run_dir(run_id)
    target = _contained(workspace, path)
    if not target.is_file():
        raise HTTPException(404, "file not found")
    return {"path": path, "content": target.read_text(encoding="utf-8", errors="replace")}


@app.post("/api/run/{run_id}/resume")
async def resume_run(run_id: str, payload: Optional[ResumeRequest] = None) -> dict:
    """Continue a run from the plan and files already on disk.

    The expensive phases are skipped: a run that died after building goes straight to
    review, repair and its receipt. Nothing is re-planned, so a resume cannot silently
    become a different build under the same id.
    """
    existing = harness.get(run_id)
    if existing is None:
        raise HTTPException(404, "unknown run")
    ids = list(payload.specialist_ids) if payload else []
    team = _team(ids, []) if ids else [spec for spec in ROSTER.load() if spec.ready]
    if not team:
        raise HTTPException(400, "no configured specialist with a model, base URL and key")
    depth = (payload.depth if payload else "") or existing.depth
    resumed = harness.resume(run_id, team, depth)
    if resumed is None:
        raise HTTPException(404, "unknown run")
    return {"run_id": resumed.run_id, "resumed": True, "depth": resumed.depth, "state": "queued"}


@app.get("/api/run/{run_id}/receipt")
async def run_receipt(run_id: str) -> dict:
    """The run's receipt: request, files, checks, cost, approvals and open issues."""
    workspace = _run_dir(run_id)
    text = receipt.read(workspace)
    if text is None:
        run = harness.get(run_id)
        if run is None:
            raise HTTPException(404, "unknown run")
        receipt.write(run)
        text = receipt.read(workspace)
    if text is None:
        raise HTTPException(404, "no receipt for this run")
    return {"run_id": run_id, "path": "RECEIPT.md", "content": text}


@app.post("/api/run/{run_id}/preview-check")
async def preview_check(run_id: str) -> dict:
    """Tier 3: probe the live service, and record the facts as a `verify` event."""
    _run_dir(run_id)
    service = runner.get(run_id)
    report = await probe.check(service.url if service else None)
    run = harness.get(run_id)
    if run is not None:
        await run.emit({"type": "verify", **report})
        await run.emit(
            {
                "type": "message",
                "from": "verify",
                "kind": "review",
                "text": (
                    f"preview checks: {report['checked']} URL(s) requested, {report['errors']} problem(s)"
                    if report["checked"]
                    else f"preview check: {report['issues'][0]['detail'] if report['issues'] else 'nothing to check'}"
                ),
            }
        )
    return report


@app.get("/api/run/{run_id}/metrics")
async def run_metrics(run_id: str) -> dict:
    path = _run_dir(run_id) / ".harness-metrics.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    run = harness.get(run_id)
    if run:
        return run.metrics()
    raise HTTPException(404, "unknown run")


@app.post("/api/run/{run_id}/serve")
async def serve_start(run_id: str, restart: bool = False, approve: bool = False) -> dict:
    """Install, build and serve what the run wrote. Returns at once; poll for state.

    `restart` stops the running service first, so edited files are installed, built and
    served again instead of the old process being handed back.

    `approve` is the human checkpoint. Installs, builds and model-authored servers are an
    `ask` decision in modules/permissions.py, so without it the response is
    `needs_approval` naming the command, and nothing has executed yet.
    """
    workspace = _run_dir(run_id)
    if not workspace.exists():
        raise HTTPException(404, "unknown run")
    # The run's own event stream receives the install/build/serve calls as `tool` events,
    # so a failed install is visible in `events.jsonl` and not only in the service log.
    run = harness.get(run_id)
    if run is not None and not run.done:
        # Building a half-written tree is how an empty package.json became a "failed
        # build". The preview is a verdict on finished work, not a progress bar.
        raise HTTPException(409, "the run is still writing files; wait for it to finish before building")
    service = await runner.start(
        run_id,
        workspace,
        restart=restart,
        approved=approve,
        on_event=run.emit if run is not None else None,
    )
    if run is not None:
        _record_build(run, service)
    return _service_payload(service, run_id)


@app.get("/api/run/{run_id}/serve")
async def serve_status(run_id: str) -> dict:
    service = runner.get(run_id)
    run = harness.get(run_id)
    # The state a poll sees is more honest than the one the start returned: installing and
    # building settle later, so the ledger follows them instead of freezing on "detecting".
    if service is not None and run is not None and service.state != (run.build or {}).get("state"):
        _record_build(run, service)
    return _service_payload(service, run_id)


@app.post("/api/run/{run_id}/serve/stop")
async def serve_stop(run_id: str) -> dict:
    return _service_payload(await runner.stop(run_id), run_id)


@app.on_event("startup")
async def startup() -> None:
    """Artifacts get their own origin; a busy preview port disables previews only."""
    await preview.serve_in_background()


@app.on_event("shutdown")
async def shutdown() -> None:
    """Never leave a served run, an artifact origin or a connection pool behind."""
    await runner.stop_all()
    await preview.stop_background()
    await llm.aclose_client()


if DIST_DIR.exists():
    app.mount("/", StaticFiles(directory=str(DIST_DIR), html=True), name="ui")
