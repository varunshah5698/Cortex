"""A public origin that serves exactly one run.

A tunnel in front of `preview.py` would expose every run on the machine: the preview app
serves `/preview/<run>/…` for any run id. Exposing generated code is a deliberate act, so
the public surface is a separate app with one run baked in, one path token, and no `/api`
at all. It is mounted on a free loopback port by `modules/cloud.py` and that is the only
port the tunnel points at.

The token lives in the path because a browser navigation cannot carry a header. A wrong
token is a 404, not a 403, so the public origin does not confirm which run it is.
"""

from __future__ import annotations

import mimetypes
import secrets
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse

from .modules import runner
from .paths import contained, run_dir
from .utils.validators import UnsafePath

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
    # `aiter_raw` forwards the body as received; the length/encoding headers describe
    # the original representation, not this stream.
    "content-encoding",
    "content-length",
}


def _headers() -> dict:
    return {
        "cache-control": "no-store",
        "x-content-type-options": "nosniff",
        "referrer-policy": "no-referrer",
    }


def _mimetype(path: Path) -> str:
    if path.suffix == ".js":
        return "text/javascript"
    if path.suffix == ".css":
        return "text/css"
    return mimetypes.guess_type(str(path))[0] or "application/octet-stream"


def create_app(run_id: str, token: str) -> FastAPI:
    """Build the one-run app. `token` is the public path segment, not an API key."""
    app = FastAPI(title="Cortex shared run", docs_url=None, redoc_url=None, openapi_url=None)
    workspace = run_dir(run_id)

    def allowed(candidate: str) -> bool:
        return secrets.compare_digest(candidate, token)

    @app.get("/")
    async def root():
        # The token is already in the public URL the owner copied; sending the root here
        # makes the link forgiving without guessing a run.
        return RedirectResponse(url=f"/{token}/", status_code=307)

    @app.get("/{candidate}")
    async def entry(candidate: str):
        if not allowed(candidate):
            raise HTTPException(404, "not found")
        return RedirectResponse(url=f"/{token}/", status_code=307)

    @app.api_route(
        "/{candidate}/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    )
    async def public(candidate: str, path: str, request: Request):
        if not allowed(candidate):
            raise HTTPException(404, "not found")

        service = runner.get(run_id)
        if service is not None and service.state == "ready" and service.port:
            # The live app's own origin. A static tree lives under the run's nonce, which
            # is why the prefix is included here rather than exposed to the visitor.
            target = f"http://127.0.0.1:{service.port}{service.prefix}/{path}"
            if request.url.query:
                target = f"{target}?{request.url.query}"
            forward = {key: value for key, value in request.headers.items() if key.lower() not in HOP_BY_HOP}
            client = httpx.AsyncClient(timeout=None)
            try:
                upstream = await client.send(
                    client.build_request(
                        request.method,
                        target,
                        headers=forward,
                        content=await request.body(),
                    ),
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
                **_headers(),
                **{k: v for k, v in upstream.headers.items() if k.lower() not in HOP_BY_HOP},
            }
            return StreamingResponse(
                body(),
                status_code=upstream.status_code,
                headers=headers,
                media_type=upstream.headers.get("content-type"),
            )

        # The service is stopped or not serving: serve the files as written rather than
        # a blank proxy. This is a preview, not a promise that the app is running.
        try:
            target = contained(workspace, path or "index.html")
        except UnsafePath:
            raise HTTPException(404, "not found") from None
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            raise HTTPException(404, "not found")
        return FileResponse(target, media_type=_mimetype(target), headers=_headers())

    return app


__all__ = ["create_app"]
