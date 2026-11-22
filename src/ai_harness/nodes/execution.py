"""Execution node: contracts first, then every specialist writes its files at once."""

from __future__ import annotations

import asyncio

from ..agents import executor
from ..config.settings import AGENT_STEPS, REASONING_CRITIC, REASONING_WORDS
from ..core.state import RunState
from ..models import Specialist, WorkUnit
from ..modules import agent_loop, context, goal, intent, llm, memory, progress, prompts, protected, thrash, tool_log
from . import verify


class FileSink:
    """Streams an agent's answer onto disk and registers every file it opens.

    One sink can carry several files: the executor splits the stream on
    ``<<<FILE path>>>`` and every section is opened, written and closed here, so a
    marker file is a real file — in the run's ledger, on disk and in the verifier.
    """

    def __init__(self, run: RunState, agent: Specialist, mode: str = "") -> None:
        self.run = run
        self.agent = agent
        self.mode = mode
        self.buffers: dict[str, str] = {}
        self.opened: set[str] = set()
        self.dropped: set[str] = set()

    @property
    def phase(self) -> str:
        return "revising" if self.mode == "rewrite" else "building"

    async def start(self, path: str) -> None:
        if path in self.opened or path in self.dropped:
            return
        if not path:
            return
        existing = str((self.run.files.get(path) or {}).get("content") or "")
        if protected.frozen(path, self.mode, existing):
            # The judge cannot be edited by the thing it judges: a revision that weakens a
            # test to make it pass is refused here, once, with the reason in the transcript.
            self.dropped.add(path)
            await self.run.emit({"type": "error", "agent": self.agent.id, "message": protected.describe(path)})
            return
        if not self.run.fm.can_write(path):
            self.dropped.add(path)
            await self.run.emit(
                {
                    "type": "error",
                    "agent": self.agent.id,
                    "message": f"{self.agent.name} tried to write {path!r}, which is outside the run workspace",
                }
            )
            return
        self.opened.add(path)
        self.buffers[path] = ""
        self.run.files[path] = {
            "agent": self.agent.id,
            "content": "",
            "done": False,
            "revised": self.mode == "rewrite",
        }
        async with tool_log.call(
            self.run, "file_manager.open", {"path": path}, agent=self.agent.id, phase=self.phase
        ) as call:
            self.run.fm.write(path, "")
            call.set_result("opened empty file")
        await self.run.emit(self._event("file_start", path))

    async def write(self, path: str, delta: str) -> None:
        if path in self.dropped:
            return
        if path not in self.opened:
            await self.start(path)
        if path in self.dropped or path not in self.opened:
            return
        self.buffers[path] += delta
        self.run.files[path].update(agent=self.agent.id, content=self.buffers[path])
        self.run.fm.write(path, self.buffers[path])
        await self.run.emit({"type": "file", "path": path, "delta": delta})

    async def finish(self, files: list[tuple[str, str]]) -> list[str]:
        """Persist each section's final text, close it, and report what shipped."""
        shipped: list[str] = []
        for path, content in files:
            if path in self.dropped or not content.strip():
                continue
            self.run.files[path] = {
                "agent": self.agent.id,
                "content": content,
                "done": True,
                "revised": self.mode == "rewrite",
            }
            async with tool_log.call(
                self.run,
                "file_manager.write",
                {"path": path, "bytes": len(content.encode("utf-8"))},
                agent=self.agent.id,
                phase=self.phase,
            ) as call:
                self.run.fm.write(path, content)
                call.set_result(f"{content.count(chr(10)) + 1} lines written")
            shipped.append(path)
            progress.tick(self.run, path)
            writes = thrash.note_write(str(getattr(self.run, "run_id", "") or ""), path)
            if writes == 4:
                await self.run.emit(
                    {
                        "type": "thrash",
                        "tool": "file_manager.write",
                        "path": path,
                        "count": writes,
                        "message": f"{path} has been written {writes} times — is the same fix being retried?",
                    }
                )
            await self.run.emit(self._event("file_end", path, lines=content.count("\n") + 1))
        return shipped

    def _event(self, kind: str, path: str, **extra) -> dict:
        event = {"type": kind, "agent": self.agent.id, "path": path, **extra}
        if self.mode:
            event["mode"] = self.mode
        if self.mode == "rewrite":
            event["revised"] = True
        return event


