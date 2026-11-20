"""A bounded, provider-agnostic tool loop for repair.

The rest of the harness is a pipeline: plan, write, verify, review. That is enough when
the first draft is close, and it is exactly where a small model fails — it rewrites a
whole file to change one selector, loses the parts that worked, and is charged again for
the privilege. This module adds the missing half of a real coding harness: the author can
*look* at the tree, make a targeted edit, run the deterministic checks, and see the
result before deciding what to do next.

Two design choices keep it usable with weak and local models:

* **Text protocol, not provider tools.** One JSON object per turn works on every
  OpenAI-compatible endpoint and on Ollama, including models that never implemented
  native tool calling. The parser tolerates fences, prose and a missing ``args`` wrapper,
  and a malformed turn costs one correction, not the run.
* **A flat state block, rebuilt from disk each turn.** The model does not carry a growing
  chat. It sees the objective, the open findings, an action ledger, the files it has
  read, and the last observation — pinned facts and an LRU working set instead of an
  ever-longer transcript. Context therefore stays flat while the loop runs, which is what
  lets a weak model stay on task for eight steps.

The finish gate is mechanical: ``done`` is refused while the last check has errors, and
a check is forced after any edit. A loop cannot report success on a failing tree, and the
caller still runs the real verifier afterwards.
"""

from __future__ import annotations

import json
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ..config.settings import (
    AGENT_ACTIVE_FILES,
    AGENT_CONTEXT_CHARS,
    AGENT_OBSERVATION_CHARS,
    AGENT_STEPS,
    MAX_FILE_BYTES,
)
from ..models import Specialist
from ..utils.validators import UnsafePath
from . import llm, progress, prompts, protected, thrash, tool_log

TOOLS = ("list", "read", "search", "edit", "write", "check", "done")

# The run's own bookkeeping is not the model's file. The verifier reads `todo.md`, the
# receipt is the audit trail, and `events.jsonl` is the transcript; a repair that can
# rewrite them can hide its own failure.
RESERVED = {progress.FILENAME, "RECEIPT.md", "events.jsonl"}

TOOL_ALIASES = {
    "read_file": "read",
    "readfile": "read",
    "cat": "read",
    "open": "read",
    "write_file": "write",
    "writefile": "write",
    "edit_file": "edit",
    "replace": "edit",
    "patch": "edit",
    "ls": "list",
    "tree": "list",
    "files": "list",
    "grep": "search",
    "find": "search",
    "verify": "check",
    "validate": "check",
    "test": "check",
    "finish": "done",
    "stop": "done",
    "complete": "done",
}

# Two malformed turns in a row means this model cannot speak the protocol. Stop and let
# the caller fall back to the whole-file rewrite instead of burning the step budget.
PARSE_FAILURES = 2
# Two checks with the same findings means the edits are not moving the tree. Stop early
# so the outer repair round can try a different strategy.
NO_PROGRESS = 2


class ToolError(RuntimeError):
    """A tool refused the call. The message is an observation, not a crash."""


# ---------- protocol ----------


def _objects(text: str):
    """Yield each balanced ``{...}`` substring, ignoring braces inside strings."""
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0 and start >= 0:
                yield text[start : index + 1]
                start = -1


def _first(args: dict, keys: tuple[str, ...], default: Any = "") -> Any:
    for key in keys:
        value = args.get(key)
        if value is not None:
            return value
    return default


