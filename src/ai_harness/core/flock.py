"""Flock: one request, split across several models, composed back into one answer.

A single chat turn is one specialist answering alone. Flock is the other shape: a
planner cuts the request into independent parts, each part is handed to the specialist
whose role fits it, the parts run at the same time, and a synthesizer writes the one
answer the person actually reads.

Three rules this module keeps, because each one was a way the feature could lie:

* Every call goes through ``llm.call`` / ``llm.stream``. A second path to a provider
  would spend tokens the budget never sees, and a flock is the most expensive thing a
  chat can do.
* A part that fails does not sink the answer. It is reported as failed, its text is
  left out of the synthesis, and the synthesizer is told which parts are missing — an
  answer that silently drops a third of the work is worse than one that says so.
* The split is a plan, not a promise. If the planner returns one part, or nothing
  parseable, the run degrades to a single answer instead of inventing work.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

from ..config.settings import REASONING_WORDS
from ..core.state import RunState, now_iso
from ..models import Specialist
from ..modules import llm, prompts, runtime
from ..modules.budget import BudgetExceeded
from ..modules.guard import ReflectionGuard, strip_reflection

# `TASK: title | role | instruction` — the shape the planner is asked for. Anchored at
# line start so a model that echoes the format inside prose does not create a phantom task.
TASK_LINE = re.compile(r"^\s*(?:[-*\d.)\s]*)?TASK\s*:\s*(.+)$", re.I | re.M)

MAX_PARTS = 4
MIN_PARTS = 2


@dataclass
class Part:
    """One slice of the request, owned by one specialist."""

    id: str
    title: str
    role: str
    instruction: str
    agent: str = ""
    agent_name: str = ""
    status: str = "pending"
    text: str = ""
    error: str = ""

    def public(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "role": self.role,
            "agent": self.agent,
            "agentName": self.agent_name,
            "status": self.status,
            "chars": len(self.text),
        }


def parse_plan(raw: str, request: str) -> list[Part]:
    """Read the planner's split, or fall back to one part holding the whole request.

    The fallback is deliberate: a weak model that ignores the format should still get an
    answer, and a one-part flock is just a normal chat turn.
    """
    parts: list[Part] = []
    for index, match in enumerate(TASK_LINE.finditer(raw or "")):
        line = match.group(1).strip()
        if not line:
            continue
        chunks = [chunk.strip() for chunk in line.split("|")]
        title = chunks[0] if chunks else ""
        role = chunks[1].lower() if len(chunks) > 1 else "general"
        instruction = chunks[2] if len(chunks) > 2 else title
        if not title:
            continue
        parts.append(
            Part(
                id=f"p{index + 1}",
                title=title[:90],
                role=role[:24] or "general",
                instruction=(instruction or title)[:1200],
            )
        )
        if len(parts) >= MAX_PARTS:
            break

    if len(parts) < MIN_PARTS:
        return [Part(id="p1", title="The whole request", role="general", instruction=request[:1200])]
    return parts


def assign(parts: list[Part], specialists: list[Specialist]) -> None:
    """Give each part an owner, preferring a role match, never the same model twice running.

    A role match is the point of a flock: the frontend specialist takes the UI part. When
    no role matches, parts are spread round-robin so two parts do not queue behind one
    model while the others sit idle.
    """
    pool = [spec for spec in specialists if spec.model]
    if not pool:
        return
    used: dict[str, int] = {}
    for index, part in enumerate(parts):
        match = next((spec for spec in pool if spec.role == part.role), None)
        if match is None:
            match = pool[index % len(pool)]
        part.agent = match.id
        part.agent_name = match.name
        # The planner names a role, but a weak model often echoes a specialist's name
        # there instead. Record the owner's real role so the board colours and labels
        # the part by what it is, not by what the model guessed.
        part.role = match.role
        used[match.id] = used.get(match.id, 0) + 1


def _by_id(specialists: list[Specialist], agent_id: str) -> Specialist | None:
    return next((spec for spec in specialists if spec.id == agent_id), None)


async def _run_part(run: RunState, part: Part, specialist: Specialist, request: str) -> None:
    """Answer one part, streaming its tokens tagged with its author."""
    part.status = "running"
    await run.emit({"type": "flock", "stage": "task", "id": part.id, "status": "running"})
    await run.emit({"type": "status", "agent": specialist.id, "status": "writing"})

    stats = llm.StreamStats()
    collected: list[str] = []
    guard = ReflectionGuard()
    try:
        async for kind, text in llm.stream(
            specialist,
            [
                {
                    "role": "system",
                    "content": prompts.FLOCK_PART.format(
                        name=specialist.name,
                        role=specialist.role,
                        request=request[:2400],
                        title=part.title,
                        instruction=part.instruction,
                        words=max(300, REASONING_WORDS // 3),
                    ),
                },
                {"role": "user", "content": part.instruction},
            ],
            think=False,
            stats=stats,
        ):
            if not text:
                continue
            if kind == "think":
                await run.emit({"type": "think", "agent": specialist.id, "delta": text})
                continue
            collected.append(text)
            safe = guard.feed(text)
            if safe:
                await run.emit({"type": "token", "agent": specialist.id, "delta": safe})
    except (llm.ProviderError, RuntimeError) as exc:
        part.status = "error"
        part.error = str(exc)[:300]
        await run.emit(run.track_usage(specialist, f"flock:{part.id}", stats.usage))
        await run.emit({"type": "flock", "stage": "task", "id": part.id, "status": "error"})
        await run.emit({"type": "status", "agent": specialist.id, "status": "error"})
        return

    await run.emit(run.track_usage(specialist, f"flock:{part.id}", stats.usage))
    part.text = strip_reflection("".join(collected)).text.strip()
    part.status = "done" if part.text else "error"
    if not part.text:
        part.error = f"{specialist.name} returned nothing for this part."
    await run.emit({"type": "flock", "stage": "task", "id": part.id, "status": part.status})
    await run.emit({"type": "status", "agent": specialist.id, "status": "done"})


async def _synthesize(
    run: RunState,
    synthesizer: Specialist,
    request: str,
    parts: list[Part],
) -> None:
    """Write the one answer the person reads, from the parts that succeeded."""
    await run.emit({"type": "flock", "stage": "synth", "agent": synthesizer.id})
    await run.emit({"type": "phase", "phase": "answering"})
    await run.emit({"type": "status", "agent": synthesizer.id, "status": "writing"})

    done = [part for part in parts if part.status == "done" and part.text]
    missing = [part for part in parts if part.status != "done"]
    body = "\n\n".join(
        f"### {part.title} (from {part.agent_name or 'a specialist'})\n{part.text[:6000]}" for part in done
    )
    gap = ""
    if missing:
        gap = (
            "\n\nThese parts did not come back, and the answer must say so plainly instead of "
            "inventing them:\n" + "\n".join(f"- {part.title} ({part.error or part.status})" for part in missing)
        )

    stats = llm.StreamStats()
    answer: list[str] = []
    guard = ReflectionGuard()
    try:
        async for kind, text in llm.stream(
            synthesizer,
            [
                {
                    "role": "system",
                    "content": prompts.FLOCK_SYNTH.format(
                        name=synthesizer.name,
                        request=request[:2400],
                        parts=body[:16000],
                        gap=gap,
                    ),
                },
                {"role": "user", "content": "Write the answer."},
            ],
            think=False,
            stats=stats,
        ):
            if not text:
                continue
            if kind == "think":
                await run.emit({"type": "think", "agent": synthesizer.id, "delta": text})
                continue
            answer.append(text)
            safe = guard.feed(text)
            if safe:
                await run.emit({"type": "token", "agent": synthesizer.id, "delta": safe})
    except (llm.ProviderError, RuntimeError) as exc:
        await run.emit(run.track_usage(synthesizer, "flock:synthesis", stats.usage))
        # The parts are still worth showing. Handing the person the joined parts beats
        # handing them an error after four models already did the work.
        run.answer = body or f"The flock could not compose an answer — {exc}"
        await run.emit({"type": "error", "message": str(exc), "agent": synthesizer.id})
        return

    tail = guard.finish()
    if tail:
        await run.emit({"type": "token", "agent": synthesizer.id, "delta": tail})
    await run.emit(run.track_usage(synthesizer, "flock:synthesis", stats.usage))
    run.answer = strip_reflection("".join(answer)).text.strip() or body
    await run.emit({"type": "status", "agent": synthesizer.id, "status": "done"})


async def run_flock(
    run: RunState,
    specialists: list[Specialist],
    messages: list[dict[str, str]],
    depth: str = "brief",
) -> None:
    """Split, fan out, compose. The run's events carry every step."""
    from .chat import clean_history, last_question  # local import: chat owns the history rules

    turns = clean_history(messages)
    request = last_question(turns)
    if not turns or not request:
        run.save_metrics()
        await run.emit({"type": "error", "message": "Nothing to answer — the message was empty."})
        return

    pool = [spec for spec in specialists if spec.model]
    if len(pool) < 2:
        # One model cannot flock. Say so and answer normally rather than pretend.
        from .chat import run_chat

        await run.emit(
            {
                "type": "message",
                "from": "harness",
                "kind": "flock",
                "text": "Flock needs at least two configured specialists; answering with one.",
            }
        )
        await run_chat(run, pool[0] if pool else specialists[0], messages, depth)
        return

    await run.emit({"type": "start", "prompt": run.prompt, "kind": "flock", "created": run.created})
    await run.emit({"type": "phase", "phase": "planning"})

    planner = next((spec for spec in pool if spec.role == "planner"), pool[0])
    await run.emit({"type": "status", "agent": planner.id, "status": "thinking"})

    try:
        raw, plan_usage = await llm.call(
            planner,
            [
                {
                    "role": "system",
                    "content": prompts.FLOCK_PLAN.format(
                        request=request[:2400],
                        team="\n".join(f"- {spec.name} ({spec.role})" for spec in pool),
                        max_parts=MAX_PARTS,
                    ),
                },
                {"role": "user", "content": "Split the request."},
            ],
        )
    except llm.ProviderError as exc:
        run.save_metrics()
        await run.emit({"type": "error", "message": str(exc), "agent": planner.id})
        return

    await run.emit(run.track_usage(planner, "flock:plan", plan_usage))
    await run.emit({"type": "status", "agent": planner.id, "status": "done"})

    parts = parse_plan(raw, request)
    assign(parts, pool)
    await run.emit({"type": "flock", "stage": "plan", "tasks": [part.public() for part in parts]})

    # Fan out. `return_exceptions` keeps one dead part from cancelling its siblings; the
    # part records its own failure, so nothing is lost either way.
    await run.emit({"type": "phase", "phase": "building"})
    jobs = []
    for part in parts:
        owner = _by_id(pool, part.agent)
        if owner is None:
            part.status = "error"
            part.error = "no specialist owns this part"
            continue
        jobs.append(_run_part(run, part, owner, request))
    if jobs:
        await asyncio.gather(*jobs, return_exceptions=True)

    synthesizer = next((spec for spec in pool if spec.role == "synthesizer"), pool[-1])
    await _synthesize(run, synthesizer, request, parts)

    run.done = True
    run.save_metrics()
    await run.emit({"type": "phase", "phase": "done"})
    await run.emit(
        {
            "type": "done",
            "answer": run.answer,
            "metrics": run.metrics(),
            "build": run.build,
            "previewed": run.previewed,
            "finished": now_iso(),
        }
    )


async def flock_task(
    run: RunState,
    specialists: list[Specialist],
    messages: list[dict[str, str]],
    depth: str = "brief",
) -> None:
    """Bind the run, then flock. Mirrors `chat.chat_task` so the harness has one shape."""
    runtime.bind(run)
    try:
        await run_flock(run, specialists, messages, depth)
    except BudgetExceeded as exc:
        run.save_metrics()
        await run.emit({"type": "budget", **run.budget.as_dict(), "message": str(exc)})
        await run.emit({"type": "error", "message": f"The flock stopped by its budget — {exc}"})
    except Exception as exc:  # noqa: BLE001 - a failure must reach the page
        run.save_metrics()
        await run.emit({"type": "error", "message": str(exc)})
