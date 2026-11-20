"""Planning node: plan → scaffold the tree → assign owners."""

from __future__ import annotations

import json

from ..agents import planner as planner_agent
from ..core.state import RunState
from ..models import Specialist, WorkUnit
from ..modules import memory, progress, tool_log

PLAN_FILE = ".harness-plan.json"


def assign(roster: list[Specialist], units: list[WorkUnit]) -> dict[str, Specialist]:
    """Spread units across the team: load first, role second, planner last."""
    loads: dict[str, int] = {}

    def weight(spec: Specialist, unit: WorkUnit) -> tuple[int, int, int]:
        off_role = 0 if spec.role == unit.role else 2
        reserved = 20 if spec.role in ("reviewer", "synthesizer") else 6 if spec.role == "planner" else 0
        return (loads.get(spec.id, 0), off_role + reserved, 0)

    mapping: dict[str, Specialist] = {}
    for unit in units:
        chosen = sorted(roster, key=lambda spec: weight(spec, unit))[0]
        mapping[unit.id] = chosen
        loads[chosen.id] = loads.get(chosen.id, 0) + 1
    return mapping


async def run(run: RunState, planner: Specialist) -> tuple[list[WorkUnit], list[str], list[str]]:
    await run.emit({"type": "phase", "phase": "planning"})
    await run.emit({"type": "status", "agent": planner.id, "status": "thinking"})

    objective, units, files, questions, usage = await planner_agent.plan(planner, run.prompt, memory.load())
    await run.emit(run.track_usage(planner, "planning", usage))

    mapping = assign(run.specialists, units)
    for unit in units:
        unit.agent = mapping[unit.id].id

    # The environment exists before any model writes: empty files, real paths.
    await run.emit({"type": "phase", "phase": "scaffolding"})
    async with tool_log.call(
        run, "file_manager.scaffold", {"files": files}, agent=planner.id, phase="scaffolding"
    ) as call:
        created = run.fm.scaffold(files)
        call.set_result(f"{len(created)} empty file(s) created")
    await run.emit({"type": "scaffold", "files": created, "objective": objective})
    for path in created:
        run.files[path] = {"agent": None, "content": "", "done": False}

    # The checklist and the plan go to disk before any model writes: `todo.md` is the
    # recitation every later prompt re-reads, and `.harness-plan.json` is what a resumed
    # run needs to skip planning without re-asking a model.
    progress.write(run, objective, units)
    _save_plan(run, objective, units)

    await run.emit(
        {
            "type": "plan",
            "objective": objective,
            "units": [
                {
                    "id": unit.id,
                    "title": unit.title,
                    "role": unit.role,
                    "objective": unit.objective,
                    "file": unit.file,
                    "agent": unit.agent,
                }
                for unit in units
            ],
        }
    )
    await run.emit({"type": "status", "agent": planner.id, "status": "done"})
    await run.emit(
        {
            "type": "message",
            "from": planner.id,
            "text": f"Scaffolded {len(created)} files · " + " → ".join(u.title for u in units),
            "kind": "plan",
        }
    )
    return units, files, questions


def _save_plan(run: RunState, objective: str, units: list[WorkUnit]) -> None:
    payload = {
        "objective": objective,
        "prompt": run.prompt,
        "files": list(run.files),
        "units": [
            {
                "id": unit.id,
                "title": unit.title,
                "role": unit.role,
                "file": unit.file,
                "objective": unit.objective,
                "agent": unit.agent,
            }
            for unit in units
        ],
    }
    try:
        (run.workspace / PLAN_FILE).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        pass


def load_plan(run: RunState) -> list[WorkUnit] | None:
    """The units a resumed run should keep working from, or None when it never planned."""
    try:
        body = json.loads((run.workspace / PLAN_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    units = [
        WorkUnit(
            id=str(item.get("id") or f"unit-{index}"),
            title=str(item.get("title") or ""),
            role=str(item.get("role") or "general"),
            file=str(item.get("file") or ""),
            objective=str(item.get("objective") or ""),
            agent=str(item.get("agent") or ""),
        )
        for index, item in enumerate(body.get("units") or [])
        if item.get("file")
    ]
    return units or None
