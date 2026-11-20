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

    code_prompt = next(system for _, system in seen if "Output format, exactly:" in system)
    assert "VERDICT: REVISE" in code_prompt, "the code pass must be grounded in the critique"

    assert any(
        event["type"] == "think" and event["agent"] == "r1" and "VERDICT" in event["delta"] for event in run.events
    ), "the critique must stream into the visible reasoning channel"
