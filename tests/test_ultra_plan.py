"""Ultra planning is two drafts: a critic, then a revised plan.

The first live Ultra run crashed because the revision call was unpacked as two values
instead of five. This keeps the path exercised without a provider.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import ai_harness.core.state as state_mod  # noqa: E402
from ai_harness.core.state import RunState  # noqa: E402
from ai_harness.models import Specialist, WorkUnit  # noqa: E402
from ai_harness.modules import llm  # noqa: E402
from ai_harness.modules.usage import Usage  # noqa: E402
from ai_harness.nodes import planning  # noqa: E402


def test_ultra_revises_the_plan_after_the_critic(monkeypatch, tmp_path):
    monkeypatch.setattr(state_mod, "RUNS_ROOT", tmp_path)
    first = ["app.py"]
    revised = ["app.py", "coffee.py", "templates/menu.html"]
    calls: list[str] = []

    async def fake_plan(agent, prompt, memory=""):  # noqa: ANN001
        calls.append(memory)
        files = first if len(calls) == 1 else revised
        units = [
            WorkUnit(id=f"u{i}", title=path, role="backend", file=path, objective="build it")
            for i, path in enumerate(files)
        ]
        return "a coffee shop page", units, files, [], Usage(prompt_tokens=10, completion_tokens=5, calls=1)

    async def fake_call(agent, messages, **kwargs):  # noqa: ANN001
        return "VERDICT: REVISE\nFINDINGS: 1. split the menu into its own file", Usage(
            prompt_tokens=5, completion_tokens=5, calls=1
        )

    monkeypatch.setattr(planning.planner_agent, "plan", fake_plan)
    monkeypatch.setattr(llm, "call", fake_call)

    planner = Specialist(id="p", name="Scout", role="planner", model="stub")
    critic = Specialist(id="r", name="Lyra", role="reviewer", model="stub")
    run = RunState(run_id="ultra-plan", prompt="build a coffee shop page", specialists=[planner, critic], depth="ultra")
    asyncio.run(planning.run(run, planner))

    assert len(calls) == 2, "the planner must get one revision call"
    assert "Plan review findings" in calls[1]
    assert run.planned_files == revised
