"""Researcher: plans queries, searches the open web, reads pages, reports facts."""

from __future__ import annotations

from ..models import Specialist
from ..modules import llm, prompts, tool_log, untrusted
from ..tools import web_search


async def queries(agent: Specialist, prompt: str, seeded: list[str]) -> tuple[list[str], llm.Usage]:
    if seeded:
        return seeded[:3], llm.Usage()
    text, usage = await llm.call(
        agent,
        [
            {"role": "system", "content": prompts.RESEARCH_QUERY.format(n=3, prompt=prompt)},
            {"role": "user", "content": "Queries:"},
        ],
    )
    lines = [line.strip(" -•\t") for line in text.splitlines() if line.strip()]
    return [line[:120] for line in lines][:3], usage


async def summarise(agent: Specialist, prompt: str, sources: list[dict], notes: list[str]) -> tuple[str, llm.Usage]:
    # Page text is written by strangers: every excerpt is fenced as untrusted data, and
    # lines that address the model (`ignore previous instructions`, a forged `system:` or
    # `<<<FILE` marker) never reach the prompt at all. See modules/untrusted.py.
    blob = untrusted.sources(sources)
    if not blob:
        blob = "\n\n".join(
            untrusted.fence(note, "earlier research note", kind="note") for note in notes[:5] if note.strip()
        )
    return await llm.call(
        agent,
        [
            {"role": "system", "content": prompts.RESEARCH_SUMMARY.format(prompt=prompt, sources=blob)},
            {"role": "user", "content": "Findings:"},
        ],
    )


async def gather(prompt: str, seeded: list[str], log=tool_log.null) -> tuple[list[dict], list[str]]:
    """Raw search without a model, so research never blocks on a slow provider."""
    query_list = seeded[:3] or [f"how to build {prompt[:80]}"]
    return await web_search.research(query_list, log=log)
