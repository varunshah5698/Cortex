"""Hooks: extension points around every tool call, without touching the loop.

The motto is "hook around the loop, never rewrite the loop". The harness already has
exactly one choke point every tool call passes through — `modules/tool_log.py` — so a
hook system mounted there covers `agent.read`, `file_manager.write`, `web_search.search`
and every future tool at once, with no node changed and no second dispatch table.

Two deliberate limits, both because a hook file is configuration and configuration is
not code:

* **Declarative, not executable.** A rule matches a tool name and an optional path glob
  and answers `deny`, `allow` or `note`. It cannot run a shell command. A command hook
  would be model-reachable code execution outside `modules/sandbox.py`, which is the one
  thing `AGENTS.md` forbids; an operator who needs one adds a `permissions.py` rule and a
  sandboxed runner instead, and the blast radius gets a row in `docs/tool-inventory.md`.
* **A refusal is loud.** A denied call emits a `hook` event naming the rule and its
  reason, then raises. A tool that silently did nothing is the failure mode this exists
  to prevent.

Rules load from `harness.hooks.json` (override with `HARNESS_HOOKS_FILE`):

    {"PreToolUse": [
       {"match": "agent.write", "path": "tests/*", "action": "deny",
        "reason": "a run may not edit its own judge"}],
     "PostToolUse": [
       {"match": "agent.check", "action": "note", "text": "check recorded"}]}

`match` is an fnmatch against the tool name; `path` is an optional fnmatch against the
call's `path` argument. PreToolUse rules are consulted in order and the first match wins,
so a deny placed above an allow is a deny. No config and no registered rule means the
module is inert: an unconfigured harness behaves exactly as it did before this existed.
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass
from typing import Any

from ..config.settings import HOOKS_ENABLED, HOOKS_FILE

PRE = "PreToolUse"
POST = "PostToolUse"
EVENTS = (PRE, POST)

DENY = "deny"
ALLOW = "allow"
NOTE = "note"


class Denied(RuntimeError):
    """A PreToolUse rule refused the call. The message is the rule's reason."""

    def __init__(self, tool: str, reason: str, rule: Rule) -> None:
        self.tool = tool
        self.reason = reason
        self.rule = rule
        super().__init__(f"{tool} refused by hook: {reason}")


@dataclass(frozen=True)
class Rule:
    """One declarative hook. `path` is empty when the rule matches any path."""

    event: str
    match: str
    action: str
    reason: str = ""
    path: str = ""
    text: str = ""

    def applies(self, tool: str, args: dict[str, Any] | None) -> bool:
        if not fnmatch.fnmatch(tool, self.match):
            return False
        if not self.path:
            return True
        target = str((args or {}).get("path") or (args or {}).get("file") or "")
        return bool(target) and fnmatch.fnmatch(target, self.path)


# Registered rules, in order. The config file is read once and cached; `reset()` drops
# both, which is what a test needs and what a long-lived process needs after an edit.
_registered: dict[str, list[Rule]] = {PRE: [], POST: []}
_loaded: dict[str, list[Rule]] | None = None


def _from_config() -> dict[str, list[Rule]]:
    """Parse the hook file. A malformed file is inert, not fatal: a run must not die
    because an operator left a trailing comma in a config that only adds annotations."""
    parsed: dict[str, list[Rule]] = {PRE: [], POST: []}
    try:
        raw = json.loads(HOOKS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return parsed
    if not isinstance(raw, dict):
        return parsed
    for event in EVENTS:
        entries = raw.get(event)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            match = str(entry.get("match") or "").strip()
            action = str(entry.get("action") or "").strip().lower()
            if not match or action not in (DENY, ALLOW, NOTE):
                continue
            parsed[event].append(
                Rule(
                    event=event,
                    match=match,
                    action=action,
                    reason=str(entry.get("reason") or "").strip(),
                    path=str(entry.get("path") or "").strip(),
                    text=str(entry.get("text") or "").strip(),
                )
            )
    return parsed


def rules(event: str) -> list[Rule]:
    """Registered rules first, then the config file. Registration wins, so a test can
    install a rule without writing a file into the project root."""
    global _loaded
    if _loaded is None:
        _loaded = _from_config() if HOOKS_ENABLED else {PRE: [], POST: []}
    return [*_registered.get(event, []), *_loaded.get(event, [])]


def register(event: str, rule: Rule) -> None:
    """Add an in-process rule (tests, and a node that wants one for its own lifetime)."""
    if event not in EVENTS:
        raise ValueError(f"unknown hook event {event!r}; use one of {', '.join(EVENTS)}")
    _registered.setdefault(event, []).append(rule)


def reset() -> None:
    """Forget every registered and loaded rule. The next `rules()` re-reads the file."""
    global _loaded
    _registered[PRE] = []
    _registered[POST] = []
    _loaded = None


async def pre(run: Any, tool: str, args: dict[str, Any] | None) -> None:
    """Consult PreToolUse. Raises `Denied` when a rule refuses the call.

    The refusal event is emitted *before* the raise, because a caller that catches the
    exception would otherwise leave a transcript with no trace of why the tool stopped.
    """
    if not HOOKS_ENABLED:
        return
    for rule in rules(PRE):
        if not rule.applies(tool, args):
            continue
        if rule.action == DENY:
            await _emit(run, PRE, tool, DENY, rule)
            raise Denied(tool, rule.reason or "denied by a PreToolUse hook", rule)
        if rule.action == ALLOW:
            # An explicit allow stops the search: a later deny must not override a rule
            # the operator put first on purpose.
            await _emit(run, PRE, tool, ALLOW, rule)
            return
        await _emit(run, PRE, tool, NOTE, rule)


async def post(run: Any, tool: str, args: dict[str, Any] | None, ok: bool) -> None:
    """Consult PostToolUse. Observes only: it cannot change what the tool returned."""
    if not HOOKS_ENABLED:
        return
    for rule in rules(POST):
        if not rule.applies(tool, args):
            continue
        await _emit(run, POST, tool, rule.action, rule, ok=ok)


async def _emit(run: Any, event: str, tool: str, action: str, rule: Rule, ok: bool = True) -> None:
    if run is None:
        return
    emit = getattr(run, "emit", None)
    if emit is None:
        return
    payload: dict[str, Any] = {
        "type": "hook",
        "event": event,
        "tool": tool,
        "action": action,
        "match": rule.match,
        "ok": ok,
    }
    if rule.reason:
        payload["reason"] = rule.reason
    if rule.text:
        payload["text"] = rule.text
    try:
        await emit(payload)
    except Exception:  # noqa: BLE001 - a hook must never be able to fail a tool call
        pass


def table() -> list[dict]:
    """The active rules as data, for `/api/health` and the docs."""
    return [
        {
            "event": rule.event,
            "match": rule.match,
            "path": rule.path,
            "action": rule.action,
            "reason": rule.reason,
        }
        for event in EVENTS
        for rule in rules(event)
    ]
