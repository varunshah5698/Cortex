"""Chat compaction: a long conversation is made to fit instead of overflowing.

`modules/compact.py` had its own tests but nothing called it, so the harness could still
reach a provider 400 on a long chat. The claim under test here is the wiring: `chat.fit`
compacts, reports what it cost as an event, and leaves a short conversation untouched.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_harness.core import chat  # noqa: E402
from ai_harness.modules import compact  # noqa: E402


class FakeRun:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def emit(self, event: dict) -> None:
        self.events.append(event)


def _turns(count: int, size: int) -> list[dict]:
    return [{"role": "user" if index % 2 == 0 else "assistant", "content": "x" * size} for index in range(count)]


def test_a_short_conversation_is_untouched_and_says_nothing():
    run = FakeRun()
    turns = _turns(4, 100)

    out = asyncio.run(chat.fit(run, turns, window=128000))

    assert out == turns
    assert run.events == []


def test_an_oversized_conversation_is_compacted_and_reported():
    run = FakeRun()
    turns = _turns(40, 4000)

    out = asyncio.run(chat.fit(run, turns, window=8000))

    assert len(out) < len(turns)
    assert run.events, "a compaction a person cannot see is a memory bug"
    assert run.events[0]["type"] == "compact"
    assert run.events[0]["before_tokens"] > run.events[0]["after_tokens"]


def test_the_newest_turns_always_survive():
    """The most recent exchange is what the next answer is about."""
    run = FakeRun()
    turns = _turns(40, 4000)
    newest = turns[-1]["content"]

    out = asyncio.run(chat.fit(run, turns, window=8000))

    assert any(message["content"] == newest for message in out)


def test_compaction_is_off_when_the_setting_is_off(monkeypatch):
    run = FakeRun()
    turns = _turns(40, 4000)
    monkeypatch.setattr(compact, "COMPACT_ENABLED", False)

    out = asyncio.run(chat.fit(run, turns, window=8000))

    assert out == turns
    assert run.events == []


def test_the_event_carries_what_was_dropped_not_only_that_something_was():
    run = FakeRun()
    turns = _turns(40, 4000)

    asyncio.run(chat.fit(run, turns, window=8000))

    payload = run.events[0]
    assert "snipped" in payload
    assert "dropped_turns" in payload
    assert payload["notes"]
