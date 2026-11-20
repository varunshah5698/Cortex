"""Test isolation for the harness's project-level side effects.

The orchestrator deliberately writes outside a run's workspace: a row in `failures.md`, a
promoted case in `evals/regressions.jsonl`, a line of project memory, an event in
`logs/harness.log`. That is right in production and wrong in a test suite, which would
otherwise append its fake runs ("build a page", "budget-mid", "deadline", "gate") to the
real files on every run — and make the build room's log read as a product that keeps
failing when the only thing failing is a fixture that is supposed to fail.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_harness.modules import failures, memory  # noqa: E402
from ai_harness.utils import logger  # noqa: E402


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
