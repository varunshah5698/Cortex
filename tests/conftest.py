"""Test isolation for the harness's project-level side effects.

The orchestrator deliberately writes outside a run's workspace: a row in `failures.md`, a
promoted case in `evals/regressions.jsonl`, a line of project memory. That is right in
production and wrong in a test suite, which would otherwise append its fake runs ("build a
page", "budget-mid") to the real files on every run. Redirect those three paths per test.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_harness.modules import failures, memory  # noqa: E402


@pytest.fixture(autouse=True)
def isolate_project_files(tmp_path, monkeypatch):
    monkeypatch.setattr(failures, "LOG", tmp_path / "failures.md")
    monkeypatch.setattr(failures, "REGRESSIONS", tmp_path / "regressions.jsonl")
    monkeypatch.setattr(memory, "PATH", tmp_path / "memory.md")
