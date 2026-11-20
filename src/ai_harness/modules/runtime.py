"""Run-scoped context for the provider client.

``llm.py`` is called from every node and agent, and none of them should have to know
about budgets or event streams. Binding the current run into a context variable keeps
that layer thin: the client reads ``run.budget``, reports a cache hit or a retry to the
run, and no call signature grows a parameter.

The run *is* the context — the budget is read from it rather than bound alongside it,
so the two can never disagree. ``asyncio.create_task`` copies the current context, so
every concurrent specialist inside one run sees that run and no other.
"""

from __future__ import annotations

import time
from contextvars import ContextVar
from typing import Any

from ..config.settings import MAX_RUN_SECONDS
from .budget import Budget

RUN: ContextVar[Any | None] = ContextVar("harness_run", default=None)


class RunDeadlineExceeded(RuntimeError):
    """The run is out of wall-clock time and may not start another provider call."""


def bind(run: Any) -> None:
    RUN.set(run)


def current_run() -> Any | None:
    return RUN.get()


def current_budget() -> Budget | None:
    """The bound run's budget, or None outside a run (tests, plain calls)."""
    run = RUN.get()
    return getattr(run, "budget", None) if run is not None else None


def elapsed() -> float | None:
    """Seconds the bound run has been going, or None outside a run."""
    run = RUN.get()
    started = getattr(run, "started_at", None) if run is not None else None
    return None if started is None else max(0.0, time.monotonic() - started)


def check_deadline() -> None:
    """Refuse to start another call once the run has used its wall-clock time.

    Checked beside the budget, and for the same reason: the bound has to stop work
    rather than report it afterwards. `MAX_RUN_SECONDS` of 0 disables it, as the
    budget's limits do. A run is left with what it already wrote, and the orchestrator
    says so instead of hanging.
    """
    used = elapsed()
    if used is None or not MAX_RUN_SECONDS or used < MAX_RUN_SECONDS:
        return
    raise RunDeadlineExceeded(
        f"the run used {used:.0f}s of its {MAX_RUN_SECONDS:.0f}s wall-clock limit (HARNESS_MAX_SECONDS)"
    )


async def emit(event: dict) -> None:
    """Emit into the bound run, or do nothing outside a run."""
    run = RUN.get()
    if run is not None:
        await run.emit(event)
