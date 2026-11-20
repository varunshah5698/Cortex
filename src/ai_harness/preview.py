"""Artifacts on their own origin.

Generated files are untrusted: they are model output, and the model may have read a page
an attacker wrote. Serving them from the API's origin made every preview script
same-origin with `/api`, so a run's own code could read the roster and start runs.

This app therefore listens on a separate loopback port and exposes no `/api` at all.
A script inside a preview can only reach the API cross-origin — and the API refuses a
foreign `Origin` before the handler runs. The UI additionally frames previews sandboxed,
so the page has an opaque origin and no storage.

`/live/<run>/<token>/…` proxies a run's served app (the `/serve` pipeline). That server
is arbitrary model-authored code, so we cannot add auth to it; instead its port is never
advertised and the only URL the UI gets carries a per-run secret token.
"""

from __future__ import annotations

import asyncio
import mimetypes
import re
import secrets
import socket
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

from .config.settings import PREVIEW_HOST, PREVIEW_PORT
from .modules import runner, static_server
from .paths import contained, run_dir
from .utils.validators import UnsafePath

app = FastAPI(title="Cortex artifacts", docs_url=None, redoc_url=None, openapi_url=None)

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
}


def _run(run_id: str) -> Path:
    try:
        return run_dir(run_id)
    except UnsafePath:
        raise HTTPException(404, "unknown run") from None


def _hardened(headers: dict) -> dict:
    headers["cache-control"] = "no-store"
    headers["x-content-type-options"] = "nosniff"
    return headers


# A generated page is written as if it were the site root: `/src/styles.css`,
# `/assets/app.js`, `url(/img/hero.png)`. Served under `/preview/<run>/` those
# resolve to the preview origin's own root and 404, so the page arrives unstyled
# and scriptless — the "nothing is working" preview. Rebase them onto the run.
_ROOT_REF = re.compile(
    r"""(?P<attr>\b(?:href|src|action|poster|data)\s*=\s*)(?P<q>["'])(?P<url>/[^"'<>]*)(?P=q)""",
    re.IGNORECASE,
)
_CSS_REF = re.compile(r"""url\(\s*(?P<q>["']?)(?P<url>/[^)"']*)(?P=q)\s*\)""", re.IGNORECASE)


def _rebase(url: str, prefix: str) -> str:
    """`/src/app.js` -> `/preview/<run>/src/app.js`, leaving foreign URLs alone."""
    if url.startswith("//") or url.startswith(prefix):
        return url
    return prefix + url.lstrip("/")


def _rebase_html(text: str, run_id: str) -> str:
    prefix = f"/preview/{run_id}/"
    text = _ROOT_REF.sub(
        lambda m: f"{m.group('attr')}{m.group('q')}{_rebase(m.group('url'), prefix)}{m.group('q')}", text
    )
    return _CSS_REF.sub(lambda m: f"url({m.group('q')}{_rebase(m.group('url'), prefix)}{m.group('q')})", text)


def _notice(title: str, detail: str) -> HTMLResponse:
    """A readable page for the iframe.

    The preview pane embeds this origin directly, so a JSON error body renders as
    literal `{"detail":"unknown run"}` inside the panel and reads as a broken app.
    Every non-file answer here is HTML for that reason.
    """
    return HTMLResponse(
        "<!doctype html><body style='font-family:ui-sans-serif;padding:32px;color:#3d3843'>"
        f"<h3>{title}</h3><p style='color:#6f6879'>{detail}</p></body>",
        status_code=404,
        headers=_hardened({}),
    )


