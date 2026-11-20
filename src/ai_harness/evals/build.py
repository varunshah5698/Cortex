"""The build tier: does the tree a case describes actually compile and serve?

The scripted suite scores facts about files, and `nodes/verify.py` is static: it reads
the tree and reports contradictions, but it cannot see a TypeScript error, a missing
dependency or a config the model invented. `modules/runner.py` can, because it installs,
builds and serves — and the pipeline never calls it, so a run can be reported `done`,
with a clean verification, on a tree that would not compile.

This tier closes that gap for the evals, without touching the pipeline's verdict. It
writes a case's files into a scratch workspace (exactly what the scripted run would have
written), hands it to that same runner with the approval a person gives, and requires
two things of the result:

* the service reaches `ready` — a tree nobody can load is not a build;
* when the tree declares a build script, what answers is the built output, not
  `npm run dev`. The runner deliberately falls back to the dev server when
  `npm run build` fails or leaves nothing servable, so "a dev server answered" is
  precisely the failure this tier exists to catch.

A case opts in with `servable=True`, because not every case is meant to serve
(python-service is a module and a test, not a server). The tier is opt-in too
(`--build`), because it installs and compiles: network, a toolchain, minutes. The
scripted suite stays free, offline and instant, and this module's tests stub the runner,
so CI needs neither npm nor a port.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
import uuid
from pathlib import Path

from ..modules import runner
from ..tools.file_manager import FileManager
from .cases import Case

# A service that has stopped moving. `ready` is the only good end; `failed`,
# `needs_approval` and `stopped` fail the case, with the reason in detail and logs.
SETTLED = ("ready", "failed", "needs_approval", "stopped")
POLL_SECONDS = 0.25
LOG_TAIL = 24


def budget() -> float:
    """How long a tree may take: the runner's own three limits, plus a margin.

    Derived rather than configured, so raising `HARNESS_INSTALL_TIMEOUT` does not leave
    the tier cutting a build off before the runner itself would have given up.
    """
    return runner.INSTALL_TIMEOUT + runner.BUILD_TIMEOUT + runner.BOOT_TIMEOUT + 30.0


def write_tree(case: Case, workspace: Path) -> list[str]:
    """Write the files this case's run would have produced. Returns what was written.

    An empty body is not written: the executor drops an empty `<<<FILE>>>` section, so
    a declared file nobody wrote stays absent here too (`missing-file` is that case).
    """
    manager = FileManager(Path(workspace))
    written: list[str] = []
    for path, body in case.code.items():
        if not body.strip():
            continue
        manager.write(path, body)
        written.append(path)
    return written


def declares_build(workspace: Path) -> bool:
    """True when the tree asks for a production build (`package.json` `scripts.build`)."""
    package = Path(workspace) / "package.json"
    if not package.is_file():
        return False
    try:
        body = json.loads(package.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    scripts = body.get("scripts") if isinstance(body, dict) else None
    return bool(isinstance(scripts, dict) and scripts.get("build"))


async def _settle(service, timeout: float) -> str:
    """The state a service settled in, or "timeout" when it never did."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, timeout)
    while True:
        state = str(getattr(service, "state", ""))
        if state in SETTLED:
            return state
        if loop.time() >= deadline:
            return "timeout"
        await asyncio.sleep(POLL_SECONDS)


async def _serve(case: Case, workspace: Path, *, approved: bool, timeout: float) -> dict:
    """Run one tree through the runner and judge the service it produces."""
    run_id = f"build-{uuid.uuid4().hex[:10]}"
    started = time.perf_counter()
    service = await runner.start(run_id, Path(workspace), approved=approved)
    state = await _settle(service, timeout)
    detail = str(getattr(service, "detail", ""))
    kind = str(getattr(service, "kind", ""))
    port = getattr(service, "port", None)
    logs = list(getattr(service, "logs", []) or [])[-LOG_TAIL:]
    try:
        await runner.stop(run_id)
    finally:
        # The tier owns this id; nothing else will ever ask the runner about it again.
        runner.SERVICES.pop(run_id, None)

    built = declares_build(workspace)
    problem = ""
    if state == "timeout":
        problem = f"no ready service within {timeout:.0f}s"
    elif state == "ready" and built and kind != "static":
        # The runner fell through to `npm run dev`: the production build failed or left
        # nothing servable, and a dev server answering must not pass for a compile.
        problem = "a dev server answered, so `npm run build` produced nothing servable"
    elif state != "ready":
        problem = f"the runner settled in {state}"

    ok = state == "ready" and not problem
    return {
        "case": case.name,
        "ok": ok,
        "skipped": False,
        "state": state,
        "kind": kind,
        "declares_build": built,
        "problem": problem,
        "detail": detail,
        "port": port if ok else None,
        "ms": int((time.perf_counter() - started) * 1000),
        "logs": logs,
    }


async def check(
    case: Case,
    workspace: Path | None = None,
    *,
    approved: bool = True,
    timeout: float | None = None,
) -> dict:
    """Build and serve this case's tree; report whether it compiled and answered.

    Without a `workspace`, the tree is written into a scratch directory that is removed
    afterwards, so the tier leaves nothing behind — no `node_modules`, no `dist/` in the
    repository. Passing one writes the case's files there instead, which is what the
    tests do.
    """
    if not case.servable:
        return {
            "case": case.name,
            "ok": None,
            "skipped": True,
            "state": "skipped",
            "kind": "",
            "declares_build": False,
            "problem": "",
            "detail": "this case's tree is not meant to serve",
            "port": None,
            "ms": 0,
            "logs": [],
        }

    limit = budget() if timeout is None else timeout
    if workspace is not None:
        write_tree(case, workspace)
        return await _serve(case, Path(workspace), approved=approved, timeout=limit)

    with tempfile.TemporaryDirectory(prefix="cortex-build-") as scratch:
        write_tree(case, Path(scratch))
        return await _serve(case, Path(scratch), approved=approved, timeout=limit)
