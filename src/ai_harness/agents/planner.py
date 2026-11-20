"""Planner: turns a prompt into a file tree and work units."""

from __future__ import annotations

import json
import re

from ..config.settings import MAX_UNITS
from ..models import Specialist, WorkUnit
from ..modules import llm, prompts

ROLES = {"planner", "frontend", "backend", "reviewer", "research", "synthesizer", "general"}

# Fallback scaffolds, used only when the planner's JSON cannot be parsed. The stack is
# inferred from the request instead of assuming everything is a web page: a React ask
# gets a .tsx tree, a Python ask gets .py + requirements.txt.
#
# Keywords match WHOLE WORDS: substring matching scaffolded a TypeScript CLI for
# "client dashboard" ('cli' inside 'client') and for "nodes" ('node' inside 'nodes').
# Language families are listed before tooling, so "a rust cli" and "a go cli" are not
# read as Node. Bare "go" is deliberately not a keyword — it is ordinary English
# ("updates as you go") — so Go is matched by "golang", "go.mod" or a phrase like
# "go api"/"go server".
# (stack, keyword pattern, [(title, role, path, objective)])


def _hints(*words: str) -> re.Pattern:
    """Whole-word matcher: 'cli' cannot match 'client', 'node' cannot match 'nodes'."""
    return re.compile(r"\b(?:" + "|".join(re.escape(word) for word in words) + r")\b", re.IGNORECASE)


FAMILIES: list[tuple[str, re.Pattern, list[tuple[str, str, str, str]]]] = [
    (
        "vue",
        _hints("vue", "nuxt"),
        [
            ("Manifest", "backend", "package.json", "name, dev/build scripts, and only the dependencies actually used"),
            ("Shell", "frontend", "index.html", "the document with #app that loads /src/main.ts"),
            ("Entry", "frontend", "src/main.ts", "create the Vue app and mount it into #app"),
            ("UI", "frontend", "src/App.vue", "the complete component tree for the request, styles included"),
        ],
    ),
    (
        "svelte",
        _hints("svelte", "sveltekit"),
        [
            ("Manifest", "backend", "package.json", "name, dev/build scripts, and only the dependencies actually used"),
            ("Shell", "frontend", "index.html", "the document with #app that loads /src/main.ts"),
            ("Entry", "frontend", "src/main.ts", "import the root component and mount it into #app"),
            ("UI", "frontend", "src/App.svelte", "the complete component for the request, styles included"),
        ],
    ),
    (
        "react+typescript",
        _hints("react", "tsx", "jsx", "vite", "next.js", "nextjs"),
        [
            ("Manifest", "backend", "package.json", "name, dev/build scripts, and only the dependencies actually used"),
            ("Shell", "frontend", "index.html", "the document with #root that loads /src/main.tsx"),
            ("Entry", "frontend", "src/main.tsx", "mount the app into #root"),
            ("UI", "frontend", "src/App.tsx", "the complete component tree for the request, styles included"),
        ],
    ),
    (
        "python",
        _hints("python", "fastapi", "flask", "django", "pandas", "numpy"),
        [
            ("Application", "backend", "app.py", "the runnable program for the request"),
            ("Dependencies", "backend", "requirements.txt", "pinned third-party packages, one per line"),
            ("Checks", "general", "test_app.py", "a small runnable test of the core behaviour"),
        ],
    ),
    (
        "rust",
        _hints("rust", "cargo", "tokio", "axum"),
        [
            ("Manifest", "backend", "Cargo.toml", "package name, edition and dependencies"),
            ("Entry", "backend", "src/main.rs", "the complete runnable program for the request"),
            ("Library", "backend", "src/lib.rs", "the logic behind the binary, in testable functions"),
        ],
    ),
    (
        "go",
        _hints(
            "golang", "go.mod", "go module", "go api", "go server", "go service", "go cli", "go program", "gin", "fiber"
        ),
        [
            ("Manifest", "backend", "go.mod", "module path and the Go version"),
            ("Entry", "backend", "main.go", "the complete runnable program for the request"),
            ("Checks", "general", "main_test.go", "a table-driven test of the core behaviour"),
        ],
    ),
    (
        "typescript (node)",
        _hints("typescript", "node", "express", "npm", "bun", "tsc", "cli", "api server"),
        [
            ("Manifest", "backend", "package.json", "name, type: module, scripts and dependencies"),
            ("Entry", "backend", "src/index.ts", "the complete entry point for the request"),
            ("Config", "backend", "tsconfig.json", "a strict compiler configuration"),
        ],
    ),
]

