"""Reasoning is two passes: the author plans, a different specialist attacks the plan.

The critic exists because the model that wrote a plan is the worst judge of it. The code
pass must see the critique, and the critique must stream into the visible reasoning
channel rather than disappear into a log.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import ai_harness.core.state as state_mod  # noqa: E402
from ai_harness.agents import executor  # noqa: E402
from ai_harness.core.state import RunState  # noqa: E402
from ai_harness.models import Specialist, WorkUnit  # noqa: E402
from ai_harness.modules import llm  # noqa: E402
from ai_harness.nodes import execution  # noqa: E402


def test_a_deep_build_is_critiqued_by_another_specialist_before_code(monkeypatch, tmp_path):
    monkeypatch.setattr(state_mod, "RUNS_ROOT", tmp_path)
    seen: list[tuple[str, str]] = []

    async def fake_stream(agent, messages, think=True, stats=None):  # noqa: ANN001
        system = messages[0]["content"]
        seen.append((agent.id, system))
        if "Attack the plan" in system:
            line = "VERDICT: REVISE\nFINDINGS: 1. #ghost is never declared."
            stats.content.append(line)
            yield ("token", line)
            return
        if "Think before writing" in system:
            line = "1. RESTATE a page. 2. DECISION one button."
            stats.content.append(line)
            yield ("think", line)
            return
        for line in ("<<<FILE app.js>>>\n", 'document.querySelector("#real");\n'):
            stats.content.append(line)
            yield ("content", line)

    monkeypatch.setattr(llm, "stream", fake_stream)

    author = Specialist(id="a1", name="Ada", role="frontend", model="stub")
    reviewer = Specialist(id="r1", name="Rex", role="reviewer", model="stub")
    run = RunState(run_id="critique", prompt="a page", specialists=[author, reviewer])
    unit = WorkUnit(id="wu-1", title="Logic", role="frontend", file="app.js", objective="the page", agent="a1")

    asyncio.run(execution.write_unit(run, unit, []))

    critic_calls = [agent for agent, system in seen if "Attack the plan" in system]
    assert critic_calls == ["r1"], "the plan must be attacked by someone other than its author"

    reason_prompt = next(system for _, system in seen if "Think before writing" in system)
    code_prompt = next(system for _, system in seen if "Output format, exactly:" in system)
    assert "That file's job in the build: the page" in reason_prompt
    assert "That file's job in the build: the page" in code_prompt
    assert "VERDICT: REVISE" in code_prompt, "the code pass must be grounded in the critique"

    assert any(
        event["type"] == "think" and event["agent"] == "r1" and "VERDICT" in event["delta"] for event in run.events
    ), "the critique must stream into the visible reasoning channel"


def test_a_deep_file_that_does_not_parse_is_repaired_before_the_next_file(monkeypatch, tmp_path):
    """Every file is checked as it lands, not only on Ultra and not only at the end.

    Writing a full-crew build one file at a time and looking at the tree only after the
    last one is what let a broken file be imported by every file written after it.
    """
    monkeypatch.setattr(state_mod, "RUNS_ROOT", tmp_path)
    repaired: list[str] = []

    async def fake_stream(agent, messages, think=True, stats=None):  # noqa: ANN001
        system = messages[0]["content"]
        if "Attack the plan" in system:
            stats.content.append("VERDICT: OK")
            yield ("token", "VERDICT: OK")
            return
        if "Think before writing" in system:
            stats.content.append("1. write the module")
            yield ("think", "1. write the module")
            return
        for line in ("<<<FILE app.py>>>\n", "def broken(:\n"):
            stats.content.append(line)
            yield ("content", line)

    async def fake_repair(run, agent, path, findings, check, max_steps=0):  # noqa: ANN001
        repaired.append(path)
        return True

    monkeypatch.setattr(llm, "stream", fake_stream)
    monkeypatch.setattr(execution.agent_loop, "repair", fake_repair)

    author = Specialist(id="a1", name="Ada", role="backend", model="stub")
    reviewer = Specialist(id="r1", name="Rex", role="reviewer", model="stub")
    run = RunState(run_id="per-file", prompt="a service", specialists=[author, reviewer], depth="deep")
    unit = WorkUnit(id="wu-1", title="Logic", role="backend", file="app.py", objective="the module", agent="a1")

    asyncio.run(execution.write_unit(run, unit, []))

    assert repaired == ["app.py"], "a deep run must fix a file that does not parse before moving on"


def test_a_rambling_reasoning_stream_is_cut_off(monkeypatch):
    monkeypatch.setattr(executor, "MAX_REASONING_CHARS", 100)
    seen: list[str] = []

    async def fake_stream(agent, messages, think=True, stats=None):  # noqa: ANN001
        for _ in range(1000):
            stats.thinking.append("word ")
            yield ("think", "word ")

    async def sink(text: str) -> None:
        seen.append(text)

    monkeypatch.setattr(executor.llm, "stream", fake_stream)
    agent = Specialist(id="a", name="A", role="general", model="stub")
    text, _usage = asyncio.run(executor.reason(agent, [{"role": "user", "content": "x"}], sink))

    assert len(text) <= 200
    assert len(seen) < 1000
    assert any("reasoning budget" in item for item in seen)


def test_a_compose_that_only_thinks_never_becomes_a_file(monkeypatch):
    monkeypatch.setattr(executor, "MAX_REASONING_CHARS", 100)
    calls = {"think": 0, "code": 0}

    async def fake_stream(agent, messages, think=True, stats=None):  # noqa: ANN001
        for _ in range(1000):
            calls["think"] += 1
            stats.thinking.append("word ")
            yield ("think", "word ")

    async def sink(_text: str) -> None:
        return None

    async def code(_path: str, _text: str) -> None:
        calls["code"] += 1

    monkeypatch.setattr(executor.llm, "stream", fake_stream)
    agent = Specialist(id="a", name="A", role="general", model="stub")
    files, _usage = asyncio.run(
        executor.compose(agent, [{"role": "user", "content": "x"}], sink, code, sink, "index.html")
    )

    assert files == []
    assert calls["think"] < 1000
    assert calls["code"] == 0


def test_every_depth_critiques_the_plan_not_only_deep_and_ultra(monkeypatch, tmp_path):
    """Depth must not silently mean "no second opinion".

    The critique was gated to `deep`/`ultra`, so a default or `quick` build wrote code
    against a plan nobody had attacked. `quick` is allowed to skip research and
    negotiation; it is not allowed to skip the one pass that catches a bad plan before it
    becomes a bad file.
    """
    monkeypatch.setattr(state_mod, "RUNS_ROOT", tmp_path)
    seen: list[str] = []

    async def fake_stream(agent, messages, think=True, stats=None):  # noqa: ANN001
        system = messages[0]["content"]
        seen.append(system)
        if "Attack the plan" in system:
            line = "VERDICT: REVISE\nFINDINGS: 1. name the id."
            stats.content.append(line)
            yield ("token", line)
            return
        if "Think before writing" in system:
            line = "1. RESTATE a page."
            stats.content.append(line)
            yield ("think", line)
            return
        for line in ("<<<FILE app.js>>>\n", 'document.querySelector("#real");\n'):
            stats.content.append(line)
            yield ("content", line)

    monkeypatch.setattr(llm, "stream", fake_stream)

    for depth in ("quick", "brief", "deep", "ultra"):
        seen.clear()
        author = Specialist(id="a1", name="Ada", role="frontend", model="stub")
        reviewer = Specialist(id="r1", name="Rex", role="reviewer", model="stub")
        run = RunState(run_id=f"critique-{depth}", prompt="a page", specialists=[author, reviewer], depth=depth)
        unit = WorkUnit(id="wu-1", title="Logic", role="frontend", file="app.js", objective="the page", agent="a1")
        asyncio.run(execution.write_unit(run, unit, []))
        assert any("Attack the plan" in system for system in seen), f"{depth} skipped the critique pass"


def test_the_whole_plan_reaches_the_code_pass(monkeypatch, tmp_path):
    """A plan the stream allowed to be long must not be cut to a third before it is used.

    `context.clip` takes TOKENS, so the old literal 700 kept 2,800 characters of a plan
    the reasoning stream allowed to be 4,200. The model deliberated, then wrote against
    part of its own thinking, on every file.
    """
    from ai_harness.config.settings import MAX_REASONING_CHARS

    monkeypatch.setattr(state_mod, "RUNS_ROOT", tmp_path)
    marker = "BLUEPRINT-MARKER-AT-THE-END"
    # A plan that fills the stream budget, with a marker at the very end.
    plan = ("x" * (MAX_REASONING_CHARS - len(marker) - 10)) + marker
    code_prompt = ""

    async def fake_stream(agent, messages, think=True, stats=None):  # noqa: ANN001
        nonlocal code_prompt
        system = messages[0]["content"]
        if "Think before writing" in system:
            stats.content.append(plan)
            yield ("think", plan)
            return
        if "Attack the plan" in system:
            stats.content.append("VERDICT: OK")
            yield ("token", "VERDICT: OK")
            return
        code_prompt = system
        for line in ("<<<FILE app.js>>>\n", "const a = 1;\n"):
            stats.content.append(line)
            yield ("content", line)

    monkeypatch.setattr(llm, "stream", fake_stream)

    author = Specialist(id="a1", name="Ada", role="frontend", model="stub")
    reviewer = Specialist(id="r1", name="Rex", role="reviewer", model="stub")
    run = RunState(run_id="clip", prompt="a page", specialists=[author, reviewer], depth="deep")
    unit = WorkUnit(id="wu-1", title="Logic", role="frontend", file="app.js", objective="the page", agent="a1")
    asyncio.run(execution.write_unit(run, unit, []))

    assert marker in code_prompt, "the end of the plan was clipped before the code pass saw it"
