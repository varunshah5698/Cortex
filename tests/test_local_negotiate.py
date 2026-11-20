"""One local runner serves one request at a time.

Three agents negotiating at once against a single local model do not run in parallel:
they queue, and the last one can outlast the provider read timeout. The writes already
ran in order for a local roster; the contracts did not, so a local run stalled in
`contracts` while every agent showed `thinking`.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_harness.models import Specialist, WorkUnit  # noqa: E402
from ai_harness.modules import llm  # noqa: E402
from ai_harness.nodes import execution  # noqa: E402


def _local_spec(sid: str) -> Specialist:
    return Specialist(id=sid, name=sid.title(), role="frontend", model="m", baseUrl="http://127.0.0.1:11434")


def _hosted_spec(sid: str) -> Specialist:
    return Specialist(
        id=sid, name=sid.title(), role="frontend", model="m", baseUrl="https://example.test/v1", apiKey="k"
    )


def _units() -> list[WorkUnit]:
    return [
        WorkUnit(id=f"u{i}", title=f"Part {i}", role="frontend", file=f"f{i}.html", objective="x", agent=f"a{i}")
        for i in range(3)
    ]


class _Run:
    """The slice of RunState that `negotiate` touches."""

    def __init__(self, specialists: list[Specialist]) -> None:
        self.specialists = specialists
        self.prompt = "build a page"
        self.contracts: dict[str, str] = {}
        self.events: list[dict] = []

    async def emit(self, event: dict) -> None:
        self.events.append(event)

    def track_usage(self, agent, phase, usage):  # noqa: ANN001 - the RunState shape
        return {"type": "usage", "agent": agent.id, "phase": phase}


def _stub_call(monkeypatch, in_flight: list[int], peak: list[int]) -> None:
    async def fake_call(agent, messages, **kwargs):  # noqa: ANN001 - the llm.call shape
        in_flight[0] += 1
        peak[0] = max(peak[0], in_flight[0])
        await asyncio.sleep(0.02)
        in_flight[0] -= 1
        return f"contract for {agent.id}", llm.Usage()

    monkeypatch.setattr(llm, "call", fake_call)


def test_a_local_roster_negotiates_one_contract_at_a_time(monkeypatch):
    in_flight, peak = [0], [0]
    _stub_call(monkeypatch, in_flight, peak)
    run = _Run([_local_spec("a0"), _local_spec("a1"), _local_spec("a2")])

    asyncio.run(execution.negotiate(run, _units()))

    assert peak[0] == 1, f"{peak[0]} contract calls ran at once against one local runner"
    assert len(run.contracts) == 3


def test_a_hosted_roster_still_negotiates_in_parallel(monkeypatch):
    in_flight, peak = [0], [0]
    _stub_call(monkeypatch, in_flight, peak)
    run = _Run([_hosted_spec("a0"), _hosted_spec("a1"), _hosted_spec("a2")])

    asyncio.run(execution.negotiate(run, _units()))

    assert peak[0] > 1, "a hosted roster lost its parallelism"
    assert len(run.contracts) == 3