def parse_step(text: str) -> tuple[dict | None, str]:
    """Extract one tool call from a model turn. Returns ``(call, error)``.

    Tolerant on purpose: fences and a sentence before the JSON are common, and a small
    model often flattens ``args`` into the top level. Both are accepted; a missing tool
    is not, because guessing which tool was meant would be the harness writing the code.
    """
    for raw in _objects(text or ""):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        tool = str(obj.get("tool") or obj.get("action") or "").strip().lower()
        if not tool and obj.get("done") is True:
            tool = "done"
        if not tool:
            continue
        tool = TOOL_ALIASES.get(tool, tool)
        args = obj.get("args")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        if not isinstance(args, dict):
            args = {key: value for key, value in obj.items() if key not in ("thought", "tool", "action", "done")}
        thought = str(obj.get("thought") or obj.get("reason") or "").strip()
        if tool not in TOOLS:
            return None, f"unknown tool {tool!r}; use one of {', '.join(TOOLS)}"
        return {"thought": thought, "tool": tool, "args": args}, ""
    return None, "no JSON object with a tool field"


# ---------- state ----------


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 60)].rstrip() + "\n…[truncated — read or search for the rest]"


def _line_count(text: str) -> int:
    return text.count("\n") + 1


def _format_check(report: dict | None) -> str:
    if not report:
        return "CHECK: not run yet."
    issues = list(report.get("issues") or [])
    head = (
        f"CHECK: {report.get('errors', 0)} error(s), {report.get('warnings', 0)} warning(s), "
        f"{report.get('checked', 0)} file(s)"
    )
    if not issues:
        return head + " — clean."
    lines = [head]
    for item in issues[:8]:
        detail = str(item.get("detail") or "")
        lines.append(
            f"- [{item.get('level', 'error')}] {item.get('file', '?')} · {item.get('code', '?')}: {detail[:220]}"
        )
    if len(issues) > 8:
        lines.append(f"… and {len(issues) - 8} more.")
    return "\n".join(lines)


