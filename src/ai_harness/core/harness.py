"""Harness: the registry of runs, the run queue, and the HTTP boundary.

A run is not free to start the moment it is requested. Concurrency is capped so a
handful of specialists cannot flood a provider (or the machine) at once, and the queue
is bounded so a misbehaving client cannot park unbounded work in memory. A run that has
to wait says so with a `queue` event rather than looking hung.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

from ..config.settings import MAX_CONCURRENT_RUNS, MAX_QUEUED_RUNS, RUNS_ROOT
from ..core.chat import chat_task
from ..core.flock import flock_task
from ..core.orchestrator import Orchestrator
from ..core.state import RunState, restore_run
from ..models import Specialist


class HarnessBusy(RuntimeError):
    """The queue is full. The API answers 429 rather than pretending the run started."""


class Harness:
    def __init__(self) -> None:
        self.runs: dict[str, RunState] = {}
        self.concurrency = MAX_CONCURRENT_RUNS
        # Built on first use, inside the running loop: on 3.9 a Semaphore binds to the
        # loop that exists when it is constructed, and at import time there may be none.
        self._slots: asyncio.Semaphore | None = None
        self._gate_loop = None
        self._queued = 0
        self._running = 0

    def _gate(self) -> asyncio.Semaphore:
        """The semaphore for the loop we are running on, rebuilt if the loop changed."""
        loop = asyncio.get_event_loop()
        if self._slots is None or self._gate_loop is not loop:
            self._slots = asyncio.Semaphore(self.concurrency)
            self._gate_loop = loop
        return self._slots

    # ---------- start ----------

    def start(self, prompt: str, specialists: list[Specialist], depth: str = "deep") -> RunState:
        self._room()
        run_id = uuid.uuid4().hex[:12]
        run = RunState(run_id=run_id, prompt=prompt.strip(), specialists=specialists, depth=depth)
        return self._submit(run, lambda: Orchestrator(run).run_all())

    def resume(self, run_id: str, specialists: list[Specialist], depth: str = "deep") -> RunState | None:
        """Continue a run whose files and plan are on disk, without re-planning or writing.

        Returns the live run when it is still working, None when the id is unknown, and a
        freshly submitted run otherwise. Its terminal event is dropped from the in-memory
        transcript so the ui does not show a `done` above a run that is working again.
        """
        run = self.get(run_id)
        if run is None:
            return None
        if run.task and not run.task.done():
            return run
        if not specialists:
            raise HarnessBusy("a resumed run needs at least one configured specialist")
        self._room()
        run.specialists = specialists
        run.depth = depth if depth in ("off", "brief", "deep", "ultra", "quick") else run.depth
        run.done = False
        run.events = [event for event in run.events if event.get("type") not in ("done", "error")]
        return self._submit(run, lambda: Orchestrator(run).resume_all())

    def start_chat(self, specialist: Specialist, messages: list[dict[str, str]], depth: str = "brief") -> RunState:
        """A chat is a run: same registry, same queue, same usage ledger."""
        self._room()
        prompt = next((str(m.get("content", "")) for m in reversed(messages) if m.get("role") == "user"), "").strip()
        run = RunState(
            run_id=uuid.uuid4().hex[:12],
            prompt=prompt[:400],
            specialists=[specialist],
            depth=depth if depth in ("off", "brief", "deep", "ultra") else "brief",
            kind="chat",
        )
        return self._submit(run, lambda: chat_task(run, specialist, messages, run.depth))

    def start_flock(
        self, specialists: list[Specialist], messages: list[dict[str, str]], depth: str = "brief"
    ) -> RunState:
        """A flock is a chat with a team: one request split, answered in parallel, composed.

        It is a run like any other — the same registry, queue, budget and event stream —
        so a flock shows up in `/api/runs` and the metrics page exactly as a chat does.
        """
        self._room()
        prompt = next((str(m.get("content", "")) for m in reversed(messages) if m.get("role") == "user"), "").strip()
        run = RunState(
            run_id=uuid.uuid4().hex[:12],
            prompt=prompt[:400],
            specialists=list(specialists),
            depth=depth if depth in ("off", "brief", "deep", "ultra") else "brief",
            kind="flock",
        )
        return self._submit(run, lambda: flock_task(run, list(specialists), messages, run.depth))

    def _room(self) -> None:
        """Refuse before a workspace is created, so a rejected run leaves nothing behind."""
        if self._queued >= MAX_QUEUED_RUNS:
            raise HarnessBusy(
                f"{self.concurrency} run(s) are executing and {self._queued} are queued; retry once the queue drains"
            )

    def _submit(self, run: RunState, make_work) -> RunState:
        self._room()
        self._queued += 1
        self.runs[run.run_id] = run
        run.task = asyncio.create_task(self._launch(run, make_work))
        return run

    async def _launch(self, run: RunState, make_work) -> None:
        """Wait for a slot, then run. A cancelled waiter leaves a record behind."""
        acquired = False
        slots = self._gate()
        try:
            if slots.locked():
                await run.emit(
                    {
                        "type": "queue",
                        "position": self._queued,
                        "concurrency": self.concurrency,
                        "message": f"Waiting for a slot ({self._queued} queued, {self.concurrency} running)",
                    }
                )
            await slots.acquire()
            acquired = True
            self._running += 1
            self._queued = max(0, self._queued - 1)
            await make_work()
        except asyncio.CancelledError:
            if not acquired:
                self._queued = max(0, self._queued - 1)
                try:
                    await run.emit({"type": "error", "message": "Run cancelled while it was still queued."})
                except Exception:  # noqa: BLE001 - cancellation is already in flight
                    pass
            raise
        finally:
            if acquired:
                self._running = max(0, self._running - 1)
                slots.release()

    # ---------- inspect ----------

    def pending(self) -> int:
        """Runs waiting for a slot, including the one that just arrived."""
        return self._queued

    def occupying(self) -> int:
        return self._running

    def get(self, run_id: str) -> RunState | None:
        """Live runs come from memory; anything older is rebuilt from its event log on disk."""
        run = self.runs.get(run_id)
        if run is not None:
            return run
        restored = restore_run(run_id)
        if restored is not None:
            self.runs[run_id] = restored
        return restored

    def stop(self, run_id: str) -> bool:
        run = self.runs.get(run_id)
        if run and run.task and not run.task.done():
            run.task.cancel()
            return True
        return False

    def history(self, limit: int = 24) -> list[dict]:
        """Recent runs, live ones from memory and older ones from disk."""
        items: dict[str, dict] = {}
        for path in sorted(Path(RUNS_ROOT).iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
            if not path.is_dir():
                continue
            # The ledger on disk is authoritative: a run rebuilt from its event log has
            # no usage in memory, and must not overwrite its own recorded metrics.
            metrics = path / ".harness-metrics.json"
            if metrics.exists():
                try:
                    items[path.name] = json.loads(metrics.read_text(encoding="utf-8"))
                    continue
                except (OSError, json.JSONDecodeError):
                    pass
            run = self.runs.get(path.name)
            if run:
                items[path.name] = run.metrics()
        return list(items.values())