async def negotiate(run: RunState, units: list[WorkUnit]) -> None:
    """Each specialist publishes its interface; everyone builds against all of them."""
    await run.emit({"type": "phase", "phase": "contracts"})
    plan_text = "\n".join(f"- {u.file} ({u.role}, {u.title}): {u.objective}" for u in units)

    async def one(unit: WorkUnit) -> None:
        agent = next((s for s in run.specialists if s.id == unit.agent), run.specialists[0])
        await run.emit({"type": "status", "agent": agent.id, "status": "thinking"})
        try:
            text, usage = await llm.call(
                agent,
                [
                    {
                        "role": "system",
                        "content": prompts.CONTRACT.format(
                            name=agent.name, role=agent.role, prompt=run.prompt, plan=plan_text
                        ),
                    },
                    {"role": "user", "content": f"Your file: {unit.file}. State your contract."},
                ],
            )
        except llm.ProviderError as exc:
            await run.emit({"type": "error", "message": str(exc), "agent": agent.id})
            return
        await run.emit(run.track_usage(agent, "contracts", usage))
        run.contracts[agent.id] = text[:600]
        await run.emit({"type": "status", "agent": agent.id, "status": "idle"})
        await run.emit({"type": "message", "from": agent.id, "to": "all", "text": text[:400], "kind": "talk"})

    # Same rule as the writes below: one local runner serves one request at a time, so
    # firing every contract at once makes them queue behind each other and the last one
    # can outlast the provider read timeout. Ordering keeps the team and the contracts,
    # without the pile-up. A hosted roster still negotiates in parallel.
    if all(spec.is_local for spec in run.specialists):
        for unit in units:
            await one(unit)
        return
    await asyncio.gather(*(one(unit) for unit in units))


