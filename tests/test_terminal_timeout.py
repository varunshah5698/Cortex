"""A timed-out terminal command must take its children with it.

`process.kill()` signals only the direct child. A command that forked leaves the
grandchild holding the pipe — so `communicate()` never returns, the timeout is reported
but not enforced, and the orphan outlives the run. The fix is a process group, and these
tests prove the group is what gets signalled.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_harness.tools import terminal  # noqa: E402


def test_the_child_starts_in_its_own_session_so_a_group_kill_is_possible(tmp_path, monkeypatch):
    """Read the spawn call rather than racing a real orphan: the property is the flag."""
    monkeypatch.setattr(terminal, "ENABLED", True)
    monkeypatch.setattr(terminal.permissions, "decision", lambda parts: (terminal.permissions.ALLOW, "test"))

    seen: dict = {}
    real = asyncio.create_subprocess_exec

    async def spy(*args, **kwargs):
        seen.update(kwargs)
        return await real(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    asyncio.run(terminal.run("ls", tmp_path, timeout=5))
    assert seen.get("start_new_session") is (os.name == "posix")


def test_a_command_that_outlives_its_timeout_is_killed_and_reported(tmp_path, monkeypatch):
    """`sleep` blocks without reading stdin, so the timeout path is the one exercised."""
    monkeypatch.setattr(terminal, "ENABLED", True)
    monkeypatch.setattr(terminal.permissions, "decision", lambda parts: (terminal.permissions.ALLOW, "test"))
    monkeypatch.setattr(terminal, "ALLOWED", {"sleep"})
    started = time.monotonic()
    result = asyncio.run(terminal.run("sleep 30", tmp_path, timeout=0.3))
    assert result == "[timeout]"
    # It returned on the timeout, not when the sleep ended.
    assert time.monotonic() - started < 10


def test_killing_an_already_dead_process_is_not_an_error():
    """The kill path runs after a timeout, when the child may already be gone."""

    class Gone:
        pid = 999_999_999

    # Must not raise: a process that is already gone is not a failure to report.
    terminal._kill_tree(Gone())


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - exists but not ours
        return True
    return True


@pytest.mark.skipif(os.name != "posix", reason="process groups are a POSIX property")
def test_a_grandchild_does_not_survive_the_group_kill():
    """The real regression: a shell that forks a sleeper, then a group kill.

    Without `start_new_session` on the spawn, the sleeper is in the parent's group and
    `killpg` would either miss it or take the test runner down with it.
    """

    async def scenario() -> tuple[int, int]:
        parent = await asyncio.create_subprocess_exec(
            "sh",
            "-c",
            "sleep 30 & echo $!; wait",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        line = await parent.stdout.readline()
        grandchild = int(line.strip())
        assert _alive(grandchild)
        terminal._kill_tree(parent)
        await asyncio.wait_for(parent.wait(), timeout=5)
        return parent.pid, grandchild

    parent_pid, grandchild_pid = asyncio.run(scenario())
    # Give the kernel a moment to reap, then assert both are gone.
    for _ in range(50):
        if not _alive(grandchild_pid) and not _alive(parent_pid):
            break
        time.sleep(0.05)
    assert not _alive(grandchild_pid), "the grandchild outlived the group kill"
    assert not _alive(parent_pid)


@pytest.mark.skipif(os.name != "posix", reason="signals are a POSIX property")
def test_the_group_kill_does_not_reach_the_caller():
    """`killpg` on our own group would kill the test runner; the spawn prevents that."""
    assert os.getpgid(0) != 0

    # A process started with start_new_session has a group id equal to its own pid, so a
    # killpg aimed at it cannot touch the process that spawned it.
    async def scenario() -> bool:
        child = await asyncio.create_subprocess_exec("sleep", "5", start_new_session=True)
        same_group = os.getpgid(child.pid) == os.getpgid(0)
        terminal._kill_tree(child)
        await child.wait()
        return same_group

    assert asyncio.run(scenario()) is False
    assert signal.SIGKILL  # the signal the kill path uses exists
