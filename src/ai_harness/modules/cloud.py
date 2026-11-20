"""One-run public shares: a tunnel in front of `share.py`, never in front of `/api`.

Publishing generated code is a different act from previewing it on loopback. The tunnel
is therefore opt-in (the API asks for approval), time-bounded (`HARNESS_SHARE_TTL`), and
points at a dedicated one-run origin (`share.py`) with a path token. It never points at
the preview app, because that app serves every run by id, and it never points at `/api`.

The tunnel provider is either `HARNESS_TUNNEL_COMMAND` (a command template with `{url}`)
or `cloudflared` when it is installed. A missing provider is a clear error, not a fake
success; the harness does not install binaries behind the owner's back.
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import shlex
import shutil
import signal
import socket
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import uvicorn

from .. import share
from ..config.settings import REQUIRE_APPROVAL, SHARE_TTL, TUNNEL_COMMAND, TUNNEL_TIMEOUT
from ..utils.logger import event_log
from . import permissions, runner

ANSI = re.compile(r"\x1b\[[0-9;]*m")
URL = re.compile(r"https?://[^\s\"'<>]+")
LOG_LINES = 120


class ShareError(RuntimeError):
    """A share was refused or failed before it reached a public URL."""


class TunnelUnavailable(ShareError):
    """No tunnel provider is installed or configured."""


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _public_url(text: str) -> str | None:
    """The tunnel URL, not the provider's documentation link.

    cloudflared prints its terms and docs URLs before the tunnel exists. A generic
    custom tunnel may publish any host, so the default provider is strict about
    `trycloudflare.com` and a configured command accepts the first other public host.
    """
    for candidate in URL.findall(ANSI.sub("", text)):
        clean = candidate.rstrip(".,);]")
        host = (urlparse(clean).hostname or "").lower()
        if not host or host in ("127.0.0.1", "localhost", "::1") or "." not in host:
            continue
        if host.endswith(".trycloudflare.com"):
            return clean
        if host.endswith("cloudflare.com") or host.endswith("cloudflare.org"):
            continue  # terms/docs, not the tunnel
        if TUNNEL_COMMAND:
            return clean
    return None


def _command(loopback: str) -> list[str]:
    if TUNNEL_COMMAND:
        try:
            return shlex.split(TUNNEL_COMMAND.format(url=loopback))
        except (KeyError, ValueError) as exc:
            raise ShareError(f"HARNESS_TUNNEL_COMMAND is not a valid template: {exc}") from exc
    binary = shutil.which("cloudflared")
    if not binary:
        raise TunnelUnavailable(
            "no tunnel provider: install cloudflared, or set HARNESS_TUNNEL_COMMAND with a {url} placeholder"
        )
    return [binary, "tunnel", "--url", loopback, "--no-autoupdate"]


@dataclass
class Share:
    run_id: str
    state: str = "idle"
    url: str | None = None
    port: int | None = None
    command: str = ""
    detail: str = ""
    logs: list[str] = field(default_factory=list)
    expires_at: str | None = None
    process: asyncio.subprocess.Process | None = None
    server: uvicorn.Server | None = None
    server_task: asyncio.Task | None = None
    pump: asyncio.Task | None = None
    timer: asyncio.Task | None = None

    def log(self, line: str) -> None:
        clean = ANSI.sub("", line).rstrip()
        if not clean:
            return
        self.logs.append(clean[:400])
        if len(self.logs) > LOG_LINES:
            del self.logs[: len(self.logs) - LOG_LINES]

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "state": self.state,
            "url": self.url,
            "detail": self.detail,
            "command": self.command,
            "expires_at": self.expires_at,
            "logs": self.logs[-LOG_LINES:],
        }


SHARES: dict[str, Share] = {}


def get(run_id: str) -> Share | None:
    return SHARES.get(run_id)


def _preview_command() -> str:
    """What an approval is about, before a port exists."""
    if TUNNEL_COMMAND:
        return TUNNEL_COMMAND.format(url="http://127.0.0.1:<free>/")
    binary = shutil.which("cloudflared")
    if not binary:
        raise TunnelUnavailable(
            "no tunnel provider: install cloudflared, or set HARNESS_TUNNEL_COMMAND with a {url} placeholder"
        )
    return f"{binary} tunnel --url http://127.0.0.1:<free>/ --no-autoupdate"


async def _read_url(share_obj: Share, process: asyncio.subprocess.Process) -> str:
    """Read the tunnel's own output until it names a public URL."""
    stdout = process.stdout
    if stdout is None:
        raise ShareError("the tunnel process produced no output")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + TUNNEL_TIMEOUT
    collected: list[str] = []
    while loop.time() < deadline:
        remaining = max(0.1, deadline - loop.time())
        try:
            raw = await asyncio.wait_for(stdout.readline(), remaining)
        except asyncio.TimeoutError:
            break
        if not raw:
            break
        line = raw.decode("utf-8", "replace")
        collected.append(line)
        share_obj.log(line)
        found = _public_url(line)
        if found:
            share_obj.pump = asyncio.create_task(_pump(share_obj, process))
            return found
    detail = "".join(collected)[-600:] or "the tunnel exited without printing a URL"
    raise ShareError(f"the tunnel did not publish a URL: {detail}")


