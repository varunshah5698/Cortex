"""Test isolation for the harness's project-level side effects.

The orchestrator deliberately writes outside a run's workspace: a row in `failures.md`, a
promoted case in `evals/regressions.jsonl`, a line of project memory, an event in
`logs/harness.log`. That is right in production and wrong in a test suite, which would
otherwise append its fake runs ("build a page", "budget-mid", "deadline", "gate") to the
real files on every run — and make the build room's log read as a product that keeps
failing when the only thing failing is a fixture that is supposed to fail.

The same argument applies to the network, and it took a flaky test to notice. A node that
reaches a provider through a seam a test forgot to stub does not fail loudly: it makes a
real request, retries it five times with backoff, and returns whatever the live model said.
The test then asserts against a stranger's output. `test_review.py` did exactly this —
twenty HTTPS connections to a live endpoint from a test named `without_network` — and it
passed or failed depending on what that endpoint answered. AGENTS.md rule 7 says tests
stub the provider; this fixture is what enforces it instead of trusting it.
"""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_harness.modules import failures, memory  # noqa: E402
from ai_harness.utils import logger  # noqa: E402

# Loopback is how a test talks to its own machine: the preview server, the CDP port a
# headless Chrome listens on, a local FastAPI app. That is not the internet and blocking
# it would break the browser and cloud-runner suites for no safety gain.
LOOPBACK = {"127.0.0.1", "::1", "localhost", "0.0.0.0", ""}

# The one deliberate exception: the live hosted-model test is opt-in by environment, and
# it exists precisely to touch a real provider. It says so in its own skipif.
LIVE_OPT_IN = "HARNESS_LIVE_FREE"


def _is_loopback(address) -> bool:
    if isinstance(address, (str, bytes)):
        host = address.decode() if isinstance(address, bytes) else address
    elif isinstance(address, tuple) and address:
        host = str(address[0])
    else:
        return False
    return host in LOOPBACK or host.startswith("127.") or host.startswith("/")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """A test may reach this machine; it may not reach the internet.

    A blocked connection raises the same `OSError` a refused socket does, so a test that
    forgot a stub fails on its own assertion in milliseconds instead of spending two
    minutes in provider retries and then reporting whatever the live model happened to
    say. The message names the seam, because the fix is always the same: stub it.
    """
    if os.getenv(LIVE_OPT_IN) == "1":
        yield
        return

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def guard(self, address, *args, **kwargs):  # noqa: ANN001
        if _is_loopback(address):
            return real_connect(self, address, *args, **kwargs)
        raise OSError(
            f"test reached the network at {address!r}. A test must stub the provider seam "
            f"(llm.call / llm.call_tools / the adapter) rather than call one — AGENTS.md rule 7. "
            f"Set {LIVE_OPT_IN}=1 only for the opt-in live test."
        )

    def guard_ex(self, address, *args, **kwargs):  # noqa: ANN001
        if _is_loopback(address):
            return real_connect_ex(self, address, *args, **kwargs)
        raise OSError(f"test reached the network at {address!r}; stub the provider seam instead")

    monkeypatch.setattr(socket.socket, "connect", guard)
    monkeypatch.setattr(socket.socket, "connect_ex", guard_ex)
    yield


@pytest.fixture(autouse=True)
def isolate_project_files(tmp_path, monkeypatch):
    monkeypatch.setattr(failures, "LOG", tmp_path / "failures.md")
    monkeypatch.setattr(failures, "REGRESSIONS", tmp_path / "regressions.jsonl")
    monkeypatch.setattr(memory, "PATH", tmp_path / "memory.md")
    monkeypatch.setattr(memory, "LESSONS", tmp_path / "lessons.md")
    # The event log is the fourth project-level file. Without this the suite's
    # deliberately-broken runs land in `logs/harness.log`, the same file the build room
    # shows, so expected test output reads as recurring product failures.
    monkeypatch.setattr(logger, "LOGS_ROOT", tmp_path / "logs")
