"""Executor: thinks in the open, then writes one or more files.

A single answer may contain several files. Every ``<<<FILE path>>>`` line closes the
section before it and opens a new one, so an agent asked for ``src/App.tsx`` can also
land the component or stylesheet its own code imports. The split happens here, at the
boundary of the model output; the caller writes each section to its own path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..models import Specialist
from ..modules import llm

MARKER = re.compile(r"^\s*<<<FILE\s+(.+?)>>>\s*$")
FENCE = re.compile(r"^\s*```")


def clean_path(raw: str) -> str:
    """Normalise a marker path into a workspace-relative one."""
    name = re.sub(r"[^\w./-]", "", str(raw or "")).strip().strip("`'\"")
    while name.startswith("./"):
        name = name[2:]
    return name.strip("/")


@dataclass
class Section:
    path: str
    lines: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(self.lines).strip("\n")


class CodeStream:
    """Splits a streamed answer into visible reasoning and one section per file.

    Code that arrives before the first marker belongs to ``default_path`` — the file
    the agent was asked to write — so an answer that ignores the marker format still
    produces exactly one file instead of nothing.
    """

    def __init__(self, default_path: str = "") -> None:
        self.buffer = ""
        self.mode = "think"
        self.default_path = default_path
        self.thought: list[str] = []
        self.sections: list[Section] = []

    @property
    def current(self) -> Section | None:
        return self.sections[-1] if self.sections else None

    def _open(self, path: str) -> Section:
        section = Section(path or self.default_path)
        self.sections.append(section)
        return section

    def feed(self, chunk: str) -> list[tuple[str, str, str]]:
        """Yield (kind, path, text) for every complete line in this chunk."""
        self.buffer += chunk
        out: list[tuple[str, str, str]] = []
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            out.extend(self._line(line + "\n"))
        return out

    def flush(self) -> list[tuple[str, str, str]]:
        out = self._line(self.buffer) if self.buffer else []
        self.buffer = ""
        return out

    def _line(self, line: str) -> list[tuple[str, str, str]]:
        marker = MARKER.match(line.strip())
        if marker:
            path = clean_path(marker.group(1)) or self.default_path
            self.mode = "code"
            self._open(path)
            return [("marker", path, "")]
        if self.mode == "think":
            self.thought.append(line)
            return [("think", "", line)]
        if FENCE.match(line):
            return []
        section = self.current or self._open(self.default_path)
        section.lines.append(line)
        return [("code", section.path, line)]

    def rendered(self) -> list[tuple[str, str]]:
        """(path, content) per section, empty sections dropped, order preserved."""
        return [(section.path, section.text) for section in self.sections if section.text]


def fallback_code(thought: str) -> str:
    blocks = re.findall(r"```[a-zA-Z0-9]*\n(.*?)```", thought, flags=re.S)
    return max(blocks, key=len).strip("\n") if blocks else ""


async def reason(agent: Specialist, messages: list[dict[str, str]], on_think) -> tuple[str, llm.Usage]:
    """Pass one: pure deliberation, no code. This is the layer the human watches."""
    stats = llm.StreamStats()
    async for _kind, text in llm.stream(agent, messages, think=True, stats=stats):
        await on_think(text)
    # Providers with a dedicated thinking channel put nothing in `content`.
    deliberation = "".join(stats.content).strip() or "".join(stats.thinking).strip()
    return deliberation, stats.usage


async def compose(
    agent: Specialist,
    messages: list[dict[str, str]],
    on_think,
    on_code,
    on_marker,
    default_path: str = "",
) -> tuple[list[tuple[str, str]], llm.Usage]:
    """Pass two: stream the answer, routing reasoning away from code.

    ``on_code(path, delta)`` receives each code chunk with the file it belongs to, and
    ``on_marker(path)`` fires when a new section opens. Returns every completed
    section as (path, content).
    """
    router = CodeStream(default_path)
    stats = llm.StreamStats()

    async def route(chunks: list[tuple[str, str, str]]) -> None:
        for kind, path, text in chunks:
            if kind == "think":
                await on_think(text)
            elif kind == "marker":
                await on_marker(path)
            else:
                await on_code(path, text)

    async for kind, text in llm.stream(agent, messages, think=True, stats=stats):
        if kind == "think":
            await on_think(text)
        else:
            await route(router.feed(text))
    await route(router.flush())

    files = router.rendered()
    if not files:
        rescued = fallback_code("".join(router.thought))
        if rescued:
            path = (router.current.path if router.current else "") or default_path
            await on_marker(path)
            await on_code(path, rescued)
            files = [(path, rescued)]
    return files, stats.usage
