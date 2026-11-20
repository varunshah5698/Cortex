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
