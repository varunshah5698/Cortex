"""Planning node: plan → scaffold the tree → assign owners."""

from __future__ import annotations

import asyncio
import json

from ..agents import planner as planner_agent
from ..config.settings import PLAN_SECONDS
from ..core.state import RunState
from ..models import Specialist, WorkUnit
from ..modules import llm, memory, progress, prompts, tool_log

PLAN_FILE = ".harness-plan.json"
HARNESS_FILES = ("package.json", "index.html", "vite.config.ts", "tsconfig.json")


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
        body["devDependencies"].update(
            {"typescript": "^5.5.4", "@types/react": "^18.3.12", "@types/react-dom": "^18.3.1"}
        )
    return json.dumps(body, indent=2) + "\n"


def _tsconfig() -> str:
    return (
        json.dumps(
            {
                "compilerOptions": {
                    "target": "ES2020",
                    "useDefineForClassFields": True,
                    "lib": ["ES2020", "DOM", "DOM.Iterable"],
                    "module": "ESNext",
                    "skipLibCheck": True,
                    "moduleResolution": "bundler",
                    "allowImportingTsExtensions": True,
                    "resolveJsonModule": True,
                    "isolatedModules": True,
                    "noEmit": True,
                    "jsx": "react-jsx",
                    "strict": True,
                },
                "include": ["src"],
            },
            indent=2,
        )
        + "\n"
    )


def _index(entry: str, css: str = "") -> str:
    link = f'    <link rel="stylesheet" href="/{css}" />\n' if css else ""
    return (
        '<!doctype html>\n<html lang="en">\n  <head>\n'
        '    <meta charset="UTF-8" />\n'
        '    <meta name="viewport" content="width=device-width, initial-scale=1.0" />\n'
        f"{link}"
        "    <title>Portfolio</title>\n  </head>\n  <body>\n"
        '    <div id="root"></div>\n'
        f'    <script type="module" src="/{entry}"></script>\n'
        "  </body>\n</html>\n"
    )


def _base_css() -> str:
    """A coherent baseline, not a design: the model still owns the look.

    A page with no stylesheet is the local model's default and it reads as broken. The
    harness plants variables, typography and spacing; the agent can extend or replace it.
    """
    return """:root {
  --ink: #1f2937;
  --muted: #6b7280;
  --paper: #f8fafc;
  --line: #e5e7eb;
  --accent: #7c4fe0;
  --radius: 12px;
}
* { box-sizing: border-box; }
html { color-scheme: light; }
body {
  margin: 0;
  font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  line-height: 1.6;
  color: var(--ink);
  background: var(--paper);
}
img { max-width: 100%; display: block; }
h1, h2, h3 { line-height: 1.15; letter-spacing: -0.02em; }
h1 { font-size: clamp(2.2rem, 6vw, 4rem); }
h2 { font-size: clamp(1.5rem, 3vw, 2.2rem); }
a { color: var(--accent); }
main, section { padding: clamp(2rem, 6vw, 5rem) clamp(1rem, 5vw, 4rem); }
button, .button {
  border: 0; border-radius: 10px; padding: 0.7rem 1.2rem;
  background: var(--ink); color: white; font: inherit; cursor: pointer;
}
.button:hover, button:hover { opacity: 0.9; }
"""


def _vite_config() -> str:
    return (
        'import { defineConfig } from "vite";\n'
        'import react from "@vitejs/plugin-react";\n\n'
        'export default defineConfig({ plugins: [react()], base: "./" });\n'  # token-prefixed preview
    )


def assign(roster: list[Specialist], units: list[WorkUnit]) -> dict[str, Specialist]:
    """Spread units across the writers: load first, role second, planner last.

    A reviewer that also owns a file reviews its own work, and the separation the
    critic pass exists for is gone. Only fall back to a reviewer/synthesizer when the
    team has nobody else to write.
    """
    writers = [spec for spec in roster if spec.role not in ("reviewer", "synthesizer")] or roster
    loads: dict[str, int] = {}

    def weight(spec: Specialist, unit: WorkUnit) -> tuple[int, int, int]:
        off_role = 0 if spec.role == unit.role else 2
        reserved = 6 if spec.role == "planner" else 0
        return (loads.get(spec.id, 0), off_role + reserved, 0)

    mapping: dict[str, Specialist] = {}
    for unit in units:
        chosen = sorted(writers, key=lambda spec: weight(spec, unit))[0]
        mapping[unit.id] = chosen
        loads[chosen.id] = loads.get(chosen.id, 0) + 1
    return mapping


