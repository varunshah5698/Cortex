"""Reviewer: reads every file, files findings, interrogates the author."""

from __future__ import annotations

from ..models import Specialist
from ..modules import llm, prompts


async def review(
    agent: Specialist, prompt: str, files: dict[str, dict], on_think, evidence: str = ""
) -> tuple[str, llm.Usage]:
    blob = "\n\n".join(f"--- FILE: {path} ---\n{meta.get('content', '')[:4000]}" for path, meta in files.items())
    system = prompts.REVIEW.format(prompt=prompt)
    if evidence:
        system = f"{system}\n\n{evidence}"
    stats = llm.StreamStats()
    async for _kind, text in llm.stream(
        agent,
        [
            {"role": "system", "content": system},
            {"role": "user", "content": blob},
        ],
        think=True,
        stats=stats,
    ):
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
