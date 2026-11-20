"""Research node: the team is allowed to look things up before it writes."""

from __future__ import annotations

from ..agents import researcher as researcher_agent
from ..core.state import RunState
from ..models import Specialist
from ..modules import tool_log


async def run(run: RunState, agent: Specialist, questions: list[str]) -> list[str]:
    await run.emit({"type": "phase", "phase": "research"})
    await run.emit({"type": "status", "agent": agent.id, "status": "researching"})
    await run.emit(
        {
            "type": "message",
            "from": agent.id,
            "text": "Checking the web before we write: " + "; ".join(questions[:3])
            if questions
            else "Searching for references.",
            "kind": "talk",
        }
    )

    seeded, query_usage = await researcher_agent.queries(agent, run.prompt, questions)
    # Every query and every URL fetched lands in the transcript as a `tool` event.
    sources, notes = await researcher_agent.gather(run.prompt, seeded, log=tool_log.recorder(run, agent.id, "research"))

    for source in sources[:6]:
        await run.emit(
            {
                "type": "research",
                "query": source.get("query", ""),
                "title": source.get("title", ""),
                "url": source.get("url", ""),
                "snippet": source.get("snippet", ""),
                "agent": agent.id,
            }
        )
    run.sources = sources

    summary, summary_usage = await researcher_agent.summarise(agent, run.prompt, sources, notes)
    usage = query_usage
    usage.add(summary_usage)
    await run.emit(run.track_usage(agent, "research", usage))

    if summary:
        await run.emit(
            {
                "type": "message",
                "from": agent.id,
                "to": "all",
                "text": summary[:500],
                "kind": "research",
            }
        )
    await run.emit({"type": "status", "agent": agent.id, "status": "done"})
    return ([summary] if summary else []) + notes
