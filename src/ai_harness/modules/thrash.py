"""Repeated identical work is a symptom, and the ledger can see it.

"Thrashed and retried the same approach" is one of the guide's named failure modes, and
nothing in the harness measured it: the budget caps spend, not repetition. A model that
calls the same search four times or rewrites the same file five times with no change burns
the run's time and the owner's money while looking busy.

Counters are per run, per *identical* call (tool name plus canonical arguments), so normal
retries with different input never trip it:

* at ``SOFT`` repeats the run gets a `thrash` event, so the transcript says what is
  happening instead of burying it in twenty identical lines;
* at ``HARD`` repeats the call is refused before it runs, and the refusal is recorded like
  any other failed tool call. The run keeps going — the point is to stop one loop, not to
  kill the work.
"""

from __future__ import annotations

import json

SOFT = 3
HARD = 8

_counts: dict[str, dict[str, int]] = {}
_warned: set[str] = set()


class Thrash(RuntimeError):
    """An identical call repeated past the hard cap."""

    def __init__(self, tool: str, count: int) -> None:
        self.tool = tool
        self.count = count
        super().__init__(f"{tool} was called identically {count} times; refusing another attempt")


def key(tool: str, args: dict | None) -> str:
    try:
        body = json.dumps(args or {}, sort_keys=True, default=str)
    except (TypeError, ValueError):
        body = str(args)
    return f"{tool}|{body[:400]}"


def observe(run_id: str, tool: str, args: dict | None) -> tuple[int, bool]:
    """`(count, first_warning)`: how many times this exact call has been seen."""
    seen = _counts.setdefault(run_id, {})
    stamp = key(tool, args)
    seen[stamp] = seen.get(stamp, 0) + 1
    first = seen[stamp] == SOFT and f"{run_id}|{stamp}" not in _warned
    if first:
        _warned.add(f"{run_id}|{stamp}")
    return seen[stamp], first


def triple(run_id: str, path: str) -> int:
    """How many times a path has been written in this run (for the receipt)."""
    return _counts.get(run_id, {}).get(f"write|{path}", 0)


def note_write(run_id: str, path: str) -> int:
    seen = _counts.setdefault(run_id, {})
    stamp = f"write|{path}"
    seen[stamp] = seen.get(stamp, 0) + 1
    return seen[stamp]


def reset(run_id: str) -> None:
    _counts.pop(run_id, None)
    _warned.difference_update({item for item in _warned if item.startswith(f"{run_id}|")})
