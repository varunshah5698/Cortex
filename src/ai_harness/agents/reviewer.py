"""Reviewer: reads every file, files findings, interrogates the author."""

from __future__ import annotations

from ..config.settings import REVIEW_THINK_CHARS
from ..models import Specialist
from ..modules import llm, prompts


async def review(
    agent: Specialist, prompt: str, files: dict[str, dict], on_think, evidence: str = ""
) -> tuple[str, llm.Usage]:
    blob = "\n\n".join(f"--- FILE: {path} ---\n{meta.get('content', '')[:4000]}" for path, meta in files.items())
    system = prompts.REVIEW.format(prompt=prompt)
    if evidence:
        system = f"{system}\n\n{evidence}"
    # The reviewer's reasoning is bounded, for the same reason the author's is: a local
    # model can narrate its reading of the tree indefinitely. This stream was the one
    # unbounded consumer left in the pipeline — one review produced 7,000+ deltas and held
    # the call open for 46 minutes with no terminal event, which is what "stuck in a loop"
    # looked like from the outside. Breaking the generator closes the provider stream; the
    # findings already emitted are kept, and the verdict is whatever the model had said.
    stats = llm.StreamStats()
    seen = 0
    muted = False
    async for kind, text in llm.stream(
        agent,
        [
            {"role": "system", "content": system},
            {"role": "user", "content": blob},
        ],
        think=True,
        stats=stats,
    ):
        # The budget bounds what the *room* sees, not what the model is allowed to say.
        # Breaking the stream here would discard the findings, which arrive after the
        # reasoning — a model that over-thinks would lose its whole review and the run
        # would revise a tree the reviewer had actually approved. So the reasoning is
        # muted and the stream is still consumed; `llm.stream`'s own time bound is the
        # hard backstop for a stream that never ends at all.
        if kind == "think":
            seen += len(text)
            if not muted and seen > REVIEW_THINK_CHARS:
                muted = True
                await on_think("\n…[review reasoning budget reached — filing findings]\n")
                continue
            if not muted:
                await on_think(text)
            continue
        await on_think(text)
    return "".join(stats.content).strip(), stats.usage


async def question(agent: Specialist, author: str, path: str, finding: str) -> tuple[str, llm.Usage]:
    return await llm.call(
        agent,
        [
            {
                "role": "system",
                "content": prompts.REVIEW_QUESTION.format(
                    reviewer=agent.name, author=author, file=path, finding=finding
                ),
            },
            {"role": "user", "content": "Your question, one sentence."},
        ],
    )


async def reply(agent: Specialist, reviewer: str, path: str, finding: str, asked: str) -> tuple[str, llm.Usage]:
    return await llm.call(
        agent,
        [
            {
                "role": "system",
                "content": prompts.AUTHOR_REPLY.format(
                    author=agent.name, reviewer=reviewer, file=path, finding=finding
                ),
            },
            {"role": "user", "content": asked[:500]},
        ],
    )


async def plan_fix(agent: Specialist, path: str, findings: str, reply_text: str, on_think) -> tuple[str, llm.Usage]:
    stats = llm.StreamStats()
    async for _kind, text in llm.stream(
        agent,
        [
            {
                "role": "system",
                "content": prompts.REWRITE_REASON.format(
                    author=agent.name, file=path, findings=findings, reply=reply_text
                ),
            },
            {"role": "user", "content": "Deliberate, then stop."},
        ],
        think=True,
        stats=stats,
    ):
        await on_think(text)
    return "".join(stats.content).strip(), stats.usage
