"""A file-backed task graph with dependencies, so a goal outlives one context window.

`todo.md` is a checklist: one line per planned file, ticked when the file lands. That is
recitation, and it is enough for a single pass over a known file set. It is not enough for
work that has an *order* — "write the stylesheet before the component that imports it",
"do not start the API client until the schema exists". A checklist cannot express that,
so a model that loses the thread starts the blocked task and produces a file that imports
something nobody wrote.

This module is the graph behind the checklist: tasks with ids, an owner, a status, and
`blockedBy` edges. The rules are the ones that make a graph useful rather than decorative:

* **A task is ready only when every blocker is done.** `ready()` is the only sanctioned
  way to pick the next task, so a model cannot talk itself into starting blocked work.
* **A claim is atomic.** `claim()` is a synchronous check-then-set under a module lock,
  so two callers cannot both win the same task — whether they are coroutines gathered by
  `asyncio.gather` or threads. The lock covers every mutation, because they are all
  read-modify-write on the same file.
* **The file is the record.** Every mutation rewrites `.harness-tasks.json` in the run's
  own workspace through `FileManager`, so a restart resumes the graph instead of
  re-deriving it. `AGENTS.md` rule 4: run files are written through the file manager.

The graph is seeded from the plan's work units, so it costs nothing on a run that never
uses it, and `ready()` on an empty graph returns nothing rather than inventing work.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field

FILENAME = ".harness-tasks.json"

PENDING = "pending"
IN_PROGRESS = "in_progress"
DONE = "done"
BLOCKED = "blocked"
STATUSES = (PENDING, IN_PROGRESS, DONE, BLOCKED)

# Every mutation is read-modify-write on one file, so two callers that interleave would
# both see the same "pending" task and both win it. The lock is what makes `claim()`
# atomic; the event loop alone only serialises coroutines, not threads.
_LOCK = threading.RLock()


@dataclass
class Task:
    id: str
    title: str
    status: str = PENDING
    owner: str = ""
    file: str = ""
    objective: str = ""
    blockedBy: list[str] = field(default_factory=list)

    def public(self) -> dict:
        return asdict(self)


def _path(run):
    """The graph's path, or None for a run object with no workspace (a focused test)."""
    workspace = getattr(run, "workspace", None)
    if workspace is None:
        return None
    from pathlib import Path

    return Path(workspace) / FILENAME


def _save(run, tasks: list[Task]) -> None:
    path = _path(run)
    if path is None:
        return
    body = json.dumps([task.public() for task in tasks], indent=2)
    fm = getattr(run, "fm", None)
    try:
        if fm is not None:
            fm.write(FILENAME, body)
        else:
            path.write_text(body, encoding="utf-8")
    except OSError:
        pass


def load(run) -> list[Task]:
    """The graph from disk, or an empty list when this run never seeded one."""
    path = _path(run)
    if path is None:
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []
    tasks: list[Task] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        task_id = str(entry.get("id") or "").strip()
        if not task_id:
            continue
        status = str(entry.get("status") or PENDING)
        tasks.append(
            Task(
                id=task_id,
                title=str(entry.get("title") or task_id),
                status=status if status in STATUSES else PENDING,
                owner=str(entry.get("owner") or ""),
                file=str(entry.get("file") or ""),
                objective=str(entry.get("objective") or ""),
                blockedBy=[str(item) for item in (entry.get("blockedBy") or []) if str(item).strip()],
            )
        )
    return tasks


def seed(run, units: list) -> list[Task]:
    """One task per planned work unit, in plan order, with no edges.

    No edges are invented: the plan does not say which file must precede which, and
    guessing an order from the file list would block work for no reason. A caller that
    knows the order (a workflow, a team lead) adds edges with `depends()`.

    A run with no workspace gets no graph: the graph's whole value is that it survives a
    restart, and one that cannot be written cannot be resumed. Returning tasks that
    `load()` will never find again would be a promise the module cannot keep.
    """
    if _path(run) is None:
        return []
    tasks = [
        Task(
            id=str(getattr(unit, "id", "") or f"t{index + 1}"),
            title=str(getattr(unit, "title", "") or getattr(unit, "file", "") or "task"),
            owner=str(getattr(unit, "agent", "") or ""),
            file=str(getattr(unit, "file", "") or ""),
            objective=str(getattr(unit, "objective", "") or ""),
        )
        for index, unit in enumerate(units or [])
    ]
    with _LOCK:
        _save(run, tasks)
    return tasks