async def _pump(share_obj: Share, process: asyncio.subprocess.Process) -> None:
    stdout = process.stdout
    if stdout is None:
        return
    while True:
        raw = await stdout.readline()
        if not raw:
            break
        share_obj.log(raw.decode("utf-8", "replace"))


async def _expire(run_id: str) -> None:
    await asyncio.sleep(max(1.0, SHARE_TTL))
    if run_id in SHARES:
        await stop(run_id, reason="expired")


async def start(run_id: str, approved: bool = False) -> Share:
    """Open a public share for one ready run. Returns a `needs_approval` share first."""
    existing = SHARES.get(run_id)
    if existing is not None and existing.state in ("starting", "ready"):
        return existing

    service = runner.get(run_id)
    if service is None or service.state != "ready":
        raise ShareError("the run is not serving: run the preview first, then share it")

    command = _preview_command()
    if REQUIRE_APPROVAL and not approved:
        pending = Share(
            run_id=run_id,
            state="needs_approval",
            command=command,
            detail="this publishes the run on a public URL until it is stopped or the TTL expires",
        )
        SHARES[run_id] = pending
        return pending

    share_obj = Share(run_id=run_id, state="starting", command=command, detail="starting the one-run public origin")
    SHARES[run_id] = share_obj

    token = secrets.token_urlsafe(18)
    port = free_port()
    app = share.create_app(run_id, token)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False, lifespan="off")
    )
    share_obj.server = server
    share_obj.port = port
    share_obj.server_task = asyncio.create_task(server.serve(), name=f"share-{run_id}")

    # Wait for the public origin to bind before the tunnel can reach it.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5.0
    while not server.started and loop.time() < deadline:
        if share_obj.server_task.done():
            break
        await asyncio.sleep(0.05)
    if not server.started:
        await stop(run_id, reason="the public origin did not start")
        raise ShareError("the one-run public origin did not start")

    loopback = f"http://127.0.0.1:{port}/"
    try:
        argv = _command(loopback)
    except ShareError:
        await stop(run_id, reason="no tunnel provider")
        raise

    # The tunnel is a harness-owned process: no provider keys, no HARNESS_* settings.
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            env=permissions.safe_env({"NO_COLOR": "1"}),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        await stop(run_id, reason="the tunnel command is not installed")
        raise TunnelUnavailable(f"the tunnel command is not installed: {argv[0]}") from exc

    share_obj.process = process
    try:
        base_url = await _read_url(share_obj, process)
        # The token path goes in the copied link; the app's root still redirects here
        # for a visitor who trims it.
        share_obj.url = f"{base_url.rstrip('/')}/{token}/"
    except ShareError:
        await stop(run_id, reason="the tunnel failed")
        raise
    if process.returncode is not None:
        await stop(run_id, reason="the tunnel exited")
        raise ShareError(f"the tunnel exited with code {process.returncode}")

    share_obj.state = "ready"
    share_obj.detail = (
        f"public for one run · stops after {int(SHARE_TTL)}s"
        if SHARE_TTL > 0
        else "public for one run · no automatic expiry"
    )
    if SHARE_TTL > 0:
        share_obj.expires_at = (datetime.now(timezone.utc) + timedelta(seconds=SHARE_TTL)).isoformat()
        share_obj.timer = asyncio.create_task(_expire(run_id), name=f"share-ttl-{run_id}")
    event_log(run_id, "share", url=share_obj.url, expires_at=share_obj.expires_at)
    return share_obj


async def stop(run_id: str, reason: str = "stopped") -> Share:
    share_obj = SHARES.pop(run_id, None) or Share(run_id=run_id, state="stopped")
    process = share_obj.process
    share_obj.process = None
    if process is not None and process.returncode is None:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                process.terminate()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(process.wait(), 5.0)
        except asyncio.TimeoutError:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            await process.wait()
    for task in (share_obj.pump, share_obj.timer):
        if task is not None and not task.done():
            task.cancel()
    server = share_obj.server
    if server is not None:
        server.should_exit = True
    if share_obj.server_task is not None:
        await asyncio.gather(share_obj.server_task, return_exceptions=True)
    share_obj.state = "stopped"
    share_obj.url = None
    share_obj.detail = reason
    event_log(run_id, "share-stop", reason=reason)
    return share_obj


async def stop_all() -> None:
    for run_id in list(SHARES):
        await stop(run_id, reason="the harness is shutting down")


__all__ = ["Share", "ShareError", "TunnelUnavailable", "get", "start", "stop", "stop_all"]
