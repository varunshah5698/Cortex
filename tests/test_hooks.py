"""Hooks: declarative rules around every tool call, mounted at the one choke point.

The property that matters is coverage: a rule registered once must apply to a tool that
was never mentioned when the rule was written. These tests drive `tool_log.call`, which is
what every real tool call goes through, so they exercise the mount point rather than a
copy of it.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_harness.modules import hooks, tool_log  # noqa: E402


class FakeRun:
    def __init__(self) -> None:
        self.run_id = "hooks-test"
        self.events: list[dict] = []

    async def emit(self, event: dict) -> None:
        self.events.append(event)


@pytest.fixture(autouse=True)
def clean_hooks():
    """Every test starts with no rules and no config file, and leaves none behind."""
    hooks.reset()
    yield
    hooks.reset()


def test_a_deny_rule_stops_a_tool_that_was_never_named_in_the_rule():
    run = FakeRun()
    hooks.register(
        hooks.PRE,
        hooks.Rule(event=hooks.PRE, match="*", action=hooks.DENY, reason="no tools at all"),
    )

    async def body() -> None:
        async with tool_log.call(run, "some.tool.invented.later", {"path": "x"}) as call:
            call.set_result("should never run")

    with pytest.raises(hooks.Denied):
        asyncio.run(body())

    # The refusal is in the transcript before the exception, so a caller that catches it
    # still leaves a record of why the tool stopped.
    assert [event["type"] for event in run.events] == ["hook"]
    assert run.events[0]["action"] == "deny"
    assert run.events[0]["reason"] == "no tools at all"


def test_a_path_glob_denies_one_file_and_leaves_the_rest_alone():
    run = FakeRun()
    hooks.register(
        hooks.PRE,
        hooks.Rule(event=hooks.PRE, match="agent.write", path="tests/*", action=hooks.DENY, reason="frozen"),
    )

    async def allowed() -> str:
        async with tool_log.call(run, "agent.write", {"path": "src/app.ts"}) as call:
            call.set_result("wrote src/app.ts")
        return "ok"

    async def refused() -> None:
        async with tool_log.call(run, "agent.write", {"path": "tests/test_app.py"}) as call:
            call.set_result("wrote the test")

    assert asyncio.run(allowed()) == "ok"
    with pytest.raises(hooks.Denied):
        asyncio.run(refused())


def test_an_allow_placed_first_beats_a_later_deny():
    run = FakeRun()
    hooks.register(hooks.PRE, hooks.Rule(event=hooks.PRE, match="agent.read", action=hooks.ALLOW))
    hooks.register(hooks.PRE, hooks.Rule(event=hooks.PRE, match="*", action=hooks.DENY, reason="everything else"))

    async def body() -> str:
        async with tool_log.call(run, "agent.read", {"path": "index.html"}) as call:
            call.set_result("read it")
        return "ok"

    assert asyncio.run(body()) == "ok"


def test_a_post_rule_observes_the_outcome_and_cannot_change_it():
    run = FakeRun()
    hooks.register(
        hooks.POST,
        hooks.Rule(event=hooks.POST, match="agent.check", action=hooks.NOTE, text="check recorded"),
    )

    async def body() -> str:
        async with tool_log.call(run, "agent.check", {}) as call:
            call.set_result("3 issues")
        return "returned"

    assert asyncio.run(body()) == "returned"
    kinds = [event["type"] for event in run.events]
    assert kinds == ["tool", "hook"], kinds
    assert run.events[1]["event"] == hooks.POST
    assert run.events[1]["text"] == "check recorded"


def test_a_failing_tool_still_reaches_the_post_rule():
    run = FakeRun()
    hooks.register(hooks.POST, hooks.Rule(event=hooks.POST, match="*", action=hooks.NOTE))

    async def body() -> None:
        async with tool_log.call(run, "agent.edit", {"path": "a.ts"}) as call:
            call.fail("no unique match")
            raise RuntimeError("edit refused")

    with pytest.raises(RuntimeError):
        asyncio.run(body())

    post = [event for event in run.events if event["type"] == "hook"]
    assert post and post[0]["ok"] is False


def test_no_config_and_no_rules_is_inert(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(hooks, "HOOKS_FILE", tmp_path / "missing.json")
    run = FakeRun()

    async def body() -> str:
        async with tool_log.call(run, "agent.read", {"path": "index.html"}) as call:
            call.set_result("read it")
        return "ok"

    assert asyncio.run(body()) == "ok"
    assert [event["type"] for event in run.events] == ["tool"]


def test_a_malformed_config_file_is_inert_not_fatal(monkeypatch, tmp_path: Path):
    path = tmp_path / "harness.hooks.json"
    path.write_text("{not json at all", encoding="utf-8")
    monkeypatch.setattr(hooks, "HOOKS_FILE", path)
    hooks.reset()

    assert hooks.rules(hooks.PRE) == []


def test_a_config_file_supplies_rules(monkeypatch, tmp_path: Path):
    path = tmp_path / "harness.hooks.json"
    path.write_text(
        '{"PreToolUse": [{"match": "agent.write", "path": "*.lock", "action": "deny", '
        '"reason": "lockfiles are the harness\'s"}], "PostToolUse": []}',
        encoding="utf-8",
    )
    monkeypatch.setattr(hooks, "HOOKS_FILE", path)
    hooks.reset()

    rules = hooks.rules(hooks.PRE)
    assert len(rules) == 1
    assert rules[0].applies("agent.write", {"path": "package.lock"})
    assert not rules[0].applies("agent.write", {"path": "package.json"})


def test_an_unknown_action_is_dropped_rather_than_guessed(monkeypatch, tmp_path: Path):
    path = tmp_path / "harness.hooks.json"
    path.write_text(
        '{"PreToolUse": [{"match": "*", "action": "execute_shell", "reason": "nope"}]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(hooks, "HOOKS_FILE", path)
    hooks.reset()

    assert hooks.rules(hooks.PRE) == []


def test_the_table_reports_what_is_active():
    hooks.register(hooks.PRE, hooks.Rule(event=hooks.PRE, match="agent.write", action=hooks.DENY, reason="frozen"))

    rows = hooks.table()

    assert rows and rows[0]["match"] == "agent.write" and rows[0]["action"] == "deny"
