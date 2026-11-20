"""The review stage must survive its own fix loop with no network.

This is the regression guard for the reviewer → author → rewrite hand-off: every
model call is stubbed, so a broken signature or a missing attribute fails here
instead of halfway through a real run.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_harness.agents import executor  # noqa: E402
from ai_harness.models import Specialist, WorkUnit  # noqa: E402
from ai_harness.modules.llm import Usage  # noqa: E402
from ai_harness.nodes import review  # noqa: E402

FINDINGS = "- index.html: the <h1> is never closed. Fix the markup."


class FakeFM:
    def __init__(self) -> None:
        self.written: dict[str, str] = {}

    def write(self, path: str, content: str) -> int:
        self.written[path] = content
        return content.count("\n") + 1

    def can_write(self, path: str) -> bool:
        return ".." not in path and not path.startswith("/")


class FakeRun:
    def __init__(self, specialists: list[Specialist], files: dict) -> None:
        self.specialists = specialists
        self.files = files
        self.prompt = "A coffee shop landing page"
        self.contracts = {"vega": "index.html owns the heading"}
        self.events: list[dict] = []
        self.fm = FakeFM()

    async def emit(self, event: dict) -> None:
        self.events.append(event)

    def track_usage(self, agent: Specialist, phase: str, usage: Usage) -> dict:
        return {"type": "usage", "agent": agent.id, "phase": phase, **usage.as_dict()}


async def _fake_call(agent: Specialist, messages: list[dict], **kwargs):  # noqa: ANN001
    return FINDINGS, Usage(prompt_tokens=10, completion_tokens=5, ms=12, calls=1)


async def _fake_stream(agent: Specialist, messages: list[dict], think: bool = False, stats=None):  # noqa: ANN001
    yield ("think", "reading every file")
    stats.content.append(FINDINGS)
    yield ("content", FINDINGS)


async def _fake_compose(agent: Specialist, messages: list[dict], **kwargs):  # noqa: ANN001
    files = [
        (
            "index.html",
            '<html><head><link rel="stylesheet" href="style.css"></head>'
            '<body><h1 class="title">Coffee</h1></body></html>\n',
        ),
        ("style.css", ".title { font-size: 1.5rem; }\n"),
    ]
    return files, Usage(prompt_tokens=20, completion_tokens=9, ms=18, calls=1)


def test_review_revises_without_network(tmp_path: Path) -> None:
    author = Specialist(id="vega", name="Vega", role="frontend", model="stub")
    checker = Specialist(id="lyra", name="Lyra", role="reviewer", model="stub")
    unit = WorkUnit(id="u1", title="Page shell", role="frontend", file="index.html", objective="hero", agent="vega")
    run = FakeRun([author, checker], {"index.html": {"agent": "vega", "content": "<html>", "done": True}})

    original_call = review.llm.call
    original_stream = review.llm.stream
    original_compose = executor.compose
    review.llm.call = _fake_call
    review.llm.stream = _fake_stream
    executor.compose = _fake_compose
    try:
        findings = asyncio.run(review.run(run, checker, [unit]))
    finally:
        review.llm.call = original_call
        review.llm.stream = original_stream
        executor.compose = original_compose

    # The reviewer's findings lead; the harness may append its own deterministic facts
    # (the pre-review file here is a bare `<html>` fragment, which the design sensor
    # correctly reports). What this test guards is the revise hand-off, not a byte-exact
    # findings string.
    assert findings.startswith(FINDINGS)
    ends = [event for event in run.events if event.get("type") == "file_end"]
    assert ends and ends[-1]["revised"] is True
    assert run.files["index.html"]["done"] is True
    assert "Coffee" in run.fm.written["index.html"]
    assert any(event.get("phase") == "revising" for event in run.events)


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        test_review_revises_without_network(Path(tmp).resolve())
    print("ok   test_review_revises_without_network")
