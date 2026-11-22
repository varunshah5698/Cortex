"""The budget must charge every call, including the ones a consumer cuts short.

`executor._bounded_stream` breaks out of `llm.stream` when a model will not stop
reasoning. `llm.stream` settles the budget in a `finally`, and an async generator's
`finally` runs on close, not on `break` — so the spend for every cut-short call was
silently dropped. A run could then exceed its own cap by exactly the amount it had been
cut short, which is the one direction a budget must never be wrong in.

No provider is called: `_adapter_stream` is replaced with a generator that reports a
fixed usage and yields more text than the consumer will accept.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_harness.agents import executor  # noqa: E402
from ai_harness.models import Specialist  # noqa: E402
from ai_harness.modules import budget as pricing  # noqa: E402
from ai_harness.modules import llm, runtime  # noqa: E402

SPEC = Specialist(id="vega", name="Vega", role="frontend", model="scripted", baseUrl="http://127.0.0.1:9/v1")
PROMPT_TOKENS = 100
COMPLETION_TOKENS = 50


def _fake_stream(chunks: int = 50):
    """A provider stream that reports its usage and then keeps talking."""

    async def stream(spec, messages, **kwargs):  # noqa: ANN001, ANN003
        stats = kwargs.get("stats")
        stats.usage.prompt_tokens = PROMPT_TOKENS
        stats.usage.completion_tokens = COMPLETION_TOKENS
        for _ in range(chunks):
            yield ("think", "x" * 10)

    return stream


class _Run:
    def __init__(self, budget: pricing.Budget) -> None:
        self.budget = budget


def test_a_stream_cut_short_still_charges_the_budget(monkeypatch) -> None:
    budget = pricing.Budget(max_tokens=10_000_000)
    monkeypatch.setattr(llm, "_adapter_stream", _fake_stream())
    token = runtime.RUN.set(_Run(budget))
    try:

        async def consume() -> llm.StreamStats:
            stats = llm.StreamStats()
            stream = llm.stream(SPEC, [{"role": "user", "content": "hi"}], stats=stats)
            try:
                seen = 0
                async for _kind, text in stream:
                    seen += len(text)
                    if seen > 20:
                        break
            finally:
                await stream.aclose()
            return stats

        stats = asyncio.run(consume())
    finally:
        runtime.RUN.reset(token)

    spent = stats.usage.prompt_tokens + stats.usage.completion_tokens
    assert spent == PROMPT_TOKENS + COMPLETION_TOKENS
    assert budget.tokens == spent, "the budget dropped the spend for a stream the caller cut short"
    assert budget.calls == 1


def test_bounded_stream_closes_its_generator_so_the_spend_lands(monkeypatch) -> None:
    """The real consumer: `_bounded_stream` breaks on the reasoning limit.

    This is the regression that mattered. It asserts on the budget, not on the stats,
    because the stats were always right — it was the ledger that lost the charge.
    """
    budget = pricing.Budget(max_tokens=10_000_000)
    monkeypatch.setattr(llm, "_adapter_stream", _fake_stream())
    token = runtime.RUN.set(_Run(budget))
    try:

        async def consume() -> llm.StreamStats:
            async def on_think(_text: str) -> None:
                return None

            return await executor._bounded_stream(SPEC, [{"role": "user", "content": "hi"}], on_think, limit=20)

        stats = asyncio.run(consume())
    finally:
        runtime.RUN.reset(token)

    spent = stats.usage.prompt_tokens + stats.usage.completion_tokens
    assert budget.tokens == spent, "_bounded_stream broke out without closing the stream; the spend was lost"
    assert budget.calls == 1


def test_a_fully_consumed_stream_still_charges_once(monkeypatch) -> None:
    """The fix must not double-charge the ordinary path."""
    budget = pricing.Budget(max_tokens=10_000_000)
    monkeypatch.setattr(llm, "_adapter_stream", _fake_stream(chunks=3))
    token = runtime.RUN.set(_Run(budget))
    try:

        async def consume() -> llm.StreamStats:
            stats = llm.StreamStats()
            async for _kind, _text in llm.stream(SPEC, [{"role": "user", "content": "hi"}], stats=stats):
                pass
            return stats

        stats = asyncio.run(consume())
    finally:
        runtime.RUN.reset(token)

    spent = stats.usage.prompt_tokens + stats.usage.completion_tokens
    assert budget.tokens == spent
    assert budget.calls == 1, "a fully consumed stream was charged more than once"
