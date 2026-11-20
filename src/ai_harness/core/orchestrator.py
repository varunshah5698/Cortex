"""Orchestrator: drives the nodes in order and owns the run-level guarantees."""

from __future__ import annotations

import asyncio

from ..config.settings import REPAIR_ROUNDS
from ..core.state import RunState, now_iso
from ..models import Specialist, WorkUnit
from ..modules import failures, llm, memory, progress, prompts, receipt, runtime
from ..modules.budget import BudgetExceeded
from ..nodes import execution, planning, research, review
from ..utils.logger import event_log


class Orchestrator:
    def __init__(self, run: RunState):
        self.run = run
        self.notes: list[str] = []
        self.review_text = ""

    def pick(self, *roles: str) -> Specialist:
        for role in roles:
            found = next((s for s in self.run.specialists if s.role == role), None)
            if found:
                return found
        return self.run.specialists[0]

    async def synthesize(self) -> None:
        agent = self.pick("synthesizer", "planner")
        await self.run.emit({"type": "phase", "phase": "synthesizing"})
        await self.run.emit({"type": "status", "agent": agent.id, "status": "thinking"})
        roster = "; ".join(
            f"{path} by {next((s.name for s in self.run.specialists if s.id == meta.get('agent')), 'agent')}"
            for path, meta in self.run.files.items()
        )
        try:
            text, usage = await llm.call(
                agent,
                [
                    {"role": "system", "content": prompts.SYNTHESIS},
                    {
                        "role": "user",
                        "content": f"Request: {self.run.prompt}\nFiles: {roster}\nReviewer: {self.review_text[:800]}",
                    },
                ],
            )
            await self.run.emit(self.run.track_usage(agent, "synthesis", usage))
            if text:
                await self.run.emit({"type": "message", "from": agent.id, "text": text[:800], "kind": "report"})
        except llm.ProviderError as exc:
            await self.run.emit({"type": "error", "message": str(exc), "agent": agent.id})
        await self.run.emit({"type": "status", "agent": agent.id, "status": "done"})

    def persist_metrics(self) -> None:
        self.run.save_metrics()

    async def conclude(self, units: list[WorkUnit], reviewer_agent: Specialist) -> None:
        """Review, repair, and the only place a run may be reported `done`.

        Shared by a fresh run and a resumed one: the tail is where the guarantees live, so
        there is exactly one copy of it.
        """
        self.review_text = await review.run(self.run, reviewer_agent, units)

        # A run may not finish on a failing check. The review pass already revised once;
        # give the facts one bounded round of their own, then refuse to report success.
        # `error` is a terminal event, so the UI and the ledger both see a run that stopped
        # short instead of a green run with broken files.
        report = self.run.verification or {}
        if not report.get("ok", True):
            rounds = REPAIR_ROUNDS * 2 if self.run.depth == "ultra" else REPAIR_ROUNDS
            report = await review.repair(self.run, reviewer_agent, units, rounds=rounds)
        if not report.get("ok", True):
            self.run.done = False
            self.persist_metrics()
            failed = report.get("errors", 0)
            # Step 4, as machinery: the table row and the replayable case are written at the
            # moment of failure, not remembered later.
            failures.record(self.run, report)
            failures.promote(self.run, report)
            receipt.write(self.run)
            memory.append(self.run, f"stopped short — {failed} verification error(s)")
            event_log(self.run.run_id, "verification-failed", errors=failed, issues=report.get("issues", [])[:12])
            await self.run.emit(
                {
                    "type": "error",
                    "blocked": True,
                    "verification": report,
                    "message": (
                        f"Run stopped short: {failed} verification error(s) still fail after repair. "
                        "Nothing was reported as done — see the findings for the files that need work."
                    ),
                }
            )
            return

        await self.synthesize()

        self.run.done = True
        self.persist_metrics()
        receipt.write(self.run)
        memory.append(self.run, "shipped" if report.get("ok", True) else "shipped with warnings")
        await self.run.emit(
            {
                "type": "done",
                "files": self.run.file_summary(),
                "metrics": self.run.metrics(),
                # `done` is a verification verdict, not a build report: the pipeline never
                # compiles anything, so both halves are carried explicitly and a run that
                # was only checked says `previewed: false, build: null`.
                "build": self.run.build,
                "previewed": self.run.previewed,
                "finished": now_iso(),
            }
        )
        event_log(self.run.run_id, "done", files=len(self.run.files))

    async def run_all(self) -> None:
        # Bind this run for every nested call (see modules/runtime.py).
        runtime.bind(self.run)
        planner = self.pick("planner")
        reviewer_agent = self.pick("reviewer", "planner")
        try:
            await self.run.emit({"type": "start", "prompt": self.run.prompt, "created": self.run.created})
            event_log(self.run.run_id, "start", prompt=self.run.prompt, depth=self.run.depth)

            # A local daemon that is not running cannot write a file; fail before the
            # planner spends three minutes discovering that. Then load the model once,
            # so the first file does not pay the cold-start.
            warmed: set[tuple[str, str]] = set()
            for spec in self.run.specialists:
                if not spec.is_local:
                    continue
                ok, detail = await llm.reachable(spec)
                if not ok:
                    message = (
                        f"{spec.name}: the local provider at {spec.baseUrl} is not answering "
                        f"({detail}). Start it (for example `ollama serve`), then run again."
                    )
                    event_log(self.run.run_id, "error", error=message)
                    failures.record(self.run, None, message)
                    await self.run.emit({"type": "error", "message": message, "agent": spec.id})
                    return
                key = ((spec.baseUrl or "").rstrip("/"), spec.model)
                if key in warmed:
                    continue
                warmed.add(key)
                await self.run.emit(
                    {
                        "type": "message",
                        "from": "harness",
                        "kind": "notice",
                        "text": f"Loading {spec.model} before the first call — this is the slow part.",
                    }
                )
                ok, detail = await llm.warm(spec)
                if not ok:
                    message = f"{spec.name}: {spec.model} did not load ({detail}). Check `ollama list`."
                    event_log(self.run.run_id, "error", error=message)
                    failures.record(self.run, None, message)
                    await self.run.emit({"type": "error", "message": message, "agent": spec.id})
                    return

            all_local = all(spec.is_local for spec in self.run.specialists)
            units, files, questions = await planning.run(self.run, planner)
            self.run.units = units

            if self.run.depth != "quick":
                researcher = self.pick("research", "planner")
                self.notes = await research.run(self.run, researcher, questions)
                if all_local:
                    await self.run.emit(
                        {
                            "type": "message",
                            "from": "harness",
                            "kind": "notice",
                            "text": (
                                "Deep local crew: research and contracts run too; every call "
                                "is queued on the one local model, so this will take a while."
                            ),
                        }
                    )

            await execution.run(
                self.run,
                units,
                self.notes,
                negotiate_first=self.run.depth != "quick",
            )
            await self.conclude(units, reviewer_agent)
        except asyncio.CancelledError:
            self.persist_metrics()
            receipt.write(self.run)
            failures.record(self.run, self.run.verification, "cancelled")
            await self.run.emit({"type": "error", "message": "Run cancelled"})
            raise
        except runtime.RunDeadlineExceeded as exc:
            # Time is a bound like money, but not the same stop: nothing was refused for
            # cost, the run simply ran out of wall clock. It ends with what it already
            # wrote and lists it, instead of leaving a subscriber waiting on a `done`
            # that a half-finished tree has not earned.
            self.persist_metrics()
            files = self.run.file_summary()
            names = ", ".join(str(item["path"]) for item in files) or "no files"
            failures.record(self.run, self.run.verification, str(exc))
            receipt.write(self.run)
            event_log(self.run.run_id, "timeout", error=str(exc), files=len(files))
            await self.run.emit(
                {
                    "type": "error",
                    "blocked": True,
                    "timed_out": True,
                    "files": files,
                    "message": (f"Run stopped at its time limit — {exc}. {len(files)} file(s) are on disk: {names}"),
                }
            )
        except BudgetExceeded as exc:
            self.persist_metrics()
            failures.record(self.run, self.run.verification, str(exc))
            receipt.write(self.run)
            event_log(self.run.run_id, "budget", error=str(exc), **self.run.budget.as_dict())
            await self.run.emit({"type": "budget", **self.run.budget.as_dict(), "message": str(exc)})
            await self.run.emit({"type": "error", "message": f"Run stopped by its budget — {exc}"})
        except Exception as exc:  # noqa: BLE001 - any node failure must reach the UI
            self.persist_metrics()
            failures.record(self.run, self.run.verification, f"{type(exc).__name__}: {exc}")
            receipt.write(self.run)
            event_log(self.run.run_id, "error", error=f"{type(exc).__name__}: {exc}")
            await self.run.emit({"type": "error", "message": f"{type(exc).__name__}: {exc}"})

    async def resume_all(self) -> None:
        """Continue a run from what is on disk, without re-planning or re-writing it.

        The plan, the files and the checklist are already persisted, so a run that died
        after building can be reviewed, repaired and reported on — the expensive half is
        not paid for twice. A run with no plan on disk has nothing to resume, and says so
        rather than starting over under the same id.
        """
        runtime.bind(self.run)
        try:
            units = planning.load_plan(self.run)
            if not units:
                await self.run.emit(
                    {
                        "type": "error",
                        "resumed": False,
                        "message": "Nothing to resume: this run never saved a plan. Start a new run instead.",
                    }
                )
                return
            reviewer_agent = self.pick("reviewer", "planner")

            # Files come back from disk, not from memory: whatever survived the restart is
            # the work, and a file that is empty is honestly still open.
            for item in self.run.fm.tree():
                path = str(item["path"])
                if path in (progress.FILENAME, receipt.FILENAME):
                    continue
                content = self.run.fm.read(path)
                self.run.files[path] = {
                    "agent": next((unit.agent for unit in units if unit.file == path), ""),
                    "content": content,
                    "done": bool(content.strip()),
                }
            self.run.units = units

            await self.run.emit(
                {"type": "start", "prompt": self.run.prompt, "created": self.run.created, "resumed": True}
            )
            await self.run.emit(
                {
                    "type": "message",
                    "from": "harness",
                    "kind": "resumed",
                    "text": (
                        f"Resumed `{self.run.run_id}` from disk: {len(self.run.files)} file(s) restored, "
                        "plan kept. Skipping planning, research and writing."
                    ),
                }
            )
            await self.run.emit(
                {
                    "type": "plan",
                    "objective": "",
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
            event_log(self.run.run_id, "resume", files=len(self.run.files))
            await self.conclude(units, reviewer_agent)
        except Exception as exc:  # noqa: BLE001 - a resumed run has the same duty to report
            self.persist_metrics()
            failures.record(self.run, self.run.verification, f"{type(exc).__name__}: {exc}")
            receipt.write(self.run)
            event_log(self.run.run_id, "error", error=f"{type(exc).__name__}: {exc}")
            await self.run.emit({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
