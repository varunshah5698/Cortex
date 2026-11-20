"""Restricted terminal tool. Off unless HARNESS_ALLOW_TERMINAL=1, read-only, workspace only."""

from __future__ import annotations

import asyncio
import os
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
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        return "[timeout]"
    return stdout.decode("utf-8", "replace")[:4000]
