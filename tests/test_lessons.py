"""A failure becomes a rule, once, and is visible to the next plan.

The harness had a failure log and a run history; neither changed the next prompt. Lessons
are the bridge: verification codes map to durable rules, deduplicated, injected into the
planner and the per-file reasoning.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_harness.modules import memory  # noqa: E402


def test_a_verification_failure_becomes_a_durable_rule(monkeypatch, tmp_path):
    lessons = tmp_path / "lessons.md"
    monkeypatch.setattr(memory, "LESSONS", lessons)
    report = {"issues": [{"code": "missing_id"}, {"code": "syntax_error"}, {"code": "other"}]}

    learned = memory.learn(report)
    assert len(learned) == 2
    assert "Declare every id" in memory.lessons()

    memory.learn(report)  # same failure twice must not duplicate the rule
    text = lessons.read_text(encoding="utf-8")
    assert text.count("Declare every id") == 1
    assert text.count("must parse") == 1