@app.get("/preview/{run_id}/{path:path}")
async def preview(run_id: str, path: str = ""):
    """A run's generated files, static, straight from the workspace."""
    try:
        workspace = _run(run_id)
    except HTTPException:
        return _notice(
            "This run is not on this harness",
            f"No workspace for run {run_id}. It belongs to another deployment or was deleted.",
        )
    if not workspace.exists():
        return _notice(
            "This run is not on this harness",
            f"No workspace for run {run_id}. It belongs to another deployment or was deleted.",
        )
    try:
        target = contained(workspace, path or "index.html")
    except UnsafePath:
        raise HTTPException(403, "outside the workspace") from None
    if static_server.is_route(path, target):
        target = workspace / "index.html"  # a client-side route: serve the app shell
    elif target.is_dir():
        target = target / "index.html"
    if not target.is_file():
        files = sorted(
            str(p.relative_to(workspace)) for p in workspace.rglob("*") if p.is_file() and not p.name.startswith(".")
        )
        links = "".join(f"<li><a href='/preview/{run_id}/{name}'>{name}</a></li>" for name in files)
        return HTMLResponse(
            "<!doctype html><body style='font-family:ui-sans-serif;padding:32px;color:#3d3843'>"
            f"<h3>No index.html in this run yet</h3><ul>{links}</ul></body>",
            headers=_hardened({}),
        )
    media_type = _mimetype(target)
    if media_type in ("text/html", "text/css"):
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            raise HTTPException(404, "unreadable file") from None
        return HTMLResponse(_rebase_html(text, run_id), media_type=media_type, headers=_hardened({}))
    return FileResponse(target, media_type=media_type, headers=_hardened({}))


_TYPES = {
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".jsx": "text/javascript",
    ".ts": "text/javascript",
    ".tsx": "text/javascript",
    ".css": "text/css",
    ".map": "application/json",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
}


def _mimetype(path: Path) -> str:
    # A generated Vite tree asks for `/src/main.tsx` and `.mjs` chunks; guessing those
    # gave `application/octet-stream`, which the browser refuses to execute as a module.
    known = _TYPES.get(path.suffix.lower())
    if known:
        return known
    return mimetypes.guess_type(str(path))[0] or "application/octet-stream"


@app.api_route("/live/{run_id}/{token}/{path:path}", methods=["GET", "POST"])
async def live(run_id: str, token: str, request: Request, path: str = ""):
    """Proxy a run's served app, behind a per-run secret the port itself does not have."""
    service = runner.get(run_id)
    if service is None or service.state != "ready" or not service.port:
        raise HTTPException(404, "this run is not serving")
    if not secrets.compare_digest(token, service.share):
        raise HTTPException(403, "preview token is wrong")

    # A served tree lives under the run's nonce upstream too; a model-authored app
    # defines its own routes, so `prefix` is empty there.
    target = f"http://127.0.0.1:{service.port}{service.prefix}/{path}"
    if request.url.query:
        target = f"{target}?{request.url.query}"

    forward = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}
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

    headers = _hardened({k: v for k, v in upstream.headers.items() if k.lower() not in HOP_BY_HOP})
    return StreamingResponse(
        body(),
        status_code=upstream.status_code,
        headers=headers,
        media_type=upstream.headers.get("content-type"),
    )


# ---------- lifecycle ----------

_server: uvicorn.Server | None = None
_task: asyncio.Task[None] | None = None


def _free(host: str, port: int) -> bool:
    """Can a server bind here?

    `SO_REUSEADDR` matters: after a restart the previous preview leaves sockets in
    TIME_WAIT, and a plain bind fails on those even though uvicorn (which sets the
    option) would bind fine. Without it every restart silently disabled previews.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


async def serve_in_background() -> bool:
    """Start the artifact origin beside the API. A busy port disables previews, not the API."""
    global _server, _task
    if _task is not None and not _task.done():
        return True
    if not _free(PREVIEW_HOST, PREVIEW_PORT):
        return False
    config = uvicorn.Config(
        app,
        host=PREVIEW_HOST,
        port=PREVIEW_PORT,
        log_level="warning",
        access_log=False,
        lifespan="off",
    )
    _server = uvicorn.Server(config)
    _task = asyncio.create_task(_server.serve(), name="cortex-preview")
    await asyncio.sleep(0.2)  # let it bind before the API reports ready
    return not _task.done()


async def stop_background() -> None:
    global _server, _task
    if _server is not None:
        _server.should_exit = True
    if _task is not None:
        await asyncio.gather(_task, return_exceptions=True)
    _server, _task = None, None


__all__ = ["app", "PREVIEW_PORT", "serve_in_background", "stop_background"]