def _reaches(tasks: list[Task], start: str, goal: str) -> bool:
    """Whether `goal` is reachable from `start` by following `blockedBy` edges."""
    by_id = {task.id: task for task in tasks}
    seen: set[str] = set()
    stack = [start]
    while stack:
        current = stack.pop()
        if current == goal:
            return True
        if current in seen:
            continue
        seen.add(current)
        node = by_id.get(current)
        if node is not None:
            stack.extend(node.blockedBy)
    return False


def depends(run, task_id: str, *blockers: str) -> list[Task]:
    """Declare that `task_id` may not start until every named blocker is done.

    An edge that would close a cycle is refused: `ready()` requires every blocker to be
    done, so `a` waiting on `b` and `b` waiting on `a` is a task that can never start and
    can never say why. Unknown ids and self-edges are ignored for the same reason — an
    edge to a task nobody will ever complete is a silent deadlock too.
    """
    with _LOCK:
        tasks = load(run)
        known = {task.id for task in tasks}
        target = next((task for task in tasks if task.id == task_id), None)
        if target is None:
            return tasks
        for blocker in blockers:
            if blocker not in known or blocker == task_id or blocker in target.blockedBy:
                continue
            # Refuse the edge when `task_id` already blocks `blocker` (directly or by
            # chain); adding it would make both tasks permanently unready.
            if _reaches(tasks, blocker, task_id):
                continue
            target.blockedBy.append(blocker)
        _save(run, tasks)
        return tasks


def ready(run) -> list[Task]:
    """Tasks that may be started now: pending, with every blocker done."""
    tasks = load(run)
    done = {task.id for task in tasks if task.status == DONE}
    return [task for task in tasks if task.status == PENDING and all(item in done for item in task.blockedBy)]


def claim(run, task_id: str, owner: str) -> Task | None:
    """Take a task for `owner`. Returns None when it is not ready or already taken.

    Deliberately synchronous: the read, the check and the write happen with no await
    between them, so this is atomic against every other coroutine on the loop.
    """
    with _LOCK:
        tasks = load(run)
        done = {task.id for task in tasks if task.status == DONE}
        target = next((task for task in tasks if task.id == task_id), None)
        if target is None or target.status != PENDING:
            return None
        if not all(item in done for item in target.blockedBy):
            return None
        target.status = IN_PROGRESS
        target.owner = owner
        _save(run, tasks)
        return target


def complete(run, task_id: str) -> list[Task]:
    """Mark a task done and return the graph, so a caller can report what unblocked."""
    with _LOCK:
        tasks = load(run)
        target = next((task for task in tasks if task.id == task_id), None)
        if target is not None:
            target.status = DONE
            _save(run, tasks)
        return tasks


def block(run, task_id: str, reason: str = "") -> list[Task]:
    """Park a task. `reason` is kept on the objective so the transcript explains it."""
    with _LOCK:
        tasks = load(run)
        target = next((task for task in tasks if task.id == task_id), None)
        if target is not None:
            target.status = BLOCKED
            if reason:
                target.objective = f"{target.objective} [blocked: {reason}]".strip()
            _save(run, tasks)
        return tasks


def summary(run) -> dict:
    """Counts and the ready set, for a prompt block and the run metrics."""
    tasks = load(run)
    counts = {status: 0 for status in STATUSES}
    for task in tasks:
        counts[task.status] = counts.get(task.status, 0) + 1
    return {
        "total": len(tasks),
        "counts": counts,
        "ready": [task.id for task in ready(run)],
    }


def orient(run, limit: int = 10) -> str:
    """The recitation block for a prompt: what is ready, what is blocked, what is done."""
    tasks = load(run)
    if not tasks:
        return ""
    done = {task.id for task in tasks if task.status == DONE}
    lines = [f"TASKS ({FILENAME}): {len(done)}/{len(tasks)} done."]
    open_tasks = [task for task in tasks if task.status != DONE]
    for task in open_tasks[:limit]:
        waiting = [item for item in task.blockedBy if item not in done]
        state = f"blocked by {', '.join(waiting)}" if waiting else task.status
        owner = f" [{task.owner}]" if task.owner else ""
        lines.append(f"- {task.id}{owner} · {task.title} · {state}")
    if len(open_tasks) > limit:
        lines.append(f"… and {len(open_tasks) - limit} more.")
    return "\n".join(lines)