@dataclass
class LoopState:
    """Everything the model is allowed to remember, and nothing else."""

    run: Any
    agent: Specialist
    path: str
    findings: str
    check: Callable[[], Awaitable[dict]]
    max_steps: int = AGENT_STEPS
    step: int = 0
    active: OrderedDict[str, str] = field(default_factory=OrderedDict)
    ledger: list[str] = field(default_factory=list)
    last_observation: str = ""
    last_check: dict | None = None
    last_signature: str = ""
    no_progress: int = 0
    parse_failures: int = 0
    changed: bool = False
    dirty: bool = True
    last_written: str = ""

    # ---------- prompt ----------

    def _tree(self) -> list[dict]:
        """The workspace listing, or the in-memory map for a focused test double."""
        try:
            return self.run.fm.tree()
        except (AttributeError, OSError):
            return [
                {
                    "path": path,
                    "lines": _line_count(str(meta.get("content") or "")),
                    "bytes": len(str(meta.get("content") or "")),
                }
                for path, meta in self.run.files.items()
            ]

    def _read(self, path: str) -> str:
        try:
            return self.run.fm.read(path)
        except AttributeError:
            return str((self.run.files.get(path) or {}).get("content") or "")
        except (UnsafePath, OSError) as exc:
            raise ToolError(str(exc)) from exc

    def _remember(self, path: str, content: str) -> None:
        self.active[path] = content
        self.active.move_to_end(path)
        while len(self.active) > AGENT_ACTIVE_FILES:
            self.active.popitem(last=False)

    def render(self) -> str:
        """Rebuild the state block. Pinned facts first; files are LRU-evicted."""
        segments: list[str] = []

        segments.append(progress.orient(self.run))
        segments.append(
            "A ticked todo line means the file was written; the CHECK block is the truth about whether it passes."
        )

        tree = self._tree()
        shown = ", ".join(str(item["path"]) for item in tree[:30])
        if len(tree) > 30:
            shown += f", … {len(tree) - 30} more"
        segments.append(f"WORKSPACE ({len(tree)} files): {shown or 'empty'}")

        ledger = self.ledger[-10:]
        if len(self.ledger) > 10:
            ledger = [f"… {len(self.ledger) - 10} earlier step(s)"] + ledger
        segments.append("ACTION LEDGER:\n" + ("\n".join(ledger) if ledger else "(nothing yet)"))

        segments.append(_format_check(self.last_check))

        # Active files are the working set. Newest first, so the file just edited wins.
        file_parts: list[str] = []
        for path, content in reversed(list(self.active.items())):
            file_parts.append(f"FILE {path} ({_line_count(content)} lines):\n{content}")
        if file_parts:
            segments.append("ACTIVE FILES:\n\n".join(file_parts))

        if self.last_observation:
            segments.append("LAST OBSERVATION:\n" + self.last_observation)

        text = "\n\n".join(segment for segment in segments if segment)
        if len(text) <= AGENT_CONTEXT_CHARS:
            return text

        # Over budget: drop the oldest active file first, then trim the observation and
        # finally the ledger. The objective and the open findings live in the system
        # prompt, outside this block, so they can never be evicted.
        while len(self.active) > 1:
            self.active.popitem(last=False)
            return self.render()
        self.last_observation = _clip(self.last_observation, max(600, AGENT_OBSERVATION_CHARS // 2))
        self.ledger = self.ledger[-6:]
        text = "\n\n".join(segment for segment in segments if segment)
        return _clip(text, AGENT_CONTEXT_CHARS)

    # ---------- tools ----------

    async def _run_check(self) -> str:
        try:
            report = await self.check()
        except Exception as exc:  # noqa: BLE001 - a checker failure is an observation
            return f"CHECK FAILED TO RUN: {type(exc).__name__}: {exc}"
        self.last_check = report if isinstance(report, dict) else {}
        self.dirty = False
        signature = json.dumps(self.last_check.get("issues") or [], sort_keys=True, default=str)
        if self.last_signature and signature == self.last_signature:
            self.no_progress += 1
        else:
            self.no_progress = 0
        self.last_signature = signature
        return _format_check(self.last_check)

    def _write(self, path: str, content: str) -> str:
        path = (path or self.path).strip()
        if not self.run.fm.can_write(path):
            raise ToolError(f"{path!r} is outside the run workspace")
        if path in RESERVED or path.startswith(".harness"):
            raise ToolError(f"{path} is harness-owned bookkeeping and cannot be edited by a run")
        existing = self._read(path)
        if protected.frozen(path, "rewrite", existing):
            raise ToolError(protected.describe(path))
        size = len(content.encode("utf-8"))
        if size > MAX_FILE_BYTES:
            raise ToolError(f"{path} would be {size} bytes; the limit is {MAX_FILE_BYTES}")
        self.run.fm.write(path, content)
        meta = self.run.files.get(path) or {"agent": self.agent.id}
        meta.update({"content": content, "done": True, "revised": True})
        self.run.files[path] = meta
        self._remember(path, content)
        self.changed = True
        self.dirty = True
        self.last_written = path
        return f"wrote {path} ({_line_count(content)} lines, {size} bytes)"

    def execute(self, tool: str, args: dict) -> str:
        """Run one tool synchronously (callers own the event log and the await)."""
        if tool == "list":
            tree = self._tree()
            if not tree:
                return "workspace is empty"
            lines = [f"{item['path']} ({item['lines']} lines, {item['bytes']} bytes)" for item in tree[:60]]
            if len(tree) > 60:
                lines.append(f"… {len(tree) - 60} more")
            return "\n".join(lines)

        if tool == "read":
            path = str(_first(args, ("path", "file", "target"), self.path)).strip()
            content = self._read(path)
            if not content:
                return f"{path} is empty"
            self._remember(path, content)
            # The text lives in ACTIVE FILES, not twice. The observation is the receipt.
            return f"read {path} ({_line_count(content)} lines); it is in ACTIVE FILES."

        if tool == "search":
            query = str(_first(args, ("query", "pattern", "text"), "")).strip()
            if not query:
                raise ToolError("search needs a non-empty query")
            scope = str(_first(args, ("path", "file"), ".")).strip() or "."
            hits: list[str] = []
            ignored = {progress.FILENAME, "RECEIPT.md"}
            for item in self._tree():
                path = str(item["path"])
                if path in ignored or path.startswith(".") or not path.startswith(scope.strip("./")):
                    continue
                if item["bytes"] > 256 * 1024:
                    continue
                try:
                    body = self._read(path)
                except ToolError:
                    continue
                for number, line in enumerate(body.splitlines(), start=1):
                    if query in line:
                        hits.append(f"{path}:{number}: {line.strip()[:180]}")
                        if len(hits) >= 25:
                            break
                if len(hits) >= 25:
                    break
            if not hits:
                return f"no literal match for {query!r} under {scope}"
            return "\n".join(hits)

        if tool == "edit":
            path = str(_first(args, ("path", "file", "target"), self.path)).strip()
            find = str(_first(args, ("find", "old", "old_string", "search"), ""))
            replace = str(_first(args, ("replace", "new", "new_string"), ""))
            if not find:
                raise ToolError("edit needs a non-empty 'find' string")
            if find == replace:
                raise ToolError("'find' and 'replace' are identical; nothing would change")
            existing = self._read(path)
            count = existing.count(find)
            if count == 0:
                raise ToolError(f"{find!r} does not appear in {path}; read it again and copy the exact text")
            replace_all = bool(args.get("all") or args.get("replace_all"))
            if count > 1 and not replace_all:
                raise ToolError(
                    f'{find!r} appears {count} times in {path}; include more surrounding text or set "all": true'
                )
            updated = existing.replace(find, replace, -1 if replace_all else 1)
            self._write(path, updated)
            return f"replaced {count if replace_all else 1} occurrence(s) in {path}; {_line_count(updated)} lines now"

        if tool == "write":
            path = str(_first(args, ("path", "file", "target"), self.path)).strip()
            content = _first(args, ("content", "text", "body"), None)
            if content is None:
                raise ToolError("write needs a 'content' string")
            return self._write(path, str(content))

        raise ToolError(f"unknown tool {tool!r}")

    async def finish(self) -> tuple[bool, str]:
        """The gate: `done` is only accepted against a clean, current check."""
        if self.last_check is None or self.dirty:
            observation = await self._run_check()
            if self.last_check is None:
                return False, f"DONE REFUSED: the checks could not run.\n{observation}"
        report = self.last_check or {}
        if report and not report.get("ok", True):
            errors = int(report.get("errors") or 0)
            return (
                False,
                f"DONE REFUSED: {errors} check error(s) still fail. Fix them, or call done with a summary "
                f"naming what remains.\n{_format_check(report)}",
            )
        return True, "done accepted — the last check is clean"


# ---------- the loop ----------


def _ledger_line(step: int, tool: str, result: str) -> str:
    first = " ".join(str(result).strip().splitlines()[:1])[:160]
    return f"{step}. {tool} → {first or '(no output)'}"


async def repair(
    run: Any,
    agent: Specialist,
    path: str,
    findings: str,
    check: Callable[[], Awaitable[dict]],
    max_steps: int = AGENT_STEPS,
) -> bool:
    """Run the bounded repair loop for one file. Returns whether anything changed.

    The caller owns the outer verification. This function never emits ``done`` for the
    run; it only decides whether the file was changed enough to be worth re-checking.
    """
    state = LoopState(run=run, agent=agent, path=path, findings=findings[:4000], check=check, max_steps=max_steps)
    await run.emit(
        {
            "type": "message",
            "from": agent.id,
            "to": "all",
            "kind": "work",
            "text": f"{agent.name} is repairing {path} with tools (up to {max_steps} steps).",
        }
    )

    try:
        for step in range(1, max_steps + 1):
            if state.no_progress >= NO_PROGRESS:
                await run.emit(
                    {
                        "type": "message",
                        "from": "verify",
                        "kind": "review",
                        "text": f"Repair loop on {path} stopped: two checks in a row found the same issues.",
                    }
                )
                break
            state.step = step
            system = prompts.AGENT_REPAIR.format(
                name=agent.name,
                role=agent.role,
                prompt=run.prompt,
                file=path,
                findings=state.findings,
                state=state.render(),
            )
            text, usage = await llm.call(
                agent,
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": "Next action. Reply with one JSON object."},
                ],
                on_think=lambda chunk: run.emit({"type": "think", "agent": agent.id, "delta": chunk}),
            )
            await run.emit(run.track_usage(agent, "repair-loop", usage))

            call, error = parse_step(text)
            if call is None:
                state.parse_failures += 1
                state.last_observation = (
                    f"INVALID STEP: {error}. Reply with exactly one JSON object, for example "
                    f'{{"thought":"...","tool":"read","args":{{"path":"{path}"}}}}'
                )
                state.ledger.append(f"{step}. invalid reply → correction")
                await run.emit(
                    {
                        "type": "message",
                        "from": agent.id,
                        "kind": "work",
                        "text": f"Repair loop: invalid step ({error}); one correction spent.",
                    }
                )
                if state.parse_failures >= PARSE_FAILURES:
                    break
                continue

            state.parse_failures = 0
            thought = str(call.get("thought") or "").strip()
            if thought:
                await run.emit({"type": "think", "agent": agent.id, "delta": thought})
            tool = str(call["tool"])
            args = dict(call.get("args") or {})

            if tool == "done":
                accepted, observation = await state.finish()
                state.last_observation = observation
                state.ledger.append(f"{step}. done → {'accepted' if accepted else 'refused'}")
                if accepted:
                    break
                continue

            if tool == "check":
                # Check is the one tool with real async work: it emits `verify` events.
                try:
                    observation = await state._run_check()  # noqa: SLF001 - the state owns the checker
                except Exception as exc:  # noqa: BLE001
                    observation = f"CHECK FAILED TO RUN: {type(exc).__name__}: {exc}"
                state.last_observation = observation
                state.ledger.append(_ledger_line(step, "check", observation))
                await run.emit(
                    {
                        "type": "tool",
                        "tool": "agent.check",
                        "args": {},
                        "result": _clip(observation, 600),
                        "ms": 0,
                        "ok": True,
                        "agent": agent.id,
                        "phase": "revising",
                    }
                )
                continue

            log_args = {key: value for key, value in args.items() if key != "content"}
            if "content" in args:
                log_args["content"] = f"<{len(str(args['content']))} chars>"
            observation = ""
            state.last_written = ""
            try:
                async with tool_log.call(run, f"agent.{tool}", log_args, agent=agent.id, phase="revising") as call_log:
                    try:
                        observation = state.execute(tool, args)
                    except Exception as exc:  # noqa: BLE001 - a refused tool is an observation
                        call_log.fail(str(exc))
                        observation = f"ERROR: {type(exc).__name__}: {exc}"
                    call_log.set_result(observation)
            except thrash.Thrash as exc:
                observation = f"ERROR: {exc}. Change the call instead of repeating it."
            if state.last_written:
                path = state.last_written
                content = str((run.files.get(path) or {}).get("content") or "")
                await run.emit({"type": "file_start", "agent": agent.id, "path": path, "mode": "rewrite"})
                await run.emit({"type": "file", "path": path, "delta": content})
                await run.emit(
                    {
                        "type": "file_end",
                        "path": path,
                        "agent": agent.id,
                        "lines": _line_count(content),
                        "repaired": True,
                    }
                )
            state.last_observation = observation
            state.ledger.append(_ledger_line(step, tool, observation))
    finally:
        await run.emit(
            {
                "type": "message",
                "from": agent.id,
                "to": "all",
                "kind": "work",
                "text": (
                    f"{agent.name} finished the repair loop on {path}: "
                    f"{'changed the file' if state.changed else 'no change'} after {state.step} step(s)."
                ),
            }
        )
    return state.changed
