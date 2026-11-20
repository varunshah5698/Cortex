"""Chat: the same run registry, event bus and usage ledger a build uses.

One specialist reasons, questions its own direction, then answers. The stages are
emitted as harness events, so the chat page watches the identical stream the build
room does — and a chat shows up in `/api/runs` and the metrics page like any run.
"""

from __future__ import annotations

import re

from ..config.settings import REASONING_WORDS
from ..core.state import RunState, now_iso
from ..models import Specialist
from ..modules import llm, prompts, runtime
from ..modules.budget import BudgetExceeded
from ..modules.guard import ReflectionGuard, strip_reflection

VERDICT = re.compile(r"VERDICT\s*:\s*(ON[\s-]*TRACK|ADJUST)", re.I)
DIRECTION = re.compile(r"DIRECTION\s*:\s*(.+)", re.I | re.S)


def clean_history(messages: list[dict[str, str]], limit: int = 20) -> list[dict[str, str]]:
    turns = [
        {"role": item.get("role", "user"), "content": str(item.get("content", ""))}
        for item in messages
        if item.get("role") in ("user", "assistant") and str(item.get("content", "")).strip()
    ]
    return turns[-limit:]


def read_check(raw: str) -> tuple[str, str]:
    """Split the self-check into (verdict, direction)."""
    verdict_match = VERDICT.search(raw)
    verdict = "adjust" if verdict_match and "ADJUST" in verdict_match.group(1).upper() else "on-track"
    direction_match = DIRECTION.search(raw)
    direction = (direction_match.group(1) if direction_match else raw).strip()
    return verdict, direction[:700]


def last_question(turns: list[dict[str, str]]) -> str:
    for turn in reversed(turns):
        if turn["role"] == "user":
            return turn["content"][:1200]
    return ""


# How much reasoning a message may spend before it answers. Each step is a model call:
# off answers directly, brief reasons then answers, deep also checks its own direction.
DEPTHS = ("off", "brief", "deep", "ultra")
DEFAULT_DEPTH = "brief"


async def _fail(run: RunState, specialist: Specialist, exc: BaseException) -> None:
    run.save_metrics()
    await run.emit({"type": "status", "agent": specialist.id, "status": "error"})
    await run.emit({"type": "error", "message": str(exc), "agent": specialist.id})


async def _answer(run: RunState, specialist: Specialist, turns: list[dict[str, str]], system: str) -> None:
    """The only pass every preset runs: stream the answer and finish the run."""
    await run.emit({"type": "phase", "phase": "answering"})
    await run.emit({"type": "status", "agent": specialist.id, "status": "writing"})
    answer: list[str] = []
    stats = llm.StreamStats()
    # prompt wording asks the model not to restate its reasoning; this enforces it
    guard = ReflectionGuard()
    try:
        async for kind, text in llm.stream(
            specialist,
            [{"role": "system", "content": system}, *turns],
            think=False,
            stats=stats,
        ):
            if not text:
                continue
            if kind == "think":
                await run.emit({"type": "think", "agent": specialist.id, "delta": text})
                continue
            answer.append(text)
            safe = guard.feed(text)
            if safe:
                await run.emit({"type": "token", "agent": specialist.id, "delta": safe})
    except llm.ProviderError as exc:
        await _fail(run, specialist, exc)
        return
    tail = guard.finish()
    if tail:
        await run.emit({"type": "token", "agent": specialist.id, "delta": tail})
    await run.emit(run.track_usage(specialist, "answer", stats.usage))

    raw = "".join(answer)
    run.answer = strip_reflection(raw).text
    if guard.stripped:
        await run.emit(
            {
                "type": "guard",
                "stage": "answer",
                "heading": guard.heading,
                "removed_chars": len(raw.strip()) - len(run.answer),
            }
        )
    if not run.answer:
        await _fail(run, specialist, RuntimeError(f"{specialist.name} returned an empty answer."))
        return

    run.done = True
    run.save_metrics()
    await run.emit({"type": "status", "agent": specialist.id, "status": "done"})
    # Same verdict shape as a build run: an answer is not compiled, so both fields are
    # present on every `done` a client has to interpret.
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


