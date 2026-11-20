"""The file count is the task's decision, not the harness's.

A cap of eight planned units plus the three harness-owned Vite files made every site
look capped at eleven files. The plan keeps every file the planner names; an agent may
add more with `<<<FILE path>>>`.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_harness.agents import planner  # noqa: E402


def test_a_plan_can_name_more_than_eight_files():
    files = [f"src/components/Part{i}.tsx" for i in range(15)]
    raw = json.dumps(
        {
            "objective": "a large page",
            "stack": "react+typescript",
            "files": files,
            "work_units": [
                {"title": f"Part {i}", "role": "frontend", "file": path, "objective": "one section"}
                for i, path in enumerate(files)
            ],
            "research_questions": [],
        }
    )
    _, units, planned, _ = planner.parse(raw, "build a large page")
    assert len(planned) == 15
    assert len(units) == 15


def test_scaffolding_starts_with_one_file_and_keeps_the_rest_as_plan(monkeypatch, tmp_path):
    import asyncio

    import ai_harness.core.state as state_mod
    from ai_harness.core.state import RunState
    from ai_harness.models import Specialist, WorkUnit
    from ai_harness.modules.usage import Usage
    from ai_harness.nodes import planning

    monkeypatch.setattr(state_mod, "RUNS_ROOT", tmp_path)
    files = [f"src/module_{index}.py" for index in range(10)]
    units = [
        WorkUnit(id=f"u{index}", title=f"Module {index}", role="backend", file=path, objective="one module")
        for index, path in enumerate(files)
    ]

    async def fake_plan(agent, prompt, memory=""):  # noqa: ANN001
        return "a ten-module program", units, files, [], Usage()

    monkeypatch.setattr(planning.planner_agent, "plan", fake_plan)
    agent = Specialist(id="a1", name="Ada", role="planner", model="stub")
    run = RunState(run_id="scaffold-one", prompt="build ten modules", specialists=[agent])
    asyncio.run(planning.run(run, agent))

    assert run.planned_files == files
    assert list(run.files) == [files[0]], "only the starting file is scaffolded"