DEFAULT_FAMILY = (
    "html+css+js",
    [
        ("Markup", "frontend", "index.html", "the complete markup, linking styles.css and app.js by those names"),
        ("Styling", "frontend", "styles.css", "the complete styling, sharing ids and classes with the markup"),
        ("Behaviour", "backend", "app.js", "the complete behaviour, wiring the ids and events the markup exposes"),
    ],
)


def _units(specs: list[tuple[str, str, str, str]], prompt: str) -> list[WorkUnit]:
    return [
        WorkUnit(
            id=f"wu-{index + 1}",
            title=title,
            role=role,
            file=path,
            objective=f"{objective}. Request: {prompt}",
        )
        for index, (title, role, path, objective) in enumerate(specs)
    ]


def infer_stack(prompt: str) -> tuple[str, list[WorkUnit]]:
    """Pick the smallest sensible scaffold for the stack the request implies."""
    for stack, pattern, specs in FAMILIES:
        if pattern.search(prompt):
            return stack, _units(specs, prompt)
    return DEFAULT_FAMILY[0], _units(DEFAULT_FAMILY[1], prompt)


def _clean(path: str, index: int) -> str:
    name = re.sub(r"[^\w./-]", "", str(path or "")).strip("./")
    return name or f"file-{index + 1}.txt"


def _role_for(path: str) -> str:
    """Who should own a file the planner declared but did not assign."""
    suffix = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if suffix in ("css", "scss", "html", "vue", "svelte"):
        return "frontend"
    if suffix in ("json", "toml", "lock", "mod"):
        return "backend"
    return "general"


def parse(raw: str, prompt: str) -> tuple[str, list[WorkUnit], list[str]]:
    """Parse the planner JSON; fall back to the stack the request implies."""
    start, end = raw.find("{"), raw.rfind("}")
    objective = prompt
    files: list[str] = []
    questions: list[str] = []
    units: list[WorkUnit] = []

    if start != -1 and end > start:
        try:
            data = json.loads(raw[start : end + 1])
            objective = str(data.get("objective") or prompt)[:300]
            files = [_clean(f, i) for i, f in enumerate(data.get("files") or [])][:MAX_UNITS]
            questions = [str(q)[:160] for q in (data.get("research_questions") or [])][:3]
            for index, item in enumerate((data.get("work_units") or [])[:MAX_UNITS]):
                role = str(item.get("role") or "general")
                units.append(
                    WorkUnit(
                        id=f"wu-{index + 1}",
                        title=str(item.get("title") or f"Unit {index + 1}")[:60],
                        role=role if role in ROLES else "general",
                        file=_clean(item.get("file"), index),
                        objective=str(item.get("objective") or "")[:600],
                    )
                )
        except json.JSONDecodeError:
            pass

    if not units:
        _, units = infer_stack(prompt)
    if not files:
        files = [unit.file for unit in units]

    # every unit must own exactly one declared file, and no two units may share one
    declared = list(dict.fromkeys(files))
    assigned: set[str] = set()
    for index, unit in enumerate(units):
        if unit.file in declared and unit.file not in assigned:
            assigned.add(unit.file)
            continue
        free = next((path for path in declared if path not in assigned), None)
        unit.file = free or _clean(unit.file, index)
        assigned.add(unit.file)

    # ...and every declared file must own a unit, or it stays an empty scaffold forever
    # while the verifier correctly calls it dead. The planner is not asked to be
    # exhaustive, so the harness closes the gap instead of shipping the hole.
    for path in declared:
        if path in assigned or len(units) >= MAX_UNITS:
            continue
        units.append(
            WorkUnit(
                id=f"wu-{len(units) + 1}",
                title=path.split("/")[-1][:60],
                role=_role_for(path),
                file=path,
                objective=f"Complete {path} so the whole app runs. Request: {prompt}",
            )
        )
        assigned.add(path)

    return objective, units, files, questions


async def plan(agent: Specialist, prompt: str, memory: str = "") -> tuple[str, list[WorkUnit], list[str], llm.Usage]:
    """Plan the tree. `memory` is the project's record of earlier runs, if it has one."""
    text, usage = await llm.call(
        agent,
        [
            {
                "role": "system",
                "content": prompts.PLANNER + prompts.PROJECT_MEMORY.format(memory=memory.strip() or "none yet"),
            },
            {"role": "user", "content": f"Request: {prompt}"},
        ],
        # The planner answers with one JSON object; ask for JSON mode where it exists.
        json_mode=True,
    )
    objective, units, files, questions = parse(text, prompt)
    return objective, units, files, questions, usage
