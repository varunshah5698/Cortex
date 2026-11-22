"""Orchestrator: drives the nodes in order and owns the run-level guarantees."""

from __future__ import annotations

import asyncio
import re
import traceback

from ..config.settings import AUTO_PREVIEW, REPAIR_ROUNDS
from ..core.state import RunState, now_iso
from ..models import Specialist, WorkUnit
from ..modules import (
    cleanstate,
    cloud_sync,
    exit,
    failures,
    features,
    goal,
    handoff,
    llm,
    memory,
    progress,
    prompts,
    receipt,
    rubric,
    runtime,
    tasks,
    trajectory,
)
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

    def measured_facts(self) -> str:
        """What the harness actually measured, for the answer to be written from.

        The answer is prose, so it can invent a section or a count. This block is the only
        thing it may draw on, and every line came from the files on disk or from the
        deterministic checks - never from the reviewer's opinion.
        """
        lines: list[str] = []
        report = self.run.verification or {}
        if report:
            issues = report.get("issues", [])
            state = "passed" if report.get("ok") else f"failed with {report.get('errors', len(issues))} error(s)"
            lines.append(f"Deterministic checks: {state} over {report.get('checked', len(self.run.files))} file(s).")
            for item in issues[:4]:
                lines.append(f"  open issue: {item.get('file')} - {item.get('detail')}")
        for item in self.run.file_summary():
            lines.append(f"File {item['path']}: {item['lines']} lines, {item['bytes']} bytes.")
        for path, meta in sorted(self.run.files.items()):
            content = meta.get("content", "")
            if not content:
                continue
            # The page's own structure is the honest description of what it contains.
            sections = re.findall(r"<section[^>]*>", content, flags=re.I)
            headings = re.findall(r"<h[12][^>]*>(.*?)</h[12]>", content, flags=re.I | re.S)
            clean = [re.sub(r"<[^>]+>", "", h).strip() for h in headings]
            clean = [h for h in clean if h][:8]
            if sections:
                lines.append(f"{path} has {len(sections)} <section> block(s).")
            if clean:
                lines.append(f"{path} headings: " + "; ".join(clean))
            media = len(re.findall(r"@media", content, flags=re.I))
            if media:
                lines.append(f"{path} has {media} responsive breakpoint(s).")
        if self.run.browser:
            lines.append(f"Browser check: {'clean' if self.run.browser.get('ok') else 'found issues'}.")
        return "\n".join(lines) or "No measured facts available."

    async def synthesize(self) -> None:
        agent = self.pick("synthesizer", "planner")
        await self.run.emit({"type": "phase", "phase": "synthesizing"})
        await self.run.emit({"type": "status", "agent": agent.id, "status": "thinking"})
        facts = self.measured_facts()
        try:
            text, usage = await llm.call(
                agent,
                [
                    {"role": "system", "content": prompts.SYNTHESIS},
                    {
                        "role": "user",
                        "content": (
                            f"The person asked for: {self.run.prompt}\n\n"
                            f"Measured facts (the only things you may state):\n{facts}"
                        ),
                    },
                ],
            )
            await self.run.emit(self.run.track_usage(agent, "synthesis", usage))
            if text:
                # The answer is the deliverable; a sentence cut in half is worse than a long
                # one. The prompt bounds it at 120 words, so this cap is a guard, not a cut.
                await self.run.emit(
                    {"type": "message", "from": agent.id, "text": text.strip()[:4000], "kind": "report"}
                )
        except llm.ProviderError as exc:
            await self.run.emit({"type": "error", "message": str(exc), "agent": agent.id})
        await self.run.emit({"type": "status", "agent": agent.id, "status": "done"})

    def persist_metrics(self) -> None:
        self.run.save_metrics()

    async def _auto_preview(self) -> None:
        """Serve the finished tree so a shipped run is viewable without a second click.

        The pipeline verifies and never compiles, so a run used to end at `done` with
        `previewed: false` and nothing on a port: the files existed and the person who
        asked for them had no way to look at them.

        This is not a way around the approval gate. `runner.start` still stops at the
        first `ask` command (an install, a build, a model-authored server) and reports
        `needs_approval`; the UI keeps its Approve button for exactly that. What changes is
        that a tree needing no install — a static page, the common case — is served
        immediately instead of waiting for a click nobody knew to make.
        """
        if not AUTO_PREVIEW:
            return
        workspace = getattr(self.run, "workspace", None)
        if workspace is None or not self.run.files:
            return
        # Only a run that actually shipped gets served. A blocked or failed run has a tree
        # the harness has already refused to vouch for, and putting it on a port would
        # present a rejected build as viewable.
        report = self.run.verification or {}
        if report.get("ok") is not True:
            return
        try:
            from ..modules import runner  # local: runner imports nothing from core

            # No `on_event`: the runner's own progress and error events belong to a serve
            # the operator asked for, not to the run's transcript. Wiring them in let a
            # preview that could not start append an `error` to a run that had already
            # shipped, which read as a failed build.
            service = await runner.start(
                self.run.run_id,
                workspace,
                approved=False,
            )
        except Exception as exc:  # noqa: BLE001 - a preview must never fail a finished run
            await self.run.emit(
                {
                    "type": "message",
                    "from": "harness",
                    "kind": "notice",
                    "text": f"The build is done; the preview could not start ({type(exc).__name__}: {exc}).",
                }
            )
            return
        if service.state == "ready":
            # The ledger is what a reload and the metrics page read. A preview that is
            # live on a port but recorded as `previewed: false` is two answers to one
            # question, and the `done` event below is the one the UI sees first.
            self.run.previewed = True
            self.run.build = {
                "state": service.state,
                "kind": getattr(service, "kind", "static"),
                "detail": service.detail,
                "at": now_iso(),
            }
            self.persist_metrics()
        await self.run.emit(
            {
                "type": "message",
                "from": "harness",
                "kind": "notice",
                "text": (
                    f"Preview {service.state}: {service.detail}"
                    if service.state != "needs_approval"
                    else "Preview needs your approval before it can run this build."
                ),
            }
        )

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
        # `ok` must be present and true. `report.get("ok", True)` treated a missing key as
        # clean, so a verification report that never ran (or answered `{"errors": 3}`) was
        # reported to the operator as a shipped run.
        report = self.run.verification or {}
        if report.get("ok") is not True:
            rounds = REPAIR_ROUNDS
            report = await review.repair(self.run, reviewer_agent, units, rounds=rounds)
        if report.get("ok") is not True:
            memory.learn(report)
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
            await asyncio.to_thread(
                cloud_sync.finish,
                self.run.run_id,
                "blocked",
                f"{failed} verification error(s) still fail after repair",
                failed,
            )
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

        # The clean-state gate (Lecture 12): the verifier asked whether the tree holds
        # together; this asks whether the run left the next one a mess — an empty planned
        # file, a feature passing without evidence, no progress record, no handoff. It is
        # mechanical and cheap, so it runs before the goal gate, which may spend a call.
        clean = cleanstate.check(self.run)
        if clean.get("issues"):
            await self.run.emit({"type": "cleanstate", **clean})
        if not clean.get("ok", True):
            self.run.done = False
            # Every ending gets a name, including this one: a gate refused `done`, which
            # is `unverified`, and a blocked run is exactly what a trajectory is for.
            self.run.exit_status = exit.UNVERIFIED
            self.persist_metrics()
            handoff.write(self.run)
            receipt.write(self.run)
            detail = (clean.get("issues") or [{}])[0].get("detail") or "clean-state check failed"
            trajectory.write(self.run, status=exit.UNVERIFIED, submission=detail, report=report)
            memory.append(self.run, f"clean-state check failed — {clean.get('errors', 0)} error(s)")
            event_log(self.run.run_id, "clean-state-failed", issues=clean.get("issues", [])[:12])
            await asyncio.to_thread(
                cloud_sync.finish,
                self.run.run_id,
                "blocked",
                f"{clean.get('errors', 0)} clean-state error(s)",
                clean.get("errors", 0),
            )
            await self.run.emit(
                {
                    "type": "error",
                    "blocked": True,
                    "cleanstate": clean,
                    "message": (
                        f"Run stopped short: the clean-state check found {clean.get('errors', 0)} error(s). "
                        "Nothing was reported as done — see the findings for what is still open."
                    ),
                }
            )
            return

        # The goal gate: the verifier answered "does this tree hold together", which is not
        # the question the person asked. This asks whether the goal the run wrote down
        # before it started is actually met, and it is allowed to refuse the stop. It runs
        # after the verifier because a structurally broken tree is already a refusal, and
        # a gate that reports both at once makes the transcript harder to act on.
        verdict = await self.check_goal(reviewer_agent)
        if verdict is not None and verdict.status == goal.UNMET:
            self.run.done = False
            self.run.exit_status = exit.UNVERIFIED
            self.persist_metrics()
            self.run.goal = verdict.public()
            handoff.write(self.run)
            receipt.write(self.run)
            trajectory.write(self.run, status=exit.UNVERIFIED, submission=verdict.reason, report=report)
            memory.append(self.run, f"goal not met — {verdict.reason}")
            event_log(self.run.run_id, "goal-unmet", reason=verdict.reason, missing=verdict.missing[:8])
            await asyncio.to_thread(
                cloud_sync.finish,
                self.run.run_id,
                "blocked",
                f"goal not met: {verdict.reason}",
                0,
            )
            await self.run.emit(
                {
                    "type": "error",
                    "blocked": True,
                    "goal": verdict.public(),
                    "message": (
                        f"Run stopped short: the goal was not met — {verdict.reason}. Nothing was reported as done."
                    ),
                }
            )
            return

        # The evaluator rubric (Lecture 11): the mechanical gates are done, so this is the
        # only question left — was this a good session? It is opt-in and it can only make
        # the verdict stricter, never more generous, so it cannot turn a blocked run green.
        grade = await self.grade(reviewer_agent)
        if grade is not None and grade.verdict == rubric.BLOCK:
            self.run.done = False
            self.run.exit_status = exit.UNVERIFIED
            self.persist_metrics()
            handoff.write(self.run)
            receipt.write(self.run)
            trajectory.write(self.run, status=exit.UNVERIFIED, submission=grade.missing, report=report)
            memory.append(self.run, f"rubric blocked the run — {grade.missing or 'see the scores'}")
            event_log(self.run.run_id, "rubric-blocked", total=grade.total, missing=grade.missing[:200])
            await asyncio.to_thread(
                cloud_sync.finish,
                self.run.run_id,
                "blocked",
                f"rubric blocked: {grade.missing or 'scores too low'}",
                0,
            )
            await self.run.emit(
                {
                    "type": "error",
                    "blocked": True,
                    "rubric": grade.public(),
                    "message": (
                        f"Run stopped short: the evaluator scored this session {grade.total}/12 and blocked it. "
                        f"{grade.missing or 'See the scores.'}"
                    ),
                }
            )
            return

        await self.synthesize()

        self.run.done = True
        self.persist_metrics()
        # Start the preview before the receipt is written, so a run that shipped is
        # immediately viewable instead of waiting for someone to press Run. A run used to
        # end at `done` with `previewed: false` and nothing serving the files, which read
        # as "the harness built it and then hid it".
        await self._auto_preview()
        # The handoff is written before the receipt, so a run that ends here leaves the
        # next one a starting point even if the receipt write is the thing that fails.
        handoff.write(self.run)
        receipt.write(self.run)
        memory.append(self.run, "shipped" if report.get("ok", True) else "shipped with warnings")
        status = exit.of(True, report)
        self.run.exit_status = status
        trajectory.write(self.run, status=status, submission=self.run.answer, report=report)
        await self.run.emit(
            {
                "type": "done",
                "files": self.run.file_summary(),
                "metrics": self.run.metrics(),
                # The typed status is the machine-readable half of `done`: a reader that
                # only wants "did this run succeed, and if not why" does not have to
                # parse the transcript to find out.
                "exit_status": status,
                "build": self.run.build,
                "previewed": self.run.previewed,
                "finished": now_iso(),
            }
        )
        event_log(self.run.run_id, "done", files=len(self.run.files))
        await asyncio.to_thread(
            cloud_sync.finish,
            self.run.run_id,
            "done",
            f"{len(self.run.files)} file(s) verified",
            0,
        )

    async def check_goal(self, agent: Specialist):
        """Ask the goal gate whether this run met what it set out to do.

        Returns a `goal.Verdict` or None when the gate is off. The evaluator is a model
        call, so it is only reached when no mechanical sensor covers the goal. The gate
        refuses once and the run stops: it does not loop, because a gate that can always
        say "not yet" is a loop with no exit. A `met` or `unknown` verdict lets the run
        finish exactly as it did before this gate existed.
        """
        if not goal.GOAL_ENABLED:
            return None
        statement = goal.read(self.run)
        self.run.goal_text = statement
        verdict = await goal.evaluate(self.run, agent, self._goal_ask(agent))
        self.run.goal = verdict.public()
        await self.run.emit({"type": "goal", **verdict.public()})
        return verdict

    async def grade(self, agent: Specialist):
        """Score the session with the evaluator rubric, or None when it is off.

        The rubric is a model call by a specialist that did not write the work, so it is
        opt-in. It cannot make a verdict more generous: `rubric.parse` takes the stricter
        of the total's implied verdict and the model's stated one, and a malformed answer
        is `unknown`, which never blocks and never approves.
        """
        if not rubric.RUBRIC_ENABLED:
            return None
        score = await rubric.evaluate(self.run, agent, self._rubric_ask(agent))
        await self.run.emit({"type": "rubric", **score.public()})
        return score

    def _rubric_ask(self, agent: Specialist):
        """The evaluator call for the rubric, or None when there is no model to make it."""

        async def ask(system: str, user: str) -> str:
            text, usage = await llm.call(
                agent,
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
            )
            await self.run.emit(self.run.track_usage(agent, "rubric", usage))
            return text

        return ask

    def _goal_ask(self, agent: Specialist):
        """The evaluator call, or None when there is no model to make it with."""

        async def ask(system: str, user: str) -> str:
            text, usage = await llm.call(
                agent,
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
            )
            await self.run.emit(self.run.track_usage(agent, "goal-gate", usage))
            return text

        return ask

    async def run_all(self) -> None:
        # Bind this run for every nested call (see modules/runtime.py).
        runtime.bind(self.run)
        planner = self.pick("planner")
        reviewer_agent = self.pick("reviewer", "planner")
        try:
            await self.run.emit({"type": "start", "prompt": self.run.prompt, "created": self.run.created})
            event_log(self.run.run_id, "start", prompt=self.run.prompt, depth=self.run.depth)
            # The standing prompt text is paid on every call, so its size competes with
            # the model's own output budget. It was measured and tested but never checked
            # during a run, which made the cap a number in a test rather than a fact about
            # this run. Say it once, at the start, where an operator will see it.
            over = prompts.over_always_on_budget()
            if over:
                event_log(self.run.run_id, "prompt-budget", detail=over)
                await self.run.emit({"type": "message", "from": "harness", "kind": "notice", "text": over})
            # Best-effort mirror: a Convex outage must not fail a run that is already
            # executing on disk. See modules/cloud_sync.py.
            await asyncio.to_thread(cloud_sync.start, self.run.run_id, self.run.prompt, self.run.depth)

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
            # The task graph and the goal are written before any file exists, so a resume
            # judges the same goal and the same order rather than re-deriving both.
            seeded = tasks.seed(self.run, units)
            if seeded:
                await self.run.emit(
                    {
                        "type": "tasks",
                        "tasks": [task.public() for task in seeded],
                        "summary": tasks.summary(self.run),
                    }
                )
            # The feature list is the scope gate (Lecture 08): one active feature, and
            # `passing` only through recorded evidence. Seeded from the same plan, so the
            # two views of the work cannot disagree about what was asked for.
            feature_seed = features.seed(self.run, units)
            if feature_seed:
                await self.run.emit(
                    {
                        "type": "features",
                        "features": [feature.public() for feature in feature_seed],
                        "summary": features.summary(self.run),
                    }
                )
            goal.write(self.run, goal.derive(self.run))

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
            self.run.exit_status = exit.CANCELLED
            trajectory.write(self.run, status=exit.CANCELLED, submission="cancelled")
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
            self.run.exit_status = exit.TIME
            trajectory.write(self.run, status=exit.TIME, submission=str(exc))
            await self.run.emit(
                {
                    "type": "error",
                    "blocked": True,
                    "timed_out": True,
                    "exit_status": exit.TIME,
                    "files": files,
                    "message": (f"Run stopped at its time limit — {exc}. {len(files)} file(s) are on disk: {names}"),
                }
            )
        except BudgetExceeded as exc:
            self.persist_metrics()
            failures.record(self.run, self.run.verification, str(exc))
            receipt.write(self.run)
            event_log(self.run.run_id, "budget", error=str(exc), **self.run.budget.as_dict())
            self.run.exit_status = exit.LIMITS
            trajectory.write(self.run, status=exit.LIMITS, submission=str(exc))
            await self.run.emit({"type": "budget", **self.run.budget.as_dict(), "message": str(exc)})
            await self.run.emit(
                {"type": "error", "exit_status": exit.LIMITS, "message": f"Run stopped by its budget — {exc}"}
            )
        except Exception as exc:  # noqa: BLE001 - any node failure must reach the UI
            self.persist_metrics()
            # The traceback goes to the log, not the UI: a bare `ValueError: too many
            # values to unpack` in a toast is unfindable, and the operator reading the
            # room should not have to reproduce the run to learn which line raised it.
            event_log(self.run.run_id, "traceback", error=traceback.format_exc()[-4000:])
            failures.record(self.run, self.run.verification, f"{type(exc).__name__}: {exc}")
            receipt.write(self.run)
            event_log(self.run.run_id, "error", error=f"{type(exc).__name__}: {exc}")
            status = exit.classify(exc)
            self.run.exit_status = status
            trajectory.write(self.run, status=status, submission=f"{type(exc).__name__}: {exc}")
            await self.run.emit({"type": "error", "exit_status": status, "message": f"{type(exc).__name__}: {exc}"})

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
            self.run.planned_files = [unit.file for unit in units]
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
