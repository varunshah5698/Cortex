"""Restricted terminal tool. Off unless HARNESS_ALLOW_TERMINAL=1, read-only, workspace only."""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path

from ..modules import permissions

ALLOWED = {"ls", "cat", "wc", "head", "tail", "tree", "grep"}
ENABLED = os.getenv("HARNESS_ALLOW_TERMINAL", "0") == "1"


class TerminalDenied(RuntimeError):
    pass


async def run(command: str, workspace: Path, timeout: float = 10.0) -> str:
    """Run a read-only inspection command inside the workspace."""
    if not ENABLED:
        raise TerminalDenied("terminal tool is disabled (set HARNESS_ALLOW_TERMINAL=1 to allow read-only use)")
    parts = command.split()
    if not parts or parts[0] not in ALLOWED:
        raise TerminalDenied(f"command not allowed: {parts[:1]}")
    if any(token in command for token in ("..", ">", "|", ";", "&&", "$(")):
        raise TerminalDenied("shell metacharacters are not allowed")
    # The allowlist above is this tool's own shape; the shared policy is the guardrail.
    effect, reason = permissions.decision(parts)
    if effect != permissions.ALLOW:
        raise TerminalDenied(f"refused by policy ({effect}): {reason}")

    process = await asyncio.create_subprocess_exec(
        *parts,
        cwd=str(workspace),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        # Its own session, so a timeout can kill the whole tree. `process.kill()` signals
        # only the direct child: a `grep` that spawned a helper, or a shell that forked,
        # leaves the grandchild holding the pipe and `communicate()` never returns. The
        # tool then reports a timeout it cannot enforce, and the orphan outlives the run.
        start_new_session=os.name == "posix",
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        _kill_tree(process)
        # `communicate()` was cancelled by the wait. Reap the child so it does not stay a
        # zombie and the transport's pipe is closed before the tool returns.
        try:
            await process.wait()
        except (ProcessLookupError, OSError):
            pass
        return "[timeout]"
    return stdout.decode("utf-8", "replace")[:4000]


def _kill_tree(process) -> None:
    """Kill the process *group*, then drain the pipe so nothing is left blocked."""
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        else:
            process.kill()
    except (ProcessLookupError, PermissionError, OSError):
        # Already gone, or not ours to signal. Either way the child is not running.
        pass
