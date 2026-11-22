"""Subagents: a fresh message list, read-only tools, and one text result back.

The two properties under test are the ones that justify the mechanism: the parent's
context gains only the answer, and a subagent cannot write. Everything else is the same
text protocol the repair loop already uses.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_harness.models import Specialist  # noqa: E402
from ai_harness.modules import subagent, thrash  # noqa: E402
from ai_harness.modules.llm import Usage  # noqa: E402
from ai_harness.tools.file_manager import FileManager  # noqa: E402


class FakeRun:
    def __init__(self, workspace: Path, files: dict[str, str]) -> None:
        self.workspace = workspace
        self.run_id = "subagent-test"
        self.prompt = "Build a counter page"
        self.files = {path: {"agent": "vega", "content": body, "done": True} for path, body in files.items()}
        self.events: list[dict] = []
        self.fm = FileManager(workspace)
        for path, body in files.items():
            self.fm.write(path, body)

    async def emit(self, event: dict) -> None:
        self.events.append(event)

    def track_usage(self, agent: Specialist, phase: str, usage: Usage) -> dict:
        return {"type": "usage", "agent": agent.id, "phase": phase, **usage.as_dict()}


class Script:
    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.messages: list[list[dict]] = []

    async def __call__(self, agent, messages, **kwargs):  # noqa: ANN001
        self.messages.append([dict(message) for message in messages])
        text = self.replies.pop(0) if self.replies else '{"tool":"answer","args":{"text":"no answer"}}'
        return text, Usage(prompt_tokens=10, completion_tokens=5, calls=1, ms=1)


def _agent() -> Specialist:
    return Specialist(id="vega", name="Vega", role="frontend", model="stub")


def test_a_subagent_reads_then_answers_and_returns_only_the_answer(tmp_path: Path, monkeypatch) -> None:
    run = FakeRun(tmp_path, {"index.html": "<button id='go'>Go</button>\n"})
    agent = _agent()
    thrash.reset(run.run_id)
    script = Script(
        [
            '{"tool":"read","args":{"path":"index.html"}}',
            '{"tool":"answer","args":{"text":"index.html:1 declares id=go and nothing queries it."}}',
        ]
    )
    monkeypatch.setattr(subagent.llm, "call", script)

    report = asyncio.run(subagent.run_subagent(run, agent, "Which id does the markup declare?"))

    assert report.complete is True
    assert "id=go" in report.text
    assert report.steps == 2
    # The parent gets the answer, not the file the subagent read.
    context = subagent.as_context(report)
    assert "Subagent finding" in context
    assert "<button" not in context


def test_the_subagent_has_its_own_message_list(tmp_path: Path, monkeypatch) -> None:
    run = FakeRun(tmp_path, {"a.txt": "alpha\n"})
    agent = _agent()
    thrash.reset(run.run_id)
    script = Script(
        [
            '{"tool":"read","args":{"path":"a.txt"}}',
            '{"tool":"answer","args":{"text":"alpha"}}',
        ]
    )
    monkeypatch.setattr(subagent.llm, "call", script)

    asyncio.run(subagent.run_subagent(run, agent, "What is in a.txt?"))

    # Turn two sees the first turn's observation, which is what makes it a conversation.
    second = script.messages[1]
    assert len(second) == 4
    assert second[-1]["role"] == "user" and "alpha" in second[-1]["content"]


def test_a_subagent_cannot_write(tmp_path: Path, monkeypatch) -> None:
    run = FakeRun(tmp_path, {"index.html": "<p>ok</p>\n"})
    agent = _agent()
    thrash.reset(run.run_id)
    script = Script(
        [
            '{"tool":"write","args":{"path":"index.html","content":"replaced"}}',
            '{"tool":"answer","args":{"text":"I could not change it."}}',
        ]
    )
    monkeypatch.setattr(subagent.llm, "call", script)

    report = asyncio.run(subagent.run_subagent(run, agent, "Replace index.html"))

    assert run.fm.read("index.html") == "<p>ok</p>\n"
    assert report.complete is True
    refused = [event for event in run.events if event.get("type") == "tool" and not event.get("ok")]
    assert refused, "the refusal must be recorded"


def test_a_provider_error_returns_what_was_learned_instead_of_raising(tmp_path: Path, monkeypatch) -> None:
    run = FakeRun(tmp_path, {"a.txt": "alpha\n"})
    agent = _agent()
    thrash.reset(run.run_id)

    async def failing(_agent, _messages, **_kwargs):
        raise subagent.llm.ProviderError("upstream is down")

    monkeypatch.setattr(subagent.llm, "call", failing)

    report = asyncio.run(subagent.run_subagent(run, agent, "What is in a.txt?"))

    assert report.complete is False
    assert report.text == ""
    assert any("provider error" in line for line in report.ledger)


def test_an_unreadable_reply_gets_one_correction_then_stops(tmp_path: Path, monkeypatch) -> None:
    run = FakeRun(tmp_path, {"a.txt": "alpha\n"})
    agent = _agent()
    thrash.reset(run.run_id)
    script = Script(["I think it is fine.", "Still just prose."])
    monkeypatch.setattr(subagent.llm, "call", script)

    report = asyncio.run(subagent.run_subagent(run, agent, "What is in a.txt?"))

    assert report.complete is False
    assert len(script.messages) == 2
    assert "not a valid step" in script.messages[1][-1]["content"]


def test_the_step_budget_is_enforced(tmp_path: Path, monkeypatch) -> None:
    run = FakeRun(tmp_path, {"a.txt": "alpha\n"})
    agent = _agent()
    thrash.reset(run.run_id)
    script = Script(['{"tool":"read","args":{"path":"a.txt"}}'] * 10)
    monkeypatch.setattr(subagent.llm, "call", script)

    report = asyncio.run(subagent.run_subagent(run, agent, "Loop forever", max_steps=3))

    assert report.complete is False
    assert report.steps == 3
    assert len(script.messages) == 3


def test_a_search_that_finds_nothing_says_so_rather_than_inventing(tmp_path: Path, monkeypatch) -> None:
    run = FakeRun(tmp_path, {"a.txt": "alpha\n"})
    agent = _agent()
    thrash.reset(run.run_id)
    script = Script(
        [
            '{"tool":"search","args":{"query":"omega"}}',
            '{"tool":"answer","args":{"text":"No match for omega in the tree."}}',
        ]
    )
    monkeypatch.setattr(subagent.llm, "call", script)

    report = asyncio.run(subagent.run_subagent(run, agent, "Where is omega?"))

    assert report.complete is True
    assert any("no literal match" in line for line in report.ledger)


def test_the_tool_specs_offer_no_writing_tool() -> None:
    names = {spec["function"]["name"] for spec in subagent.TOOL_SPECS}

    assert names == set(subagent.TOOLS)
    assert "write" not in names and "edit" not in names
