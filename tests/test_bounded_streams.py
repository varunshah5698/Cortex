"""The pipeline may not hang: every stream is bounded, and reasoning is not replayed token by token.

These are the regression tests for the run that sat in `reviewing` for 46 minutes with no
terminal event. Three separate defects produced that one symptom, so each gets its own
test rather than one test for the symptom:

1. `llm.stream` checked the run deadline *before* a call and never during one, so a stream
   that stalled or a model that would not stop held the run open until the wall-clock limit.
2. The reviewer's reasoning stream had no character budget, unlike the author's, so it was
   the one unbounded consumer left in the pipeline.
3. Every reasoning token became its own event, so one review wrote 8,000 events and pushed
   8,000 SSE messages.

A test that only asserted "the run finishes" would pass with any one of the three fixed.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import ai_harness.core.state as state_mod  # noqa: E402
from ai_harness.agents import reviewer as reviewer_agent  # noqa: E402
from ai_harness.core.state import RunState  # noqa: E402
from ai_harness.models import Specialist  # noqa: E402
from ai_harness.modules import llm  # noqa: E402


def _run(tmp_path: Path, monkeypatch) -> RunState:
    monkeypatch.setattr(state_mod, "RUNS_ROOT", tmp_path)
    agent = Specialist(id="lyra", name="Lyra", role="reviewer", model="stub")
    return RunState(run_id="bounded", prompt="build a page", specialists=[agent], depth="quick", fresh=False)


# ---------- 1. a stream that outlives its bound is cut ----------


def test_a_stream_that_outlives_its_bound_is_cut_not_awaited(tmp_path: Path, monkeypatch) -> None:
    """The call must return, and it must say the answer is a prefix."""
    run = _run(tmp_path, monkeypatch)
    monkeypatch.setattr(llm, "STREAM_SECONDS", 0.05)
    monkeypatch.setattr(llm, "runtime", _bound(run))

    async def endless(spec, messages, **kwargs):  # noqa: ANN001
        for _ in range(10_000):
            yield ("think", "still thinking ")
            await asyncio.sleep(0.01)

    monkeypatch.setattr(llm, "_adapter_stream", endless)

    async def body() -> llm.StreamStats:
        stats = llm.StreamStats()
        async for _kind, _text in llm.stream(run.specialists[0], [{"role": "user", "content": "x"}], stats=stats):
            pass
        return stats

    started = time.perf_counter()
    stats = asyncio.run(body())
    elapsed = time.perf_counter() - started

    assert elapsed < 5, f"the stream was not cut: {elapsed:.1f}s"
    assert stats.truncated is True, "a cut stream must be marked as a prefix, not a complete answer"
    assert any(event.get("type") == "stream-cut" for event in run.events), run.events


def test_a_stream_inside_its_bound_is_not_cut(tmp_path: Path, monkeypatch) -> None:
    run = _run(tmp_path, monkeypatch)
    monkeypatch.setattr(llm, "STREAM_SECONDS", 30.0)
    monkeypatch.setattr(llm, "runtime", _bound(run))

    async def short(spec, messages, **kwargs):  # noqa: ANN001
        yield ("token", "APPROVED")

    monkeypatch.setattr(llm, "_adapter_stream", short)

    async def body() -> llm.StreamStats:
        stats = llm.StreamStats()
        async for _kind, _text in llm.stream(run.specialists[0], [{"role": "user", "content": "x"}], stats=stats):
            pass
        return stats

    stats = asyncio.run(body())

    assert stats.truncated is False
    assert not any(event.get("type") == "stream-cut" for event in run.events)


# ---------- 2. the reviewer's reasoning is bounded ----------


def test_the_reviewer_stops_narrating_and_files_its_findings(tmp_path: Path, monkeypatch) -> None:
    run = _run(tmp_path, monkeypatch)
    monkeypatch.setattr(reviewer_agent, "REVIEW_THINK_CHARS", 200)

    async def endless(spec, messages, **kwargs):  # noqa: ANN001
        # `llm.stream` is what records content into `stats`; a fake that replaces it has
        # to do the same, or the reviewer's findings would vanish for the wrong reason.
        stats = kwargs.get("stats")
        for _ in range(5_000):
            yield ("think", "reading another file ")
        if stats is not None:
            stats.content.append("APPROVED")
        yield ("token", "APPROVED")

    monkeypatch.setattr(reviewer_agent.llm, "stream", endless)

    seen: list[str] = []

    async def on_think(text: str) -> None:
        seen.append(text)

    findings, _usage = asyncio.run(
        reviewer_agent.review(run.specialists[0], "build a page", {"index.html": {"content": "<html>"}}, on_think)
    )

    emitted = "".join(seen)
    assert len(emitted) < 2_000, f"the review stream was not bounded: {len(emitted)} chars"
    assert "budget reached" in emitted
    # The verdict still reaches the caller: a bounded stream is not a lost answer.
    assert findings == "APPROVED"


# ---------- 3. reasoning is batched, not one event per token ----------


def test_reasoning_deltas_are_coalesced_into_few_events(tmp_path: Path, monkeypatch) -> None:
    run = _run(tmp_path, monkeypatch)
    monkeypatch.setattr(state_mod, "THINK_BATCH_CHARS", 100)
    monkeypatch.setattr(state_mod, "THINK_BATCH_MS", 0)

    async def body() -> None:
        for _ in range(1_000):
            await run.emit({"type": "think", "agent": "lyra", "delta": "abcde"})
        await run.emit({"type": "message", "from": "lyra", "text": "done"})

    asyncio.run(body())

    thinks = [event for event in run.events if event.get("type") == "think"]
    assert len(thinks) < 100, f"1000 deltas became {len(thinks)} events; they should be batched"
    # Nothing is lost: the batched text is the concatenation of the deltas.
    assert "".join(str(event.get("delta") or "") for event in thinks) == "abcde" * 1_000


def test_a_non_think_event_flushes_the_buffer_first(tmp_path: Path, monkeypatch) -> None:
    """Ordering in the log must still match what happened."""
    run = _run(tmp_path, monkeypatch)
    monkeypatch.setattr(state_mod, "THINK_BATCH_CHARS", 10_000)
    monkeypatch.setattr(state_mod, "THINK_BATCH_MS", 10_000)

    async def body() -> None:
        await run.emit({"type": "think", "agent": "lyra", "delta": "reasoning"})
        await run.emit({"type": "message", "from": "lyra", "text": "finding"})

    asyncio.run(body())

    kinds = [event.get("type") for event in run.events]
    assert kinds == ["think", "message"], kinds
    assert run.events[0]["delta"] == "reasoning"


def test_two_agents_reasoning_at_once_do_not_interleave(tmp_path: Path, monkeypatch) -> None:
    run = _run(tmp_path, monkeypatch)
    monkeypatch.setattr(state_mod, "THINK_BATCH_CHARS", 10_000)
    monkeypatch.setattr(state_mod, "THINK_BATCH_MS", 10_000)

    async def body() -> None:
        await run.emit({"type": "think", "agent": "a", "delta": "aaa"})
        await run.emit({"type": "think", "agent": "b", "delta": "bbb"})
        await run.flush()

    asyncio.run(body())

    by_agent = {event["agent"]: event["delta"] for event in run.events if event.get("type") == "think"}
    assert by_agent == {"a": "aaa", "b": "bbb"}


def test_a_terminal_event_does_not_leave_reasoning_stranded(tmp_path: Path, monkeypatch) -> None:
    run = _run(tmp_path, monkeypatch)
    monkeypatch.setattr(state_mod, "THINK_BATCH_CHARS", 10_000)
    monkeypatch.setattr(state_mod, "THINK_BATCH_MS", 10_000)

    async def body() -> None:
        await run.emit({"type": "think", "agent": "lyra", "delta": "last thought"})
        await run.emit({"type": "done", "files": []})

    asyncio.run(body())

    assert run.events[-1]["type"] == "done"
    assert any(event.get("type") == "think" for event in run.events), "the buffered thought must be written"


def _bound(run: RunState):
    """A stand-in for `modules.runtime` that binds this run and emits into it."""

    class Bound:
        @staticmethod
        def current_budget():
            return None

        @staticmethod
        def check_deadline() -> None:
            return None

        @staticmethod
        def current_run():
            return run

        @staticmethod
        async def emit(event: dict) -> None:
            await run.emit(event)

    return Bound