async def run_chat(
    run: RunState,
    specialist: Specialist,
    messages: list[dict[str, str]],
    depth: str = DEFAULT_DEPTH,
) -> None:
    """Drive one answer through the run's event stream, spending the chosen budget."""
    turns = clean_history(messages)
    question = last_question(turns)
    if not turns or not question:
        run.save_metrics()
        await run.emit({"type": "error", "message": "Nothing to answer — the message was empty."})
        return

    depth = depth if depth in DEPTHS else DEFAULT_DEPTH
    await run.emit({"type": "start", "prompt": run.prompt, "kind": "chat", "created": run.created})

    if depth == "off":
        # One call, no reasoning pass: the person asked for the answer, not the thinking.
        await _answer(
            run,
            specialist,
            turns,
            prompts.CHAT_DIRECT.format(name=specialist.name, role=specialist.role),
        )
        return

    await run.emit({"type": "phase", "phase": "thinking"})
    await run.emit({"type": "status", "agent": specialist.id, "status": "thinking"})

    # Pass 1 — the specialist thinks out loud. Watched by the human, token by token.
    thoughts: list[str] = []
    reasoning_stats = llm.StreamStats()
    try:
        async for _kind, text in llm.stream(
            specialist,
            [
                {
                    "role": "system",
                    "content": prompts.CHAT_REASON.format(
                        name=specialist.name,
                        role=specialist.role,
                        words=(
                            REASONING_WORDS * 2
                            if depth == "ultra"
                            else REASONING_WORDS
                            if depth == "deep"
                            else max(140, REASONING_WORDS // 2)
                        ),
                    ),
                },
                *turns,
            ],
            think=True,
            stats=reasoning_stats,
        ):
            if text:
                thoughts.append(text)
                await run.emit({"type": "think", "agent": specialist.id, "delta": text})
    except llm.ProviderError as exc:
        await _fail(run, specialist, exc)
        return
    await run.emit(run.track_usage(specialist, "reasoning", reasoning_stats.usage))

    reasoning = "".join(thoughts).strip()
    if not reasoning:
        reasoning = f"No separate reasoning channel from {specialist.name}; answering directly."

    if depth == "brief":
        await _answer(
            run,
            specialist,
            turns,
            prompts.CHAT_ANSWER_BRIEF.format(name=specialist.name, role=specialist.role, reasoning=reasoning[-1500:]),
        )
        return

    # Pass 2 — question the direction before committing to an answer.
    await run.emit({"type": "phase", "phase": "check"})
    try:
        raw, check_usage = await llm.call(
            specialist,
            [
                {
                    "role": "system",
                    "content": prompts.CHAT_CHECK.format(
                        name=specialist.name,
                        request=question,
                        reasoning=reasoning[-4000:],
                    ),
                },
                {"role": "user", "content": "Give the verdict and the direction."},
            ],
        )
    except llm.ProviderError as exc:
        await _fail(run, specialist, exc)
        return
    await run.emit(run.track_usage(specialist, "self-check", check_usage))

    verdict, direction = read_check(raw)
    # the card labels the verdict itself, so the body carries only the direction
    check_text = direction or raw.strip()[:500]
    await run.emit({"type": "check", "verdict": verdict, "text": check_text})

    if depth == "ultra":
        # Pass 3 — a second, adversarial reviewer. The first check is context, not a
        # ceiling: this one is asked to find the failure the first one missed.
        await run.emit({"type": "phase", "phase": "check"})
        try:
            red, red_usage = await llm.call(
                specialist,
                [
                    {
                        "role": "system",
                        "content": prompts.CHAT_RED_TEAM.format(
                            request=question,
                            reasoning=reasoning[-6000:],
                            check=raw[-2000:],
                        ),
                    },
                    {"role": "user", "content": "Attack the direction. Give the verdict."},
                ],
            )
        except llm.ProviderError as exc:
            await _fail(run, specialist, exc)
            return
        await run.emit(run.track_usage(specialist, "red-team", red_usage))
        red_verdict, red_direction = read_check(red)
        red_text = red_direction or red.strip()[:500]
        await run.emit(
            {
                "type": "check",
                "verdict": red_verdict,
                "text": f"Second reviewer: {red_text}",
            }
        )
        check_text = f"{check_text}\n\nSecond reviewer ({red_verdict}): {red_text}"

    # Pass 4 — the answer, following the direction the checks settled on.
    await _answer(
        run,
        specialist,
        turns,
        prompts.CHAT_ANSWER.format(
            name=specialist.name,
            role=specialist.role,
            reasoning=reasoning[-1500:],
            check=check_text,
            deep="\nGo deeper: show the reasoning behind the answer, name your assumptions and any uncertainty.",
        ),
    )


async def chat_task(
    run: RunState,
    specialist: Specialist,
    messages: list[dict[str, str]],
    depth: str = DEFAULT_DEPTH,
) -> None:
    """Bind this chat's run to its calls, mirroring the build orchestrator."""
    runtime.bind(run)
    try:
        await run_chat(run, specialist, messages, depth)
    except BudgetExceeded as exc:
        run.save_metrics()
        await run.emit({"type": "budget", **run.budget.as_dict(), "message": str(exc)})
        await run.emit({"type": "error", "message": f"Chat stopped by its budget — {exc}"})
    except Exception as exc:  # noqa: BLE001 - a failure must reach the chat page
        await _fail(run, specialist, exc)
