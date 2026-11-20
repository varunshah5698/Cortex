"""Planning node: plan → scaffold the tree → assign owners."""

from __future__ import annotations

import json

from ..agents import planner as planner_agent
from ..core.state import RunState
from ..models import Specialist, WorkUnit
from ..modules import memory, progress, tool_log

PLAN_FILE = ".harness-plan.json"
HARNESS_FILES = ("package.json", "index.html", "vite.config.ts")


def _vite_react(files: list[str]) -> str:
    """The entry file when this is a Vite React tree, else ''."""
    return "src/main.tsx" if "src/main.tsx" in files else "src/main.jsx" if "src/main.jsx" in files else ""


def _manifest(prompt: str, entry: str) -> str:
    name = "-".join(word for word in "".join(c if c.isalnum() else " " for c in prompt.lower()).split()[:4])
    body = {
        "name": name or "app",
        "private": True,
        "version": "0.0.0",
        "type": "module",
        "scripts": {"dev": "vite", "build": "vite build", "preview": "vite preview"},
        "dependencies": {"react": "^18.3.1", "react-dom": "^18.3.1"},
        "devDependencies": {"@vitejs/plugin-react": "^4.3.1", "vite": "^5.4.0"},
    }
    if entry.endswith(".tsx"):
        body["devDependencies"]["typescript"] = "^5.5.4"
    return json.dumps(body, indent=2) + "\n"


def _index(entry: str) -> str:
    return (
        '<!doctype html>\n<html lang="en">\n  <head>\n'
        '    <meta charset="UTF-8" />\n'
        '    <meta name="viewport" content="width=device-width, initial-scale=1.0" />\n'
        "    <title>Portfolio</title>\n  </head>\n  <body>\n"
        '    <div id="root"></div>\n'
        f'    <script type="module" src="/{entry}"></script>\n'
        "  </body>\n</html>\n"
    )


def _vite_config() -> str:
    return (
        'import { defineConfig } from "vite";\n'
        'import react from "@vitejs/plugin-react";\n\n'
        "export default defineConfig({ plugins: [react()] });\n"
    )


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

    # A manifest is boilerplate, not a design decision. The planner naming Next.js for a
    # tree with src/main.tsx is how a correct portfolio became an unbuildable one; the
    # harness owns the Vite shell and the agent owns the files that need judgement.
    entry = _vite_react(files)
    harness_files: dict[str, str] = {}
    if entry:
        for path in HARNESS_FILES:
            if path not in files:
                files.append(path)
        units = [unit for unit in units if unit.file not in HARNESS_FILES]
        harness_files = {
            "package.json": _manifest(run.prompt, entry),
            "index.html": _index(entry),
            "vite.config.ts": _vite_config(),
        }

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

    for path, content in harness_files.items():
        run.fm.write(path, content)
        run.files[path] = {"agent": "harness", "content": content, "done": True}
        await run.emit({"type": "file_end", "agent": "harness", "path": path, "lines": content.count("\n") + 1})
    if harness_files:
        await run.emit(
            {
                "type": "message",
                "from": "harness",
                "kind": "plan",
                "text": "Wrote the Vite manifest, index.html and config — the agents write the app.",
            }
        )

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
