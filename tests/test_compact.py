"""Context compaction: cheapest tier first, and nothing dropped silently.

The failure this guards is a request that outgrows the window and dies at the provider
with an unreadable 400. The tests check the order of the tiers, that the protected tail
survives, and that a compaction which removes material says so.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_harness.modules import compact  # noqa: E402


def _turn(role: str, text: str) -> dict:
    return {"role": role, "content": text}


def _conversation(turns: int, size: int = 400) -> list[dict]:
    messages = [_turn("system", "You are a specialist.")]
    for index in range(turns):
        messages.append(_turn("user", f"question {index} " + "x" * size))
        messages.append(_turn("assistant", f"answer {index} " + "y" * size))
    return messages


def test_a_request_that_fits_is_returned_untouched():
    messages = _conversation(2)

    result = compact.compact(messages, window=200_000)

    assert result.changed is False
    assert result.messages == messages
    assert result.after_tokens == result.before_tokens


def test_oversized_bodies_are_clipped_before_any_turn_is_dropped():
    messages = _conversation(6, size=40_000)

    result = compact.compact(messages, window=60_000)

    assert result.snipped > 0
    assert result.dropped_turns == 0, "the cheap tier must run before the expensive one"
    assert result.after_tokens < result.before_tokens
    assert any("clipped" in note for note in result.notes)


def test_a_clipped_body_says_it_was_clipped():
    # The newest four messages are protected, so the oversized body has to sit before
    # them for tier 1 to touch it at all.
    messages = [_turn("system", "s"), _turn("user", "A" * 20_000)]
    messages += [_turn("assistant", f"note {i}") for i in range(4)]

    result = compact.compact(messages, window=2_000)

    body = result.messages[1]["content"]
    assert "compacted" in body
    assert body.startswith("A"), "the head of the result is kept"
    assert body.endswith("A"), "the tail of the result is kept too"
    assert result.messages[-1] == messages[-1], "the protected tail is untouched"


def test_the_system_prompt_and_the_newest_turns_always_survive():
    messages = _conversation(12, size=3_000)

    result = compact.compact(messages, window=6_000)

    assert result.messages[0]["role"] == "system"
    assert result.messages[0]["content"] == "You are a specialist."
    assert result.dropped_turns > 0
    # The newest turn is the one the model is answering; losing it would be a bug.
    assert result.messages[-1] == messages[-1]


def test_the_summary_is_only_asked_for_when_the_cheap_tiers_were_not_enough():
    asked: list[list[dict]] = []

    def summarise(dropped: list[dict]) -> str:
        asked.append(dropped)
        return "Earlier: the person asked for a counter page."

    messages = _conversation(30, size=2_000)
    result = compact.compact(messages, window=1_000, summarise=summarise)

    assert asked, "the summariser should have been called"
    assert result.summarised is True
    assert any("Earlier: the person asked" in str(message.get("content")) for message in result.messages)


def test_a_failing_summariser_does_not_fail_the_call():
    def summarise(dropped: list[dict]) -> str:
        raise RuntimeError("provider is down")

    messages = _conversation(30, size=2_000)
    result = compact.compact(messages, window=1_000, summarise=summarise)

    assert result.summarised is False
    assert any("summary failed" in note for note in result.notes)
    assert result.messages, "the request still has to be sendable"


def test_without_a_summariser_the_loss_is_reported_not_hidden():
    messages = _conversation(30, size=2_000)

    result = compact.compact(messages, window=1_000)

    assert result.dropped_turns > 0
    assert result.summarised is False
    assert any("dropped" in note for note in result.notes)


def test_compaction_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(compact, "COMPACT_ENABLED", False)
    messages = _conversation(40, size=4_000)

    result = compact.compact(messages, window=1_000)

    assert result.changed is False
    assert result.messages == messages


def test_describe_states_the_cost_and_the_saving():
    messages = _conversation(20, size=3_000)
    result = compact.compact(messages, window=6_000)

    line = compact.describe(result)

    assert "compacted" in line and "tokens" in line
    assert compact.describe(compact.compact(messages, window=200_000)) == "context fit; nothing compacted"


def test_over_budget_reads_the_same_threshold_compact_uses():
    messages = _conversation(4, size=1_000)

    assert compact.over_budget(messages, window=10_000_000) is False
    assert compact.over_budget(messages, window=1_000) is True
