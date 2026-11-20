"""Runner: makes a finished run execute.

Writing files is not shipping software. The runner takes the tree a run produced, works
out what kind of project it is, installs the dependencies it declared, builds it when
the stack has a build step, and then serves the result on a real loopback port — so the
preview is a running site instead of the source files rendered as text.

It is deliberately honest about failure: a failed install or build leaves its output in
the service log, and the UI shows that log rather than a fake screenshot.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import shutil
import signal
import socket
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from ..utils.logger import event_log
from . import permissions, sandbox

INSTALL_TIMEOUT = float(os.getenv("HARNESS_INSTALL_TIMEOUT", "300"))
BUILD_TIMEOUT = float(os.getenv("HARNESS_BUILD_TIMEOUT", "300"))
BOOT_TIMEOUT = float(os.getenv("HARNESS_BOOT_TIMEOUT", "45"))
REQUIRE_APPROVAL = os.getenv("HARNESS_REQUIRE_APPROVAL", "1").strip().lower() in ("1", "true", "yes", "on")
LOG_LINES = 400

INDEX = ("index.html", "index.htm")
BUILD_ROOTS = ("dist", "build", "out")
STATIC_SERVER = Path(__file__).with_name("static_server.py")
PYTHON_MODULES = ("app", "main", "server", "api")
ASGI = re.compile(r"\b(?:FastAPI|Starlette)\s*\(")


class RunnerError(RuntimeError):
    """A failure the user has to see: missing tool, failed install, dead server."""


class ApprovalRequired(RunnerError):
    """A step the policy marks `ask` was reached without a human approval.

    This is the checkpoint, not an error: the service stays in `needs_approval` with the
    command named, and the same run starts normally once approval arrives.
    """

    def __init__(self, argv: list[str], reason: str) -> None:
        self.commands = [permissions.command_line(argv)]
        self.reason = reason
        super().__init__(f"approval required before `{self.commands[0]}`: {reason}")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _index(directory: Path) -> Path | None:
    for name in INDEX:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


@dataclass
class Service:
    """One run's live service: what it is, where it listens, and what it printed."""

    run_id: str
    workspace: Path
    state: str = "idle"
    kind: str = "none"
    detail: str = ""
    port: int | None = None
    root: Path | None = None
    process: asyncio.subprocess.Process | None = None
    task: asyncio.Task | None = None
    pump: asyncio.Task | None = None
    logs: list[str] = field(default_factory=list)
    exit_code: int | None = None
    # The human checkpoint. False until a request carries an explicit approval.
    approved: bool = False
    pending: list[str] = field(default_factory=list)
    # What the commands above will actually do: dependencies, scripts, the entry point.
    # A checkpoint that only names `npm install` asks the operator to approve a word.
    approval_detail: list[str] = field(default_factory=list)
    # The run's own `emit`, when it has one: installs, builds and servers then land in
    # `events.jsonl` as `tool` events instead of only in this service's log.
    on_event: object | None = None
    # A model-authored server cannot be given auth, so the port is reached through the
    # preview origin's proxy, which requires this per-run secret.
    share: str = field(default_factory=lambda: secrets.token_urlsafe(18))

    @property
    def prefix(self) -> str:
        """The path a served tree lives under.

        A static tree is served by our own server, so its URLs carry the run's nonce and
        a port scan finds nothing. A model-authored app defines its own routes and cannot
        be given a prefix; `share` still gates the proxy in front of it.
        """
        return f"/{self.share}" if self.kind == "static" else ""

    @property
    def url(self) -> str | None:
        """Where a browser can reach this service, once it answers on its port."""
        if self.state != "ready" or not self.port:
            return None
        return f"http://127.0.0.1:{self.port}{self.prefix}/"

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "state": self.state,
            "kind": self.kind,
            "detail": self.detail,
            "url": self.url,
            "port": self.port,
            "token": self.share,
            "exit_code": self.exit_code,
            "pending": self.pending,
            "detail_lines": self.approval_detail,
            "sandbox": sandbox.describe(),
            "logs": self.logs[-LOG_LINES:],
        }

    def log(self, line: str) -> None:
        text = line.rstrip()
        if not text:
            return
        self.logs.append(text[:400])
        if len(self.logs) > LOG_LINES:
            del self.logs[: len(self.logs) - LOG_LINES]

    # ---------- lifecycle ----------

    async def start(self) -> None:
        self.state = "detecting"
        self.detail = "looking at what the team wrote"
        self.logs.clear()
        self.exit_code = None
        self.pending.clear()

        # The checkpoint comes before the first spawn, not at it: the person approving
        # should see every command in the pipeline (`npm install`, `npm run build`,
        # `pip install`, the server) at once. Nothing below this line has executed code
        # written by the model.
        self.approval_detail = self.details()
        self.log(sandbox.describe())
        blocked = [command for command in self.plan() if self._needs_approval(command)]
        if blocked and REQUIRE_APPROVAL and not self.approved:
            self.state = "needs_approval"
            self.pending = blocked
            self.detail = f"approval required · {len(blocked)} command(s) would run"
            self.log("approval required before: " + " | ".join(blocked))
            event_log(self.run_id, "approval-required", commands=blocked)
            return

        try:
            await self._run()
        except ApprovalRequired as exc:
            self.state = "needs_approval"
            self.pending = list(exc.commands)
            self.detail = f"approval required · {exc.commands[0]}"
            self.log(f"!! {exc}")
            event_log(self.run_id, "approval-required", commands=self.pending, reason=exc.reason)
        except RunnerError as exc:
            self._fail(str(exc))
        except asyncio.CancelledError:
            self.state = "stopped"
            raise
        except Exception as exc:  # noqa: BLE001 - the UI has to see why it died
            self._fail(f"{type(exc).__name__}: {exc}")

    def _fail(self, detail: str) -> None:
        self.state = "failed"
        self.detail = detail
        self.log(f"!! {detail}")

    @staticmethod
    def _needs_approval(command: str) -> bool:
        """True when the policy says this command may not run unapproved."""
        effect, _ = permissions.decision(command.split())
        return effect != permissions.ALLOW

    def plan(self) -> list[str]:
        """The commands this run would execute, derived without executing any of them.

        Only the shape of the tree is read — `package.json`'s scripts, whether a
        `requirements.txt` exists, which module exposes an ASGI app — so the approval
        prompt is the plan, not a guess. Ports are bound at execution time; `<free>`
        stands in for the one a step picks.
        """
        workspace = self.workspace
        python = Path(sys.executable).name
        steps: list[str] = []

        package = workspace / "package.json"
        if package.is_file():
            try:
                scripts = json.loads(package.read_text(encoding="utf-8")).get("scripts") or {}
            except (OSError, json.JSONDecodeError):
                scripts = {}
            steps.append("npm install --no-audit --no-fund --loglevel=error")
            if scripts.get("build"):
                steps.append("npm run build")
            if scripts.get("dev"):
                steps.append("npm run dev -- --port <free> --host 127.0.0.1")
            elif _index(workspace) or self._build_output():
                steps.append(f"{python} static_server.py --directory <built tree>")
            return steps

        if (workspace / "requirements.txt").is_file():
            steps.append(f"{python} -m pip install -r requirements.txt")
        module = self._asgi_module()
        if module:
            steps.append(f"{python} -m uvicorn {module}:app --host 127.0.0.1 --port <free>")
        elif _index(workspace):
            steps.append(f"{python} static_server.py --directory . --bind 127.0.0.1 --port <free>")
        return steps

    def details(self) -> list[str]:
        """The diff an approval is actually about, derived from the tree without running it.

        `npm install` is a word; `react@18.3.1` and `postinstall: node ./setup.js` are the
        facts somebody needs to decide. Only files are read here — nothing executes.
        """
        rows: list[str] = []
        package = self.workspace / "package.json"
        if package.is_file():
            try:
                body = json.loads(package.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                body = {}
            for name, version in {**body.get("dependencies", {}), **body.get("devDependencies", {})}.items():
                rows.append(f"dependency {name}@{version}")
            for name, command in (body.get("scripts") or {}).items():
                rows.append(f"script {name}: {command}")
        requirements = self.workspace / "requirements.txt"
        if requirements.is_file():
            try:
                lines = requirements.read_text(encoding="utf-8").splitlines()
            except OSError:
                lines = []
            rows += [f"package {line.strip()}" for line in lines if line.strip() and not line.startswith("#")]
        entry = _index(self.workspace)
        if entry is not None:
            rows.append(f"entry {entry.name}")
        if not rows:
            rows.append("no dependencies or scripts declared")
        return rows[:16]

    async def _run(self) -> None:
        workspace = self.workspace
        package = workspace / "package.json"
        if package.is_file():
            await self._node(package)
            return
        if (workspace / "requirements.txt").is_file() or (workspace / "pyproject.toml").is_file():
            await self._python()
            return
        if self._asgi_module():
            await self._python()
            return
        if _index(workspace):
            await self._static(workspace, "a static page, served exactly as written")
            return
        raise RunnerError("nothing here can run: no package.json, nothing ASGI, no index.html")

    # ---------- node ----------

    async def _node(self, package: Path) -> None:
        self.kind = "node"
        if shutil.which("npm") is None:
            raise RunnerError("npm is not installed on this machine")
        try:
            body = json.loads(package.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RunnerError(f"package.json is not valid JSON: {exc}") from exc
        scripts = body.get("scripts") or {}

        self.state = "installing"
        self.detail = "npm install"
        code = await self._exec(
            ["npm", "install", "--no-audit", "--no-fund", "--loglevel=error"],
            self.workspace,
            INSTALL_TIMEOUT,
        )
        if code != 0:
            raise RunnerError(f"npm install failed (exit {code}) — the log above says why")
        self.log("dependencies installed")

        if scripts.get("build"):
            self.state = "building"
            self.detail = "npm run build"
            code = await self._exec(["npm", "run", "build"], self.workspace, BUILD_TIMEOUT)
            if code == 0:
                output = self._build_output()
                if output is not None:
                    where = output.name if output != self.workspace else "."
                    await self._static(output, f"built with `npm run build`, served from {where}/")
                    return
                self.log("build finished but left no dist/, build/ or out/ with an index.html")
            else:
                self.log(f"npm run build failed (exit {code})")

        if scripts.get("dev"):
            await self._dev(str(scripts["dev"]))
            return
        if _index(self.workspace):
            await self._static(self.workspace, "no runnable build — serving the source index.html")
            return
        raise RunnerError("no build output and no dev script: nothing to serve")

    def _build_output(self) -> Path | None:
        for name in BUILD_ROOTS:
            candidate = self.workspace / name
            if candidate.is_dir() and _index(candidate):
                return candidate
        return None

    async def _dev(self, script: str) -> None:
        port = free_port()
        self.state = "starting"
        self.detail = f"npm run dev on :{port}"
        env = {"PORT": str(port), "HOST": "127.0.0.1", "BROWSER": "none"}
        if "next" in script:
            command = ["npm", "run", "dev", "--", "-p", str(port), "-H", "127.0.0.1"]
        else:
            command = ["npm", "run", "dev", "--", "--port", str(port), "--host", "127.0.0.1"]
        await self._serve(command, port, env)

    # ---------- python ----------

    async def _python(self) -> None:
        self.kind = "python"
        requirements = self.workspace / "requirements.txt"
        if requirements.is_file():
            self.state = "installing"
            self.detail = "pip install -r requirements.txt"
            code = await self._exec(
                [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q", "-r", "requirements.txt"],
                self.workspace,
                INSTALL_TIMEOUT,
            )
            if code != 0:
                self.log(f"pip install exited {code} — trying to serve anyway")

        module = self._asgi_module()
        if not module:
            raise RunnerError(
                "no ASGI app to serve: expected app.py, main.py or server.py exposing `app = FastAPI(...)`"
            )
        port = free_port()
        self.state = "starting"
        self.detail = f"uvicorn {module}:app on :{port}"
        await self._serve(
            [
                sys.executable,
                "-m",
                "uvicorn",
                f"{module}:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--app-dir",
                str(self.workspace),
            ],
            port,
            {},
        )

    def _asgi_module(self) -> str | None:
        for name in PYTHON_MODULES:
            candidate = self.workspace / f"{name}.py"
            if not candidate.is_file():
                continue
            try:
                text = candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if ASGI.search(text):
                return name
        return None

    # ---------- static ----------

    async def _static(self, root: Path, detail: str) -> None:
        """Serve a tree on its own port: a build's absolute `/assets/…` URLs need one.

        `http.server` would 404 every client-side route, so the tree is served by
        `static_server.py`, which falls back to its index.html for extensionless paths.
        """
        self.kind = "static"
        self.root = root
        port = free_port()
        try:
            where = str(root.relative_to(self.workspace)) or "."
        except ValueError:
            where = str(root)
        self.log(f"serving {where} on :{port}")
        await self._serve(
            [
                sys.executable,
                str(STATIC_SERVER),
                str(port),
                "--bind",
                "127.0.0.1",
                "--directory",
                str(root),
                # The port alone must not be enough: the tree answers only under this
                # run's nonce, so a local page cannot read it by scanning ports.
                "--prefix",
                f"/{self.share}",
            ],
            port,
            {},
            kind="static",
        )
        self.detail = f"{detail} · :{port}"

    # ---------- processes ----------

    async def _tool(
        self,
        tool: str,
        command: list[str],
        result: str,
        started: float,
        ok: bool = True,
        error: str = "",
    ) -> None:
        """Record a process this service started in the run's transcript, when there is one."""
        if self.on_event is None:
            return
        event = {
            "type": "tool",
            "tool": tool,
            "args": {"command": permissions.command_line(command)},
            "result": result,
            "ms": int((time.perf_counter() - started) * 1000),
            "ok": ok,
            "agent": "runner",
            "phase": "serving" if tool == "process.serve" else "running",
        }
        if error:
            event["error"] = error
        try:
            await self.on_event(event)  # type: ignore[operator]
        except Exception:  # noqa: BLE001 - a log line must never take a service down
            pass

    async def _exec(self, command: list[str], cwd: Path, timeout: float) -> int:
        """Run an install/build step to completion, streaming its output into the log."""
        self.log(f"$ {' '.join(command)}")
        started = time.perf_counter()
        try:
            process = await self._spawn(command, cwd)
            reader = asyncio.create_task(self._pump(process))
            try:
                try:
                    code = await asyncio.wait_for(process.wait(), timeout)
                except asyncio.TimeoutError:
                    self._signal(process, signal.SIGKILL)
                    await process.wait()
                    raise RunnerError(f"`{command[0]} {command[1] if len(command) > 1 else ''}` timed out") from None
                await reader  # drain the last lines, so a failure's reason reaches the log
            finally:
                reader.cancel()
                if process.returncode is None:
                    # A restart cancelled us mid-install: npm must not keep writing into the
                    # workspace behind the next run's back.
                    self._signal(process, signal.SIGKILL)
                    await process.wait()
        except ApprovalRequired:
            raise  # a checkpoint, not a failed tool call
        except RunnerError as exc:
            await self._tool("process.run", command, "failed", started, ok=False, error=str(exc))
            raise
        await self._tool("process.run", command, f"exit {code}", started, ok=code == 0)
        return code

    async def _spawn(
        self, command: list[str], cwd: Path, env: dict[str, str] | None = None
    ) -> asyncio.subprocess.Process:
        effect, reason = permissions.decision(command)
        if effect == permissions.DENY:
            raise RunnerError(f"refused by policy: `{permissions.command_line(command)}` — {reason}")
        if effect == permissions.ASK and REQUIRE_APPROVAL and not self.approved:
            raise ApprovalRequired(command, reason)
        if effect == permissions.ASK:
            self.log(f"approved by the operator: {permissions.command_line(command)}")
            event_log(self.run_id, "approve", command=permissions.command_line(command), reason=reason)
        # Containment, not policy: writes are limited to the workspace and caches, and
        # key material is unreadable. `prefix` returns [] where no sandbox exists.
        wrapped = [*sandbox.prefix(self.run_id, self.workspace), *command]
        try:
            return await asyncio.create_subprocess_exec(
                *wrapped,
                cwd=str(cwd),
                # Never hand credentials or harness settings to code a model wrote.
                env=permissions.safe_env({"CI": "1", "NO_COLOR": "1", "PYTHONUNBUFFERED": "1", **(env or {})}),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise RunnerError(f"{command[0]} is not installed on this machine") from exc

    async def _pump(self, process: asyncio.subprocess.Process) -> None:
        stdout = process.stdout
        if stdout is None:
            return
        while True:
            raw = await stdout.readline()
            if not raw:
                break
            self.log(raw.decode("utf-8", "replace"))

    async def _serve(self, command: list[str], port: int, env: dict[str, str], kind: str = "process") -> None:
        """Start a long-running server and wait until it actually answers."""
        self.log(f"$ {' '.join(command)}")
        started = time.perf_counter()
        self.port = port
        # Set before the probe: `prefix` depends on the kind, and a static tree answers
        # 404 outside its nonce.
        self.kind = kind
        process = await self._spawn(command, self.workspace, env=env)
        self.process = process
        self.pump = asyncio.create_task(self._pump(process))

        url = f"http://127.0.0.1:{port}{self.prefix}/"
        loop = asyncio.get_running_loop()
        deadline = loop.time() + BOOT_TIMEOUT
        while True:
            if process.returncode is not None:
                self.exit_code = process.returncode
                detail = f"the server exited with code {process.returncode} before it answered"
                await self._tool("process.serve", command, "failed", started, ok=False, error=detail)
                raise RunnerError(detail)
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    response = await client.get(url)
                if response.status_code < 500:
                    self.kind = kind
                    self.state = "ready"
                    self.detail = f"serving on :{port}"
                    self.log(f"ready on {url}")
                    await self._tool("process.serve", command, f"ready on :{port}", started)
                    return
            except httpx.HTTPError:
                pass
            if loop.time() > deadline:
                raise RunnerError(f"the server did not answer on {url} within {BOOT_TIMEOUT:.0f}s")
            await asyncio.sleep(0.7)

    def _signal(self, process: asyncio.subprocess.Process, sig: int) -> None:
        """Signal the whole process group, so npm's node child dies with it."""
        try:
            os.killpg(os.getpgid(process.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                process.send_signal(sig)
            except ProcessLookupError:
                pass

    async def stop(self) -> None:
        current = self.task
        if current and not current.done() and current is not asyncio.current_task():
            # Wait for it to unwind: a start() cancelled mid-spawn could otherwise add a
            # server after stop() returned, and a restart would leave that one running.
            current.cancel()
            await asyncio.gather(current, return_exceptions=True)
        process = self.process
        self.process = None
        if process is not None and process.returncode is None:
            self._signal(process, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), 5.0)
            except asyncio.TimeoutError:
                self._signal(process, signal.SIGKILL)
                await process.wait()
            self.exit_code = process.returncode
            self.log("stopped")
        if self.pump and not self.pump.done():
            self.pump.cancel()
        if self.state != "failed":
            self.state = "stopped"
            self.detail = "stopped"


SERVICES: dict[str, Service] = {}
LIVE = ("detecting", "installing", "building", "starting", "ready")


def get(run_id: str) -> Service | None:
    return SERVICES.get(run_id)


async def start(
    run_id: str,
    workspace: Path,
    restart: bool = False,
    approved: bool = False,
    on_event: object | None = None,
) -> Service:
    """Start (or reuse) the service for a run. Returns as soon as it is queued; poll state.

    `restart` stops what is running first, so an edited file is installed, built and
    served again instead of the ready service being handed straight back.

    `approved` is the human checkpoint: without it the pipeline stops at the first step
    the policy marks `ask` (an install, a build, a model-authored server) and reports
    `needs_approval` instead of executing that code.
    """
    service = SERVICES.get(run_id)
    if service and restart:
        await service.stop()
        # Two restarts at once both wait here; the later one must not spawn a second server.
        service = SERVICES.get(run_id)
    if service and service.state in LIVE:
        return service
    service = Service(
        run_id=run_id,
        workspace=workspace,
        state="detecting",
        detail="queued",
        approved=approved,
        on_event=on_event,
    )
    SERVICES[run_id] = service
    service.task = asyncio.create_task(service.start())
    return service


async def stop(run_id: str) -> Service | None:
    service = SERVICES.get(run_id)
    if service:
        await service.stop()
    return service


async def stop_all() -> None:
    for service in list(SERVICES.values()):
        await service.stop()
