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
    MAX_RUN_BYTES,
    NATIVE_TOOLS,
)
from ..models import Specialist
from ..utils.validators import UnsafePath
from . import code_search, hooks, llm, progress, prompts, protected, shell, skills, thrash, tool_log

TOOLS = ("list", "read", "search", "grep", "shell", "edit", "write", "skill", "check", "delegate", "done")

# The run's own bookkeeping is not the model's file. The verifier reads `todo.md`, the
# receipt is the audit trail, and `events.jsonl` is the transcript; a repair that can
# rewrite them can hide its own failure.
RESERVED = {progress.FILENAME, "RECEIPT.md", "events.jsonl"}

# Tool arguments arrive as JSON from a model, so a boolean is often the *string* "false".
# `bool("false")` is True, which turned an explicit "do not replace everything" into a
# destructive replace-all. Parse the words a model actually writes.
TRUE_WORDS = {"true", "1", "yes", "y", "on"}


def _flag(value: Any) -> bool:
    """A permissive boolean for model-supplied arguments; anything unreadable is False."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in TRUE_WORDS
    return False


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
    "find": "search",
    "code_search": "grep",
    "codesearch": "grep",
    "rg": "grep",
    "regex": "grep",
    "bash": "shell",
    "sh": "shell",
    "exec": "shell",
    "run": "shell",
    "terminal": "shell",
    "verify": "check",
    "validate": "check",
    "test": "check",
    "finish": "done",
    "stop": "done",
    "complete": "done",
    "subagent": "delegate",
    "ask": "delegate",
    "investigate": "delegate",
}

# The same seven capabilities, described once for providers that speak native tools.
# The text protocol above remains the fallback and the only path for the other providers.
TOOL_SPECS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "list",
            "description": "List every file in the run workspace with line and byte counts.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read",
            "description": "Return the exact current text of one file.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": "Find a literal string across the run's files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "path": {"type": "string", "description": "Optional path prefix"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": (
                "Search the run's files with a regular expression. Use this to find a "
                "definition, a selector or a symbol; `search` is the literal-substring version."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "A regular expression"},
                    "path": {"type": "string", "description": "Optional path prefix"},
                    "extension": {"type": "string", "description": "Optional file extension, e.g. 'py'"},
                    "case_sensitive": {"type": "boolean", "description": "Default false"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "shell",
            "description": (
                "Run one allow-listed command in the run workspace. Refused unless the "
                "command is allow-listed and a sandbox is available; one command, no pipes."
            ),
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit",
            "description": "Replace one exact, unique occurrence in a file. Preferred over write.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "find": {"type": "string"},
                    "replace": {"type": "string"},
                    "all": {"type": "boolean", "description": "Replace every occurrence"},
                },
                "required": ["path", "find", "replace"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write",
            "description": "Replace a whole file. Use only when a whole file is the smallest fix.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check",
            "description": "Run the harness's deterministic checks now.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delegate",
            "description": (
                "Hand one question to a subagent with its own context. It reads the tree and "
                "returns a short answer; you never see its steps. Use it when answering would "
                "mean reading several files you do not otherwise need."
            ),
            "parameters": {
                "type": "object",
                "properties": {"task": {"type": "string", "description": "The one question to answer"}},
                "required": ["task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill",
            "description": (
                "Open a reusable procedure this run ships. Only the names are advertised; "
                "the body is read when you ask for it. Invoke one when the situation it "
                "names is the situation you are in."
            ),
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "The skill's name"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "done",
            "description": "Finish. Refused while the current check has errors.",
            "parameters": {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
            },
        },
    },
]

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
    native: bool = NATIVE_TOOLS

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
        text = self._render_block()
        if len(text) <= AGENT_CONTEXT_CHARS:
            return text

        # Over budget: drop the oldest active file first, then trim the observation and
        # finally the ledger. The objective and the open findings live in the system
        # prompt, outside this block, so they can never be evicted. Each pass rebuilds the
        # block from the (now smaller) working set — recursing into render() spent one
        # stack frame per eviction and could not terminate on a single oversized file.
        while len(self.active) > 1:
            self.active.popitem(last=False)
            text = self._render_block()
            if len(text) <= AGENT_CONTEXT_CHARS:
                return text
        self.last_observation = _clip(self.last_observation, max(600, AGENT_OBSERVATION_CHARS // 2))
        self.ledger = self.ledger[-6:]
        return _clip(self._render_block(), AGENT_CONTEXT_CHARS)

    def _render_block(self) -> str:
        """One pass over the current state. No eviction, no truncation."""
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

        return "\n\n".join(segment for segment in segments if segment)

    # ---------- tools ----------

    async def _run_check(self) -> str:
        try:
            report = await self.check()
        except Exception as exc:  # noqa: BLE001 - a checker failure is an observation
            return f"CHECK FAILED TO RUN: {type(exc).__name__}: {exc}"
        # A checker that answers something other than a report has not answered. Coercing
        # it to `{}` made `finish` read "no issues, no errors" and accept `done` on work
        # nothing had checked; leaving it unset makes the gate refuse instead.
        self.last_check = report if isinstance(report, dict) else None
        self.dirty = False
        signature = json.dumps((self.last_check or {}).get("issues") or [], sort_keys=True, default=str)
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
        # The per-file cap does not bound the run: eight files at the cap are four times
        # MAX_RUN_BYTES. Enforce the run quota here, at the tool boundary, before writing.
        others = sum(
            len(str(meta.get("content") or "").encode("utf-8"))
            for other, meta in self.run.files.items()
            if other != path
        )
        if others + size > MAX_RUN_BYTES:
            raise ToolError(f"{path} would put the run at {others + size} bytes; the run limit is {MAX_RUN_BYTES}")
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

        if tool == "grep":
            pattern = str(_first(args, ("pattern", "query", "regex", "text"), "")).strip()
            if not pattern:
                raise ToolError("grep needs a non-empty 'pattern'")
            scope = str(_first(args, ("path", "file"), ".")).strip() or "."
            extension = str(_first(args, ("extension", "file_type", "type"), "")).strip()
            case_sensitive = _flag(args.get("case_sensitive")) or _flag(args.get("case"))
            ignored = {progress.FILENAME, "RECEIPT.md"}
            candidates = [
                item
                for item in self._tree()
                if str(item.get("path") or "") not in ignored and not str(item.get("path") or "").startswith(".")
            ]
            try:
                hits = code_search.search(
                    candidates,
                    self._read,
                    pattern,
                    scope=scope,
                    extension=extension,
                    case_sensitive=case_sensitive,
                )
            except code_search.SearchError as exc:
                raise ToolError(str(exc)) from exc
            return code_search.render(hits, pattern, scope)

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
            replace_all = _flag(args.get("all")) or _flag(args.get("replace_all"))
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
        # `ok` must be present and true. A report with no `ok` key (or `{"errors": 3}`)
        # used to default to clean, which is the harness vouching for a check it never saw.
        if report.get("ok") is not True:
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
    state.native = NATIVE_TOOLS
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
                skills=skills.catalog(run.files),
            )
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": "Next action. Call one tool."},
            ]
            call: dict | None = None
            error = ""
            if state.native and llm.supports_native_tools(agent):
                try:
                    call, text, usage = await llm.call_tools(agent, messages, TOOL_SPECS)
                except llm.NativeToolsUnsupported:
                    # The provider rejects `tools`; stop paying for a rejected shape and
                    # use the text protocol for the rest of this loop.
                    state.native = False
                    continue
                await run.emit(run.track_usage(agent, "repair-loop", usage))
                if call is not None and str(call.get("tool") or "") not in TOOLS:
                    call, error = None, f"unknown tool {call.get('tool')!r}"
                if call is None and not error:
                    call, error = parse_step(text)
            else:
                text, usage = await llm.call(
                    agent,
                    messages,
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

            if tool == "delegate":
                # A subagent gets its own messages[] and returns one text answer. The
                # parent's context gains the answer and nothing else, which is the whole
                # reason to delegate: ten tool calls in a child cost one line here.
                task = str(_first(args, ("task", "question", "query"), "")).strip()
                if not task:
                    state.last_observation = "ERROR: delegate needs a non-empty 'task'"
                    state.ledger.append(f"{step}. delegate → refused: no task")
                    continue
                from . import subagent  # local: subagent imports this module for parse_step

                report = await subagent.run_subagent(run, agent, task)
                observation = subagent.as_context(report) or (
                    "The subagent returned no answer. Answer the question yourself with read/search."
                )
                state.last_observation = observation
                state.ledger.append(_ledger_line(step, "delegate", observation))
                await run.emit(
                    {
                        "type": "tool",
                        "tool": "agent.delegate",
                        "args": {"task": task[:200]},
                        "result": _clip(observation, 600),
                        "ms": 0,
                        "ok": report.complete,
                        "agent": agent.id,
                        "phase": "revising",
                    }
                )
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

            if tool == "skill":
                # A skill is a procedure read on demand. It arrives as an observation, not
                # as prompt text, so a run that never invokes it never pays for it. The
                # body goes into ACTIVE FILES so a long procedure is not re-sent each turn.
                wanted = str(_first(args, ("name", "skill", "path"), "")).strip()
                try:
                    opened = skills.open_skill(run.files, wanted)
                except LookupError as exc:
                    observation = f"ERROR: {exc}"
                else:
                    state._remember(f"{skills.SKILL_DIR}/{wanted}/{skills.SKILL_FILE}", opened)
                    observation = (
                        f"opened the `{wanted}` skill ({_line_count(opened)} lines); "
                        f"its procedure is in ACTIVE FILES. Follow it."
                    )
                state.last_observation = observation
                state.ledger.append(_ledger_line(step, "skill", observation))
                await run.emit(
                    {
                        "type": "tool",
                        "tool": "agent.skill",
                        "args": {"name": wanted[:80]},
                        "result": _clip(observation, 600),
                        "ms": 0,
                        "ok": not observation.startswith("ERROR"),
                        "agent": agent.id,
                        "phase": "revising",
                    }
                )
                continue

            if tool == "shell":
                # Shell is async like check: it spawns a contained process and emits a
                # tool event. It is refused rather than approved when policy says `ask`,
                # because a repair loop has nobody at the keyboard to approve it.
                command = str(_first(args, ("command", "cmd", "script"), "")).strip()
                observation = ""
                if not command:
                    observation = "ERROR: shell needs a non-empty 'command'"
                else:
                    try:
                        async with tool_log.call(
                            run, "agent.shell", {"command": command[:200]}, agent=agent.id, phase="revising"
                        ) as call_log:
                            try:
                                observation = await shell.run(command, run.workspace, run.run_id)
                            except Exception as exc:  # noqa: BLE001 - a refused command is an observation
                                call_log.fail(str(exc))
                                observation = f"ERROR: {type(exc).__name__}: {exc}"
                            call_log.set_result(observation)
                    except hooks.Denied as exc:
                        # A hook refusal is an observation like any other refusal: the model
                        # can read the rule's reason and try a different command.
                        observation = f"ERROR: {exc}. Pick a different command, or finish without it."
                    except thrash.Thrash as exc:
                        observation = f"ERROR: {exc}. Change the call instead of repeating it."
                state.last_observation = observation
                state.ledger.append(_ledger_line(step, "shell", observation))
                await run.emit(
                    {
                        "type": "tool",
                        "tool": "agent.shell",
                        "args": {"command": command[:200]},
                        "result": _clip(observation, 600),
                        "ms": 0,
                        "ok": not observation.startswith("ERROR"),
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
            except hooks.Denied as exc:
                # The refusal is already in the transcript as a `hook` event. Aborting the
                # repair loop because one path is frozen would end the whole revision; the
                # loop instead sees the reason and can choose another path.
                observation = f"ERROR: {exc}. Pick a different path, or finish without it."
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