async def run(run: RunState, planner: Specialist) -> tuple[list[WorkUnit], list[str], list[str]]:
    await run.emit({"type": "phase", "phase": "planning"})
    await run.emit({"type": "status", "agent": planner.id, "status": "thinking"})

    remembered = memory.load()
    learned = memory.lessons()
    if learned:
        remembered = f"{remembered}\n\nDurable lessons:\n{learned}".strip()
    plan_call = planner_agent.plan(planner, run.prompt, remembered)
    try:
        if PLAN_SECONDS > 0:
            objective, units, files, questions, usage = await asyncio.wait_for(plan_call, PLAN_SECONDS)
        else:
            objective, units, files, questions, usage = await plan_call
    except asyncio.TimeoutError:
        # The local model can spend minutes on a plan it never returns. The inferred
        # stack is a working scaffold; the team can still write against it now.
        objective, units, files, questions = planner_agent.parse("", run.prompt)
        usage = llm.Usage()
        await run.emit(
            {
                "type": "message",
                "from": "harness",
                "kind": "notice",
                "text": (
                    f"Planner took more than {PLAN_SECONDS:.0f}s — using the inferred stack fallback "
                    "and starting the room."
                ),
            }
        )
    await run.emit(run.track_usage(planner, "planning", usage))

    if run.depth == "ultra":
        critic = next((s for s in run.specialists if s.role == "reviewer" and s.id != planner.id), None)
        critic = critic or next((s for s in run.specialists if s.id != planner.id), planner)
        plan_json = json.dumps(
            {
                "objective": objective,
                "files": files,
                "work_units": [
                    {"title": unit.title, "role": unit.role, "file": unit.file, "objective": unit.objective}
                    for unit in units
                ],
            },
            indent=2,
        )
        try:
            review, critic_usage = await llm.call(
                critic,
                [
                    {
                        "role": "system",
                        "content": prompts.PLAN_CRITIC.format(prompt=run.prompt, plan=plan_json[:6000]),
                    },
                    {"role": "user", "content": "Attack the plan."},
                ],
            )
            await run.emit(run.track_usage(critic, "plan-critic", critic_usage))
            await run.emit({"type": "message", "from": critic.id, "text": review[:700], "kind": "review"})
            if "REVISE" in review.upper():
                revised = await planner_agent.plan(
                    planner,
                    run.prompt,
                    f"{remembered}\n\nPlan review findings:\n{review[:2000]}",
                )
                objective, units, files, questions, revised_usage = revised
                await run.emit(run.track_usage(planner, "plan-revision", revised_usage))
        except llm.ProviderError as exc:
            await run.emit({"type": "error", "message": str(exc), "agent": critic.id})

    # A manifest is boilerplate, not a design decision. The planner naming Next.js for a
    # tree with src/main.tsx is how a correct portfolio became an unbuildable one; the
    # harness owns the Vite shell and the agent owns the files that need judgement.
    entry = _vite_react(files)
    harness_files: dict[str, str] = {}
    if entry:
        owned = HARNESS_FILES if entry.endswith(".tsx") else HARNESS_FILES[:-1]
        for path in owned:
            if path not in files:
                files.append(path)
        units = [unit for unit in units if unit.file not in owned]
        css = next((path for path in files if path.endswith(".css")), "")
        if not css:
            css = "src/index.css"
            if css not in files:
                files.append(css)
            harness_files[css] = _base_css()
        harness_files["index.html"] = _index(entry, css)
        harness_files["package.json"] = _manifest(run.prompt, entry)
        harness_files["vite.config.ts"] = _vite_config()
        if entry.endswith(".tsx"):
            harness_files["tsconfig.json"] = _tsconfig()

    mapping = assign(run.specialists, units)
    for unit in units:
        unit.agent = mapping[unit.id].id

    # One starting file, not a fixed tree. The plan names the files it expects, but the
    # authors create them as they write; extra files are allowed and verified.
    run.planned_files = list(dict.fromkeys(files))
    initial = next((unit.file for unit in units if unit.file not in harness_files), "")
    start = [initial] if initial else []
    await run.emit({"type": "phase", "phase": "scaffolding"})
    async with tool_log.call(
        run, "file_manager.scaffold", {"files": start}, agent=planner.id, phase="scaffolding"
    ) as call:
        created = run.fm.scaffold(start)
        call.set_result(f"{len(created)} starting file(s) created")
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
