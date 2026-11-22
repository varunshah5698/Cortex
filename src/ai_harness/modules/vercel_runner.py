"""Per-run Vercel deployments for static and Node builds.

Vercel is the cloud runner for the stacks it can build: a plain page, a Vite/React tree,
or any `package.json` whose build Vercel recognizes. The run is copied to a lowercase temp
project, deployed with the user's existing Vercel CLI login (or `VERCEL_TOKEN`), and
removed on stop or after `HARNESS_VERCEL_TTL`.

This is deliberately separate from `modules/runner.py`: the local runner owns processes,
the cloud runner owns a dedicated Python service, and Vercel owns the hosted static/Node
case. A Python ASGI app is not silently shipped to a static host; the caller falls back
to the local runner and says why.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx

from ..config.settings import ROOT, VERCEL_BIN, VERCEL_SCOPE, VERCEL_TIMEOUT, VERCEL_TOKEN, VERCEL_TTL
from . import permissions

ANSI = re.compile(r"\x1b\[[0-9;]*m")
URL = re.compile(r"https://[^\s\"'<>]+\.vercel\.app")
ALIAS = re.compile(r"Aliased\s+(https://[^\s\"'<>]+)")
EXCLUDE = {"node_modules", ".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".vercel", "dist", "build", "out"}


class VercelError(RuntimeError):
    """The deployment was refused or failed; the CLI output is the reason."""


def binary() -> str:
    """Prefer the build-local CLI; Render cannot write a global npm prefix."""
    local = Path(ROOT) / ".vercel-cli" / "node_modules" / ".bin" / "vercel"
    if local.is_file():
        return str(local)
    return shutil.which(VERCEL_BIN) or VERCEL_BIN


def available() -> bool:
    local = Path(ROOT) / ".vercel-cli" / "node_modules" / ".bin" / "vercel"
    return local.is_file() or shutil.which(VERCEL_BIN) is not None


def supported(workspace: Path) -> bool:
    """Vercel can build a static page or a Node project; it cannot host an ASGI server."""
    return (workspace / "package.json").is_file() or (workspace / "index.html").is_file()


def _copy(workspace: Path, target: Path) -> None:
    for path in sorted(workspace.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(workspace)
        if any(part in EXCLUDE for part in relative.parts):
            continue
        if path.name.startswith(".harness-") or path.name in ("RECEIPT.md", "todo.md"):
            continue
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)


def _project(run_id: str) -> str:
    return f"cortex-{run_id}".lower()


@dataclass
class VercelService:
    run_id: str
    project: str
    url: str = ""
    state: str = "starting"
    detail: str = "deploying to Vercel"
    logs: list[str] = field(default_factory=list)
    exit_code: Optional[int] = None
    expires_in: int = int(VERCEL_TTL)
    remote: bool = True
    workdir: Optional[Path] = None
    timer: Optional[asyncio.Task] = None

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
        return "Vercel's build and hosting platform; the deployment gets no harness keys"

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "state": self.state,
            "kind": "vercel",
            "detail": self.detail,
            "url": self.url or None,
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

    async def stop(self) -> None:
        if self.timer is not None and not self.timer.done():
            self.timer.cancel()
        if self.state in ("stopped", "failed") and not self.url:
            return
        try:
            process = await asyncio.create_subprocess_exec(
                binary(),
                "remove",
                self.project,
                "--yes",
                *(["--token", VERCEL_TOKEN] if VERCEL_TOKEN else []),
                cwd=str(self.workdir) if self.workdir is not None else None,
                env=permissions.safe_env({"CI": "1", "NO_COLOR": "1"}),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                output, _ = await asyncio.wait_for(process.communicate(), 60)
                self.logs.extend(
                    line for line in ANSI.sub("", output.decode("utf-8", "replace")).splitlines() if line.strip()
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        except OSError as exc:
            self.logs.append(f"could not remove the Vercel project: {exc}")
        finally:
            if self.workdir is not None:
                shutil.rmtree(self.workdir, ignore_errors=True)
            self.state = "stopped"
            self.detail = "stopped on Vercel"
            self.url = ""


def _config_dirs() -> list[Path]:
    """Every place the CLI may have written its login, newest convention first.

    `~/.vercel` is the documented location, but the CLI on macOS uses
    `~/Library/Application Support/com.vercel.cli` and honours `VERCEL_CONFIG_DIR`. A check
    that only looked at `~/.vercel` reported "not authenticated" on a machine where
    `vercel whoami` worked, which is worse than no check at all.
    """
    dirs: list[Path] = []
    override = os.getenv("VERCEL_CONFIG_DIR", "").strip()
    if override:
        dirs.append(Path(override))
    dirs.append(Path.home() / ".vercel")
    if sys.platform == "darwin":
        dirs.append(Path.home() / "Library" / "Application Support" / "com.vercel.cli")
    dirs.append(Path(os.getenv("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "com.vercel.cli")
    return dirs


def credentials() -> tuple[bool, str]:
    """Whether this host can authenticate at all, and why not when it cannot.

    The CLI falls back to a stored login from an interactive `vercel login`. A server has
    no such file — Render builds the CLI into `.vercel-cli` but never logs in — so `deploy`
    fails with a bare exit 1 and "No existing credentials found", which the build room
    could only render as `Vercel exited 1`. Check first and say the real thing.
    """
    if VERCEL_TOKEN:
        return True, ""
    for config in _config_dirs():
        for name in ("auth.json", "auth"):
            if (config / name).is_file():
                return True, ""
    searched = ", ".join(str(path) for path in _config_dirs())
    return False, (
        "Vercel is not authenticated on this host: no HARNESS_VERCEL_TOKEN is set and there is "
        f"no CLI login in {searched}. Set HARNESS_VERCEL_TOKEN in the service environment, or run "
        "`vercel login` where the harness runs."
    )


async def deploy(run_id: str, workspace: Path) -> VercelService:
    if not available():
        raise VercelError(f"Vercel CLI is not installed ({VERCEL_BIN})")
    if not supported(workspace):
        raise VercelError("Vercel mode needs a package.json or index.html; this run is not a static/Node tree")
    ok, why = credentials()
    if not ok:
        # Fail before creating a project: a half-created project with no deployment is a
        # leak that the TTL has to clean up, and the reason is more useful than an exit code.
        raise VercelError(why)
    project = _project(run_id)
    workdir = Path(tempfile.mkdtemp(prefix=f"{project}-"))
    _copy(workspace, workdir)
    service = VercelService(run_id=run_id, project=project, workdir=workdir)
    common = (["--token", VERCEL_TOKEN] if VERCEL_TOKEN else []) + (["--scope", VERCEL_SCOPE] if VERCEL_SCOPE else [])
    # `deploy --project` only works for an existing project; create the exact name first
    # so the removal later has a stable identifier instead of Vercel's generated suffix.
    try:
        create = await asyncio.create_subprocess_exec(
            binary(),
            "project",
            "add",
            project,
            "--non-interactive",
            *common,
            cwd=str(workdir),
            env=permissions.safe_env({"CI": "1", "NO_COLOR": "1"}),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            created, _ = await asyncio.wait_for(create.communicate(), 60)
        except asyncio.TimeoutError:
            create.kill()
            await create.wait()
        service.logs.extend(
            line for line in ANSI.sub("", created.decode("utf-8", "replace")).splitlines() if line.strip()
        )
        # A newly created project inherits the team's SSO protection, which turns every
        # URL into Vercel's login page. This is a public, per-run preview by design; the
        # project is removed at the TTL, and it contains only the run's own files.
        protect = await asyncio.create_subprocess_exec(
            binary(),
            "project",
            "protection",
            "disable",
            project,
            "--sso",
            "--non-interactive",
            *common,
            cwd=str(workdir),
            env=permissions.safe_env({"CI": "1", "NO_COLOR": "1"}),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            protected, _ = await asyncio.wait_for(protect.communicate(), 60)
        except asyncio.TimeoutError:
            protect.kill()
            await protect.wait()
        service.logs.extend(
            line for line in ANSI.sub("", protected.decode("utf-8", "replace")).splitlines() if line.strip()
        )
    except OSError as exc:
        shutil.rmtree(workdir, ignore_errors=True)
        raise VercelError(f"could not create the Vercel project: {exc}") from exc

    command = [binary(), "deploy", "--yes", "--prod", "--json", "--project", project, *common]
    started = time.perf_counter()
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(workdir),
            env=permissions.safe_env({"CI": "1", "NO_COLOR": "1"}),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), VERCEL_TIMEOUT)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise VercelError(f"the Vercel deployment did not finish within {VERCEL_TIMEOUT:.0f}s") from None
    except OSError as exc:
        shutil.rmtree(workdir, ignore_errors=True)
        raise VercelError(f"could not start the Vercel CLI: {exc}") from exc
    text = ANSI.sub("", output.decode("utf-8", "replace"))
    service.logs = [line for line in text.splitlines() if line.strip()]
    if process.returncode != 0:
        service.state = "failed"
        # The CLI's own last line is the reason ("No existing credentials found", a scope
        # error, a build error). Reporting only the exit code left the build room showing
        # `Vercel exited 1` with nothing to act on.
        reason = next((line.strip() for line in reversed(service.logs) if line.strip()), "")
        service.detail = f"Vercel exited {process.returncode}" + (f": {reason[:300]}" if reason else "")
        service.exit_code = process.returncode
        shutil.rmtree(workdir, ignore_errors=True)
        return service
    # `--json` still prints human progress lines, then the result object. The deployment
    # URL in that object is canonical; the `Aliased` line is a nicer domain but it is not
    # guaranteed to be the project the removal step knows about.
    found = None
    start = text.rfind("\n{")
    if start >= 0:
        try:
            payload = json.loads(text[start:])
            found = (payload.get("deployment") or {}).get("url")
        except json.JSONDecodeError:
            found = None
    if found is None:
        alias = ALIAS.search(text)
        found = alias.group(1) if alias else None
    if found is None:
        urls = URL.findall(text)
        found = urls[-1] if urls else None
    if found is None:
        service.state = "failed"
        service.detail = "Vercel answered without a deployment URL"
        service.exit_code = process.returncode
        shutil.rmtree(workdir, ignore_errors=True)
        return service
    service.url = found if found.startswith("http") else f"https://{found}"
    service.state = "ready"
    service.detail = f"serving on Vercel · {int(time.perf_counter() - started)}s"
    # The deployment is READY before its first request always succeeds; a short probe
    # avoids handing the iframe a 404 that turns into a working page a second later.
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            for _ in range(8):
                response = await client.get(service.url)
                if response.status_code < 400:
                    break
                await asyncio.sleep(1)
    except httpx.HTTPError:
        pass
    service.expires_in = int(VERCEL_TTL)
    if VERCEL_TTL > 0:
        service.timer = asyncio.create_task(_expire(service), name=f"vercel-ttl-{run_id}")
    return service


async def _expire(service: VercelService) -> None:
    await asyncio.sleep(max(1.0, VERCEL_TTL))
    await service.stop()


async def stop_all(services: dict[str, VercelService]) -> None:
    for run_id in list(services):
        try:
            await services[run_id].stop()
        except Exception:  # noqa: BLE001 - one dead deployment must not block shutdown
            pass
        services.pop(run_id, None)


__all__ = ["VercelError", "VercelService", "available", "deploy", "stop_all", "supported"]
