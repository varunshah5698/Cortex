"""Per-tool-call logging: what was asked, what came back, how long it took.

The transcript already records what the team thought (`think`), what it wrote (`file`) and
what it spent (`usage`), but not what it *asked a tool to do*. Reconstructing a failed run
without that is guesswork — "the search returned nothing" and "the search was never
called" look identical in a log that only has the outcome.

So every tool call runs inside a :class:`ToolCall`, an async context manager that measures
the call and emits one `tool` event into the run's `events.jsonl`:

    async with tool_log.call(run, "web_search.search", {"query": q}, agent=agent.id) as call:
        hits = await search(q)
        call.set_result(f"{len(hits)} result(s)")

Tools stay run-agnostic: they accept a `log` factory, defaulting to :func:`null`, which
emits nothing. That is what lets a unit test call `web_search.search("x")` without a run.
"""

from __future__ import annotations

import time
from typing import Any

from . import thrash

# A log line is for reconstruction, not for replay: enough to identify the call and its
# shape, never the whole page or file it touched.
RESULT_CHARS = 600
ARGS_CHARS = 400


def _clip(value: Any, limit: int) -> Any:
    """Shorten a value so one tool event cannot dominate the transcript."""
    if isinstance(value, str):
        return value if len(value) <= limit else value[: limit - 1] + "…"
    if isinstance(value, dict):
        return {str(key): _clip(item, limit // 2) for key, item in list(value.items())[:12]}
    if isinstance(value, (list, tuple)):
        return [_clip(item, limit // 2) for item in list(value)[:8]]
    return value


class ToolCall:
    """One call: its arguments, its result, and the wall-clock time it took."""

    def __init__(
        self,
        run: Any,
        tool: str,
        args: dict[str, Any] | None = None,
        agent: str = "",
        phase: str = "",
    ) -> None:
        self.run = run
        self.tool = tool
        self.args = args or {}
        self.agent = agent
        self.phase = phase
        self.result: Any = ""
        self.ok = True
        self.error = ""
        self._started = 0.0

    @property
    def elapsed_ms(self) -> int:
        return int((time.perf_counter() - self._started) * 1000)

    def set_result(self, value: Any) -> None:
        """Record what the tool returned (or a short summary of it)."""
        self.result = value

    def fail(self, message: str) -> None:
        """Mark the call as failed even if the tool swallowed its own exception."""
        self.ok = False
        self.error = message

    async def __aenter__(self) -> ToolCall:
        self._started = time.perf_counter()
        run_id = str(getattr(self.run, "run_id", "") or "")
        if self.run is not None and run_id:
            count, first = thrash.observe(run_id, self.tool, self.args)
            if first:
                await self.run.emit(
                    {
                        "type": "thrash",
                        "tool": self.tool,
                        "count": count,
                        "message": f"{self.tool} has now been called {count}× with identical arguments",
                    }
                )
            if count >= thrash.HARD:
                # Refuse before the work happens, and record the refusal like any other
                # failed call: the run keeps its history even though this attempt stops.
                await self.run.emit(
                    {
                        "type": "tool",
                        "tool": self.tool,
                        "args": _clip(self.args, ARGS_CHARS),
                        "result": "refused: identical call repeated",
                        "ms": 0,
                        "ok": False,
                        "error": f"called identically {count} times",
                        **({"agent": self.agent} if self.agent else {}),
                    }
                )
                raise thrash.Thrash(self.tool, count)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if exc is not None:
            self.ok = False
            self.error = f"{type(exc).__name__}: {exc}"
        if self.run is not None:
            event: dict[str, Any] = {
                "type": "tool",
                "tool": self.tool,
                "args": _clip(self.args, ARGS_CHARS),
                "result": _clip(self.result, RESULT_CHARS),
                "ms": self.elapsed_ms,
                "ok": self.ok,
            }
            if self.agent:
                event["agent"] = self.agent
            if self.phase:
                event["phase"] = self.phase
            if self.error:
                event["error"] = _clip(self.error, RESULT_CHARS)
            await self.run.emit(event)
        return False  # never swallow the tool's exception


def call(
    run: Any,
    tool: str,
    args: dict[str, Any] | None = None,
    agent: str = "",
    phase: str = "",
) -> ToolCall:
    """One tool call to be logged into `run`'s event stream."""
    return ToolCall(run, tool, args, agent=agent, phase=phase)


def recorder(run: Any, agent: str = "", phase: str = ""):
    """A `log(tool, args)` factory bound to one run, for tools that take a logger."""

    def log(tool: str, args: dict[str, Any] | None = None) -> ToolCall:
        return ToolCall(run, tool, args, agent=agent, phase=phase)

    return log


def null(tool: str, args: dict[str, Any] | None = None) -> ToolCall:
    """The default for a tool with no run: measures nothing, emits nothing."""
    return ToolCall(None, tool, args)
