"""The trajectory: one run, one linear file, replayable without the harness.

mini-swe-agent's second-most portable idea is that a run's history should be a *document*
rather than a side effect of the process that produced it. Its `serialize()` writes one
JSON object per run — the messages, the config of every component, the model stats, the
exit status and the submission — and `trajectory_format` names the shape so a reader can
tell which version it is holding. That is what makes a run debuggable after the process
is gone, and what lets a benchmark score a run it did not supervise.

Cortex already has the raw material and keeps it in two places: `events.jsonl` is the
append-only transcript and `.harness-metrics.json` is the ledger. Both are internal
formats, both are spread across a directory, and neither carries the exit status. So a
finished run cannot be handed to anyone — a reviewer, an eval, a bug report — without
also handing them the harness.

This module writes the third file: `trajectory.json`, one self-contained object per run,
in a named format. It is derived, never authoritative — `events.jsonl` stays the record,
and this is a projection of it. Deleting it loses nothing; writing it wrong must never
fail a run, which is why every write here is best-effort.

The format is `cortex-trajectory-1`. It is versioned because the alternative is a reader
guessing, and a run file that has to be guessed at is not evidence.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FORMAT = "cortex-trajectory-1"
FILENAME = "trajectory.json"

# A trajectory is for reading and for replaying decisions, not for archiving file bodies:
# the workspace already holds those, and a run with four megabytes of generated code
# would otherwise produce a trajectory nobody can open.
MAX_EVENT_TEXT = 2000
MAX_EVENTS = 4000


def _clip(value: Any, limit: int = MAX_EVENT_TEXT) -> Any:
    if isinstance(value, str):
        return value if len(value) <= limit else value[: limit - 40] + "…[truncated in trajectory]"
    if isinstance(value, dict):
        return {str(key): _clip(item, limit // 2) for key, item in list(value.items())[:24]}
    if isinstance(value, (list, tuple)):
        return [_clip(item, limit // 2) for item in list(value)[:40]]
    return value


def _events(run) -> list[dict]:
    """The run's events, from memory when live and from disk when restored."""
    live = getattr(run, "events", None)
    if isinstance(live, list) and live:
        return list(live)
    try:
        from ..core.state import read_events

        return read_events(str(getattr(run, "run_id", "")))
    except Exception:  # noqa: BLE001 - a missing transcript is an empty one, not a crash
        return []


def _messages(events: list[dict]) -> list[dict]:
    """The conversation, reconstructed from the event stream.

    The harness emits `message` for what a specialist said and `think` for what it was
    thinking; both are turns a reader wants, in the order they happened. Anything else is
    bookkeeping and belongs in `events`, not in the conversation.
    """
    turns: list[dict] = []
    thinking: dict[str, list[str]] = {}
    for event in events:
        kind = event.get("type")
        if kind == "think":
            thinking.setdefault(str(event.get("agent") or ""), []).append(str(event.get("delta") or ""))
            continue
        if kind != "message":
            continue
        agent = str(event.get("agent") or event.get("from") or "")
        thought = "".join(thinking.pop(agent, [])).strip()
        turn: dict[str, Any] = {
            "role": "assistant",
            "agent": agent,
            "content": _clip(str(event.get("text") or "")),
        }
        if thought:
            turn["thought"] = _clip(thought)
        if event.get("kind"):
            turn["kind"] = event["kind"]
        turns.append(turn)
    return turns


def build(run, status: str = "", submission: str = "", report: dict | None = None) -> dict:
    """The trajectory object for one run. Pure: reads the run, changes nothing."""
    events = _events(run)
    metrics = {}
    try:
        metrics = run.metrics()
    except Exception:  # noqa: BLE001 - a trajectory must not fail on a partial run
        metrics = {}
    files = {}
    try:
        files = {
            path: {
                "agent": meta.get("agent"),
                "lines": str(meta.get("content") or "").count("\n") + 1,
                "bytes": len(str(meta.get("content") or "").encode("utf-8")),
                "done": bool(meta.get("done")),
                "revised": bool(meta.get("revised")),
            }
            for path, meta in (getattr(run, "files", {}) or {}).items()
        }
    except Exception:  # noqa: BLE001
        files = {}
    return {
        "trajectory_format": FORMAT,
        "info": {
            "run_id": str(getattr(run, "run_id", "")),
            "prompt": str(getattr(run, "prompt", "")),
            "kind": str(getattr(run, "kind", "")),
            "depth": str(getattr(run, "depth", "")),
            "created": str(getattr(run, "created", "")),
            "exit_status": status,
            "submission": _clip(submission),
            "done": bool(getattr(run, "done", False)),
            "model_stats": metrics.get("totals") or {},
            "budget": metrics.get("budget") or {},
            "verification": _clip(metrics.get("verification")),
            "goal": _clip(metrics.get("goal")),
            "config": {
                "agents": metrics.get("agents") or [],
                "prompts": metrics.get("prompts") or {},
            },
            "files": files,
            "event_count": len(events),
            "events_truncated": max(0, len(events) - MAX_EVENTS),
        },
        "messages": _messages(events),
        "events": [_clip(event) for event in events[-MAX_EVENTS:]],
        "report": _clip(report) if report else None,
    }


def write(run, status: str = "", submission: str = "", report: dict | None = None) -> Path | None:
    """Write `trajectory.json` into the run's workspace. Returns its path, or None.

    Best-effort by design: this is a derived artifact, and a full disk or a read-only
    workspace is not a reason for a finished run to report a failure it did not have.
    """
    workspace = getattr(run, "workspace", None)
    if workspace is None:
        return None
    path = Path(workspace) / FILENAME
    try:
        data = build(run, status=status, submission=submission, report=report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        return path
    except (OSError, TypeError, ValueError):
        return None


def read(run_id: str, root: Path | None = None) -> dict | None:
    """Load a written trajectory, or None when the run never produced one."""
    if root is None:
        from ..config.settings import RUNS_ROOT

        root = Path(RUNS_ROOT)
    path = Path(root) / run_id / FILENAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def summary(data: dict | None) -> dict:
    """The few fields a list view needs, from a loaded trajectory."""
    if not data:
        return {}
    info = data.get("info") or {}
    return {
        "run_id": info.get("run_id", ""),
        "prompt": info.get("prompt", ""),
        "exit_status": info.get("exit_status", ""),
        "done": bool(info.get("done")),
        "files": len(info.get("files") or {}),
        "messages": len(data.get("messages") or []),
        "format": data.get("trajectory_format", ""),
        "total_tokens": (info.get("model_stats") or {}).get("total_tokens", 0),
        "cost_usd": (info.get("model_stats") or {}).get("cost_usd", 0.0),
    }