async def write_unit(run: RunState, unit: WorkUnit, notes: list[str]) -> None:
    agent = next((s for s in run.specialists if s.id == unit.agent), run.specialists[0])
    peers = context.peer_context(
        {s.name: run.contracts[s.id] for s in run.specialists if s.id in run.contracts and s.id != agent.id}
    )
    research = context.notes_context(notes)

    try:
        # Pass 1 — deliberation. Slow, visible, and it grounds the code.
        await run.emit({"type": "status", "agent": agent.id, "status": "thinking"})
        reasoning, think_usage = await executor.reason(
            agent,
            [
                {
                    "role": "system",
                    "content": prompts.REASON.format(
                        name=agent.name,
                        role=agent.role,
                        file=unit.file,
                        objective=unit.objective,
                        prompt=run.prompt,
                        contracts=peers,
                        notes=research,
                        lessons=memory.lessons(),
                        words=REASONING_WORDS * 2 if run.depth == "ultra" else REASONING_WORDS,
                    ),
                },
                {"role": "user", "content": f"Think through {unit.file} now."},
            ],
            on_think=lambda text: run.emit({"type": "think", "agent": agent.id, "delta": text}),
        )
        await run.emit(run.track_usage(agent, "reasoning", think_usage))

        # Pass 1.5 — a different specialist attacks the plan before any code exists.
        # Separation is the point: the model that wrote the plan is its worst judge.
        critique = ""
        if run.depth in ("deep", "ultra") and REASONING_CRITIC:
            # Prefer a dedicated reviewer; with none configured, another specialist is
            # still a better critic than the author grading their own plan.
            critic = next((s for s in run.specialists if s.role == "reviewer" and s.id != agent.id), None)
            critic = critic or next((s for s in run.specialists if s.id != agent.id), agent)
            await run.emit({"type": "status", "agent": critic.id, "status": "thinking"})
            critique, critic_usage = await executor.critique(
                critic,
                [
                    {
                        "role": "system",
                        "content": prompts.REASON_CRITIC.format(
                            prompt=run.prompt,
                            file=unit.file,
                            contracts=peers,
                            reasoning=context.clip(reasoning, 1100),
                        ),
                    },
                    {"role": "user", "content": f"Attack the plan for {unit.file}."},
                ],
                on_think=lambda text: run.emit({"type": "think", "agent": critic.id, "delta": text}),
            )
            await run.emit(run.track_usage(critic, "critique", critic_usage))
            await run.emit({"type": "status", "agent": critic.id, "status": "idle"})

            if run.depth == "ultra":
                # A second reader sees the plan and the first critique, and is asked for
                # what the first missed. Two independent attacks beat one longer one.
                second = next((s for s in run.specialists if s.id not in (agent.id, critic.id)), critic)
                await run.emit({"type": "status", "agent": second.id, "status": "thinking"})
                extra, extra_usage = await executor.critique(
                    second,
                    [
                        {
                            "role": "system",
                            "content": prompts.REASON_CRITIC.format(
                                prompt=run.prompt,
                                file=unit.file,
                                contracts=peers,
                                reasoning=context.clip(
                                    f"{reasoning}\n\nFirst reviewer:\n{critique}",
                                    1800,
                                ),
                            ),
                        },
                        {"role": "user", "content": f"Find what the first reviewer missed in {unit.file}."},
                    ],
                    on_think=lambda text: run.emit({"type": "think", "agent": second.id, "delta": text}),
                )
                await run.emit(run.track_usage(second, "critique-2", extra_usage))
                await run.emit({"type": "status", "agent": second.id, "status": "idle"})
                if extra:
                    critique = f"{critique}\n\nSecond reviewer:\n{extra}"

        # Pass 2 — the file itself, written against the agent's own plan. The answer may
        # carry extra files behind <<<FILE path>>> markers; each lands on its own path.
        #
        # A unit that wrote nothing used to be logged and skipped, leaving a file the plan
        # named empty — the failure the clean-state gate reports as "declared by the plan
        # but still empty". Every planned file gets a second ask, with the empty first
        # answer named, before the unit is given up.
        shipped: list[str] = []
        for attempt in range(2):
            sink = FileSink(run, agent)
            ask = f"Write {unit.file} now."
            if attempt:
                await run.emit(
                    {
                        "type": "message",
                        "from": "verify",
                        "text": f"{unit.file} was not written; asking {agent.name} again.",
                        "kind": "review",
                    }
                )
                ask = (
                    f"Your previous answer produced no file for {unit.file}. "
                    f"Write the complete file now, starting with <<<FILE {unit.file}>>>."
                )
            await run.emit({"type": "status", "agent": agent.id, "status": "writing"})
            files, code_usage = await executor.compose(
                agent,
                [
                    {
                        "role": "system",
                        "content": prompts.CODE.format(
                            name=agent.name,
                            role=agent.role,
                            file=unit.file,
                            objective=unit.objective,
                            prompt=run.prompt,
                            acceptance=intent.contract(getattr(run, "acceptance", None) or []),
                            goal=prompts.GOAL_CONTRACT.format(goal=goal.read(run)),
                            reasoning=context.clip(reasoning, 1400 if run.depth == "ultra" else 700),
                            critique=context.clip(critique, 1600 if run.depth == "ultra" else 700)
                            or "(no reviewer pass for this run)",
                            design=prompts.DESIGN_CONTRACT,
                            contracts=peers,
                            peer_files=context.files_context(run.files, exclude=unit.file),
                            lessons=memory.lessons(),
                            progress=progress.orient(run),
                        ),
                    },
                    {"role": "user", "content": ask},
                ],
                on_think=lambda text: run.emit({"type": "think", "agent": agent.id, "delta": text}),
                on_code=sink.write,
                on_marker=sink.start,
                default_path=unit.file,
            )
            await run.emit(run.track_usage(agent, "writing", code_usage))
            shipped = await sink.finish(files)
            if unit.file in shipped:
                break

        if unit.file not in shipped:
            await run.emit(
                {"type": "error", "message": f"{agent.name} produced no code for {unit.file}", "agent": agent.id}
            )
            await run.emit({"type": "status", "agent": agent.id, "status": "error"})
            return

        if run.depth in ("deep", "ultra"):
            # A deep build does not wait for the end-of-run review: the freshly written
            # file is checked now, and its author gets a bounded tool loop to fix it while
            # the file is still the thing they are thinking about. Writing every file once
            # and only looking at the tree at the end is what made a full-crew run shallow:
            # one bad file then poisons every file that imports it.
            # Only a file that does not parse is repaired here. Missing peers, an empty
            # sibling or a style warning are facts about the whole tree; the end-of-run
            # review owns them. Repairing them per file made Ultra spin on a stylesheet
            # that was waiting for the next component.
            def _unit_syntax() -> dict:
                report = verify.check(run.files, [unit.file])
                syntax = [
                    item for item in report.get("issues", []) if item.get("code") in ("syntax_error", "invalid_json")
                ]
                return {**report, "ok": not syntax, "issues": syntax, "errors": len(syntax), "warnings": 0}

            immediate = _unit_syntax()
            if not immediate.get("ok", True):
                await run.emit(
                    {
                        "type": "message",
                        "from": "verify",
                        "text": f"{unit.file} does not parse — repairing before the next file.",
                        "kind": "review",
                    }
                )

                async def _unit_check() -> dict:
                    return _unit_syntax()

                await agent_loop.repair(
                    run,
                    agent,
                    unit.file,
                    verify.as_findings(immediate),
                    _unit_check,
                    max_steps=AGENT_STEPS * 2 if run.depth == "ultra" else AGENT_STEPS,
                )

        lines = sum(run.files[path]["content"].count("\n") + 1 for path in shipped)
        unit.status = "done"
        unit.lines = run.files[unit.file]["content"].count("\n") + 1 if unit.file in run.files else 0
        await run.emit({"type": "status", "agent": agent.id, "status": "done"})
        await run.emit(
            {
                "type": "message",
                "from": agent.id,
                "text": f"{', '.join(shipped)} shipped · {lines} lines",
                "kind": "work",
            }
        )
    except llm.ProviderError as exc:
        await run.emit({"type": "status", "agent": agent.id, "status": "error"})
        await run.emit({"type": "error", "message": str(exc), "agent": agent.id})


async def run(run: RunState, units: list[WorkUnit], notes: list[str], negotiate_first: bool) -> None:
    if negotiate_first:
        await negotiate(run, units)
    await run.emit({"type": "phase", "phase": "building"})
    if all(spec.is_local for spec in run.specialists):
        # One local runner serves one request at a time: three concurrent streams wait
        # behind each other, and the last one can outlast the provider read timeout.
        # Ordering the writes keeps the same team, same files, without the queue.
        for unit in units:
            await write_unit(run, unit, notes)
        return
    await asyncio.gather(*(write_unit(run, unit, notes) for unit in units))
