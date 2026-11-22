"""The workshop's `bash` tool, contained.

`bash_tool.go` in ghuntley/how-to-build-a-coding-agent runs `bash -c <anything>` with the
owner's full access. That is the right shape for a workshop and the wrong one for a
harness that executes a model's commands, so this module keeps the capability and
replaces the trust:

* **The command is judged before it runs.** `permissions.decision` is consulted on the
  argv, and only `ALLOW` proceeds. An `ASK` is refused rather than silently approved,
  because a repair loop has nobody at the keyboard to ask — the approval checkpoint that
  makes `npm install` safe in `modules/runner.py` does not exist here.
* **A pipeline is not one command.** `ls; rm -rf ~` is not `ls`, so the tool refuses the
  metacharacters that would turn one judged argv into several executed commands, and then
  runs the argv directly with no shell in between. This is the same rule
  `tools/terminal.py` already applies, for the same reason.
* **The child is contained.** `sandbox.prefix` wraps it, so writes stay inside the run
  workspace and key stores are unreadable; `permissions.safe_env` strips provider keys
  and `HARNESS_*` from its environment.
* **It is off unless asked for.** `HARNESS_AGENT_SHELL=1` enables it, and `available()`
  additionally requires a real containment mechanism. Where there is no sandbox the tool
  reports that it is unavailable instead of running unprotected — the harness never
  describes an unsandboxed process as contained.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import signal
from pathlib import Path

from ..config.settings import AGENT_SHELL
from . import permissions, sandbox

# One source of truth: `config/settings.py` reads the environment (and `.env`), so the
# tool and the rest of the harness cannot disagree about whether it is on.
ENABLED = AGENT_SHELL
TIMEOUT = 20.0
MAX_OUTPUT = 4000

# One judged argv must equal one executed command, or the policy is a formality.
SHELL_METACHARACTERS = ("|", "&", ";", ">", "<", "`", "$(", "\n")


class ShellDenied(RuntimeError):
    """The tool refused the call. The message is an observation, not a crash."""


def available() -> bool:
    """True only when the tool is enabled *and* a real containment mechanism exists."""
    return ENABLED and sandbox.available()


def unavailable_reason() -> str:
    """Why the tool cannot run here, in words a model can act on."""
    if available():
        return "the shell tool is available"
    if not ENABLED:
        return "the shell tool is off; set HARNESS_AGENT_SHELL=1 to enable it"
    return f"the shell tool needs containment, and {sandbox.describe()}"


def argv_for(command: str) -> list[str]:
    """Parse one command into argv, refusing anything that would smuggle in a second."""
    if not command.strip():
        raise ShellDenied("command is empty")
    for token in SHELL_METACHARACTERS:
        if token in command:
            raise ShellDenied(f"shell metacharacter {token!r} is not allowed; run one command at a time")
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise ShellDenied(f"could not parse the command: {exc}") from exc
    if not argv:
        raise ShellDenied("command is empty")
    return argv


async def run(command: str, workspace: Path, run_id: str, timeout: float = TIMEOUT) -> str:
    """Run one allow-listed command in the run workspace and return its output."""
    if not available():
        raise ShellDenied(unavailable_reason())
    argv = argv_for(command)
    effect, reason = permissions.decision(argv)
    if effect != permissions.ALLOW:
        raise ShellDenied(f"refused by policy ({effect}): {reason}")

    try:
        wrapped = [*sandbox.prefix(run_id, workspace), *argv]
    except sandbox.SandboxError as exc:
        # The tool promised containment; it may not run the command without it.
        raise ShellDenied(f"the sandbox profile could not be prepared: {exc}") from exc
    process = await asyncio.create_subprocess_exec(
        *wrapped,
        cwd=str(workspace),
        # A model's command must never read the harness's own stdin: `cat` with no
        # arguments would block until the timeout instead of answering.
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=permissions.safe_env({"CI": "1", "NO_COLOR": "1"}),
        # Its own session, so a timeout can kill the whole tree. `process.kill()` signals
        # only the direct child, and `sandbox.prefix` may wrap the command in another
        # process: killing the wrapper would leave the command running.
        start_new_session=os.name == "posix",
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        _kill_tree(process)
        # `communicate()` was cancelled by the wait; reaping here closes the transport and
        # stops the child becoming a zombie the run never collects.
        try:
            await process.wait()
        except (ProcessLookupError, OSError):
            pass
        return f"[timeout after {timeout:.0f}s]"

    text = stdout.decode("utf-8", "replace").strip()
    if len(text) > MAX_OUTPUT:
        text = text[:MAX_OUTPUT] + f"\n…[truncated at {MAX_OUTPUT} chars]"
    code = process.returncode
    if code != 0:
        return f"exit {code}\n{text}" if text else f"exit {code} (no output)"
    return text or "(no output)"


def _kill_tree(process) -> None:
    """Kill the process *group*, falling back to the direct child off POSIX."""
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        else:
            process.kill()
    except (ProcessLookupError, PermissionError, OSError):
        # Already gone, or not ours to signal. Either way it is not running.
        pass
