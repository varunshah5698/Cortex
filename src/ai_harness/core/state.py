"""Run state: the event log, the workspace and the usage ledger."""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..config.settings import MAX_RUN_CALLS, MAX_RUN_TOKENS, MAX_RUN_USD, RUNS_ROOT
from ..models import Specialist, WorkUnit
from ..modules import prompts
from ..modules.budget import Budget
from ..modules.llm import Usage
from ..tools.file_manager import FileManager

EVENT_LOG = "events.jsonl"
TERMINAL_EVENTS = ("done", "error")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RunState:
    run_id: str
    prompt: str
    specialists: list[Specialist]
    depth: str = "deep"
    kind: str = "build"
    created: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    # Monotonic clock for the run's wall-clock limit (`modules/runtime.check_deadline`).
    # A clock that can jump backwards must not be able to extend a run.
    started_at: float = field(default_factory=time.monotonic)
    events: list[dict] = field(default_factory=list)
    subscribers: list[asyncio.Queue] = field(default_factory=list)
    files: dict[str, dict] = field(default_factory=dict)
    units: list[WorkUnit] = field(default_factory=list)
    usage: dict[str, Usage] = field(default_factory=dict)
    contracts: dict[str, str] = field(default_factory=dict)
    sources: list[dict] = field(default_factory=list)
    done: bool = False
    answer: str = ""
    task: asyncio.Task | None = None
    budget: Budget = field(
        default_factory=lambda: Budget(max_tokens=MAX_RUN_TOKENS, max_usd=MAX_RUN_USD, max_calls=MAX_RUN_CALLS)
    )
    verification: dict | None = None
    # The verdict has two halves and `done` reports both. Verification read the files;
    # nothing installed or compiled them unless a preview did, so `previewed` stays False
    # on a run that was only verified, and `build` is None until something really built.
    previewed: bool = False
    build: dict | None = None
    # Tier 4, recorded after serving: what a real browser saw. It never changes `done`;
    # a page can be structurally verified and still fail here.
    browser: dict | None = None
    cache_hits: int = 0
    # False when rebuilding from disk: an existing workspace must never be wiped.
    fresh: bool = True

    def __post_init__(self) -> None:
        self.workspace = RUNS_ROOT / self.run_id
        if self.fresh and self.workspace.exists():
            shutil.rmtree(self.workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.fm = FileManager(self.workspace)
        self.event_path = self.workspace / EVENT_LOG
        self._log: object | None = None

    async def emit(self, event: dict) -> None:
        self.events.append(event)
        self._record(event)
        if event.get("type") in TERMINAL_EVENTS:
            self.done = event.get("type") == "done"
            self.close_log()
        for queue in list(self.subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                self.subscribers.remove(queue)

    def _record(self, event: dict) -> None:
        """Append the event to the run's log, so a restart cannot erase the history."""
        try:
            if self._log is None:
                self._log = self.event_path.open("a", encoding="utf-8")
            self._log.write(json.dumps(event, default=str) + "\n")  # type: ignore[union-attr]
            self._log.flush()  # type: ignore[union-attr]
        except (OSError, TypeError, ValueError):
            self._log = None

    def close_log(self) -> None:
        if self._log is None:
            return
        try:
            self._log.close()  # type: ignore[union-attr]
        except OSError:
            pass
        self._log = None

    def save_metrics(self) -> None:
        """Persist the ledger next to the workspace, so a restart cannot lose it."""
        try:
            (self.workspace / ".harness-metrics.json").write_text(
                json.dumps(self.metrics(), indent=2), encoding="utf-8"
            )
        except OSError:
            pass

    def track_usage(self, agent: Specialist, phase: str, usage: Usage) -> dict:
        ledger = self.usage.setdefault(agent.id, Usage())
        ledger.add(usage)
        self.cache_hits += usage.cached
        return {
            "type": "usage",
            "agent": agent.id,
            "name": agent.name,
            "role": agent.role,
            "model": agent.model,
            "phase": phase,
            **usage.as_dict(),
        }

    def file_summary(self) -> list[dict]:
        items = []
        for path, meta in sorted(self.files.items()):
            items.append(
                {
                    "path": path,
                    "agent": meta.get("agent"),
                    "lines": meta.get("content", "").count("\n") + 1,
                    "done": meta.get("done", False),
                    "revised": meta.get("revised", False),
                    "bytes": len(meta.get("content", "").encode()),
                }
            )
        return items

    def metrics(self) -> dict:
        totals = Usage()
        per_agent = []
        for agent in self.specialists:
            ledger = self.usage.get(agent.id)
            if not ledger:
                continue
            totals.add(ledger)
            per_agent.append({**agent.public(), **ledger.as_dict()})
        return {
            "run_id": self.run_id,
            "prompt": self.prompt,
            "depth": self.depth,
            "kind": self.kind,
            "created": self.created,
            "done": self.done,
            "answer": self.answer,
            "files": len(self.files),
            "totals": totals.as_dict(),
            "agents": per_agent,
            "budget": self.budget.as_dict(),
            "cache": {"run_hits": self.cache_hits},  # process-wide counters live in /api/health
            "verification": self.verification,
            "build": self.build,
            "previewed": self.previewed,
            "browser": self.browser,
            "prompts": prompts.fingerprints(),
        }


def read_metrics(run_id: str) -> dict | None:
    workspace = Path(RUNS_ROOT) / run_id
    memory = workspace / ".harness-metrics.json"
    if not memory.exists():
        return None
    return json.loads(memory.read_text(encoding="utf-8"))


def read_events(run_id: str) -> list[dict]:
    """Every event a run ever emitted, in order. Unreadable lines are skipped."""
    path = Path(RUNS_ROOT) / run_id / EVENT_LOG
    if not path.is_file():
        return []
    events: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def restore_run(run_id: str) -> RunState | None:
    """Rebuild a finished run from disk so its event log can be replayed.

    Live runs never take this path: `Harness.get` returns them from memory first. A run
    whose log has no terminal event was cut off by a restart, so it is closed out with a
    single in-memory error instead of leaving a subscriber waiting forever.
    """
    workspace = Path(RUNS_ROOT) / run_id
    if not workspace.is_dir():
        return None

    events = read_events(run_id)
    if not events:
        return restore_legacy_run(run_id)

    start = next((event for event in events if event.get("type") == "start"), {})
    finished = next((event for event in events if event.get("type") == "done"), {})
    run = RunState(
        run_id=run_id,
        prompt=str(start.get("prompt", "")),
        specialists=[],
        kind=str(start.get("kind") or "build"),
        fresh=False,
    )
    run.events = events
    run.answer = str(finished.get("answer") or "")
    run.done = any(event.get("type") in TERMINAL_EVENTS for event in events)
    # What the preview did happens *after* the terminal event, so it only exists in the
    # ledger: rebuilding from the event log alone would quietly reset a served run to
    # "verified, never compiled".
    ledger = read_metrics(run_id) or {}
    run.previewed = bool(ledger.get("previewed", False))
    run.build = ledger.get("build") if isinstance(ledger.get("build"), dict) else None
    run.browser = ledger.get("browser") if isinstance(ledger.get("browser"), dict) else None

    if not run.done:
        run.events.append(
            {
                "type": "error",
                "message": "This run was interrupted when the harness restarted. Its files are still on disk.",
                "recovered": True,
            }
        )
        run.done = True
    return run


def restore_legacy_run(run_id: str) -> RunState | None:
    """Runs recorded before event logs existed: rebuild a minimal replayable transcript."""
    workspace = Path(RUNS_ROOT) / run_id
    if not workspace.is_dir():
        return None

    metrics = None
    metrics_path = workspace / ".harness-metrics.json"
    if metrics_path.is_file():
        try:
            metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            metrics = None

    files = sorted(path for path in workspace.rglob("*") if path.is_file() and not path.name.startswith("."))
    if not files and metrics is None:
        return None

    events: list[dict] = [
        {"type": "start", "prompt": str((metrics or {}).get("prompt") or "")},
        # Say why this transcript is thin: the reasoning itself was never written to disk
        # before event logs existed, and an unexplained empty feed reads as a bug.
        {
            "type": "message",
            "from": "harness",
            "kind": "restored",
            "text": (
                "Restored from disk. This run finished before the harness kept event logs, "
                "so only its files and usage were recorded."
            ),
        },
    ]

    # The ledger recorded who spent what; replay it so the team and runtime panels
    # are not blank on a run whose conversation was never logged.
    for agent in (metrics or {}).get("agents") or []:
        events.append(
            {
                "type": "usage",
                "agent": str(agent.get("id") or ""),
                "name": agent.get("name"),
                "role": agent.get("role"),
                "model": agent.get("model"),
                "phase": "restored",
                "prompt_tokens": int(agent.get("prompt_tokens") or 0),
                "completion_tokens": int(agent.get("completion_tokens") or 0),
                "total_tokens": int(agent.get("total_tokens") or 0),
                "ms": int(agent.get("ms") or 0),
                "calls": int(agent.get("calls") or 0),
                "thinking_chars": int(agent.get("thinking_chars") or 0),
            }
        )

    for path in files:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").count("\n") + 1
        except OSError:
            lines = 0
        events.append(
            {
                "type": "file_end",
                "path": str(path.relative_to(workspace)).replace("\\", "/"),
                "agent": "",
                "lines": lines,
                "revised": False,
            }
        )
    events.append({"type": "done", "metrics": metrics} if metrics else {"type": "done"})

    run = RunState(
        run_id=run_id,
        prompt=str((metrics or {}).get("prompt") or ""),
        specialists=[],
        kind=str((metrics or {}).get("kind") or "build"),
        fresh=False,
    )
    run.events = events
    run.done = True
    return run
