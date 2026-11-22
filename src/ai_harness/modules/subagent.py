"""Subagents: a subtask gets its own message list, and returns one text result.

The pipeline's specialists share a room — they see each other's contracts, the research
notes, the peer files. That is what makes a build coherent and it is also why a long run
gets expensive: every specialist's prompt carries everyone else's context, and a question
that needs ten tool calls pollutes the prompt of every later turn.

A subagent is the opposite trade. It is given a task, a fresh `messages[]`, and the tools
it needs; it works in isolation and its **final text** comes back as one result. The
parent never sees the ten tool calls, only the answer. That is the whole mechanism, and
the two properties that make it worth having are:

* **Isolation is real.** The subagent's list is built here and discarded here. Nothing it
  read enters the parent's context except the string it returns, which is clipped.
* **The parent stays in charge.** A subagent cannot emit `done`, cannot write outside the
  run workspace, and cannot exceed its own step and byte bounds. It is a function call
  with a budget, not a second agent with a vote.

The loop is the same text protocol `modules/agent_loop.py` uses, deliberately: one JSON
action per turn works on every OpenAI-compatible endpoint and on Ollama, and a subagent
that needs a second protocol would be a second thing to debug. What differs is the tool
set — a subagent gets read-only tools plus `answer`, never `write` — because the reason to
delegate is to *find something out*, not to hand off authorship.

`run_subagent()` is the only entry point. It returns a `Report`, never raises for a
provider error, and always ends: a subagent that cannot finish returns what it has with
`complete=False`, so the parent can decide to proceed or to ask again.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..config.settings import SUBAGENT_STEPS
from . import agent_loop, llm, tool_log

TOOLS = ("list", "read", "search", "answer")
ANSWER = "answer"


def _first(args: dict, keys: tuple[str, ...], default: Any = "") -> Any:
    """The first present key. A model flattens `args` often enough to need this."""
    for key in keys:
        value = args.get(key)
        if value is not None:
            return value
    return default


def _clip(text: str, limit: int) -> str:
    """Shorten an observation, saying that it was shortened."""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 60)].rstrip() + "\n…[truncated — read or search for the rest]"


# What a subagent may do. `write` and `edit` are absent on purpose: a delegate that can
# change the tree is an author, and authorship stays with the specialist that owns the
# file. A subagent that needs a change reports it instead.
TOOL_SPECS: list[dict] = [
    spec for spec in agent_loop.TOOL_SPECS if spec["function"]["name"] in ("list", "read", "search")
] + [
    {
        "type": "function",
        "function": {
            "name": ANSWER,
            "description": "Return your final answer to the parent. Ends the subtask.",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
    }
]

SYSTEM = """You are a subagent. You were given one question by a specialist on a build team.
You have your own context and your own tools; the specialist will see only the text you
return, not your steps.

The question:
{task}

What the harness knows about the tree:
{tree}

Work in small steps. Call exactly one tool per turn, as exactly ONE JSON object and
nothing else — no prose, no fences:
{{"thought": "one sentence", "tool": "read", "args": {{"path": "index.html"}}}}

Tools:
- list {{}} — every file in the run workspace.
- read {{"path": "..."}} — the exact current text of one file.
- search {{"query": "...", "path": "."}} — literal text across the run.
- answer {{"text": "..."}} — your final answer. Ends the subtask.

Rules:
1. Read before you conclude. A claim about a file you did not read is a guess.
2. Answer the question that was asked, in under {words} words. Quote the exact line or
   selector you are relying on, with its file and line number.
3. If the answer is not in the tree, say so plainly and name what you looked at. "Not
   present" is a useful answer; an invented one is worse than no answer.
4. You cannot change files. If a change is needed, describe it in your answer."""


@dataclass
class Report:
    """What a subagent returned, and what it cost to get there."""

    task: str
    text: str = ""
    complete: bool = False
    steps: int = 0
    ledger: list[str] = field(default_factory=list)
    usage: Any = None

    def public(self) -> dict:
        return {
            "task": self.task,
            "text": self.text,
            "complete": self.complete,
            "steps": self.steps,
        }


def _attempted_tool(text: str) -> str:
    """The tool name a reply asked for, even when this module does not offer it."""
    for raw in agent_loop._objects(text or ""):  # noqa: SLF001 - the shared tolerant scanner
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            name = str(obj.get("tool") or obj.get("action") or "").strip().lower()
            if name:
                return name
    return ""


def _tree(run) -> str:
    """A short listing for the subagent's prompt: paths and sizes, never contents."""
    try:
        items = run.fm.tree()
    except (AttributeError, OSError):
        items = [
            {"path": path, "lines": str(meta.get("content") or "").count("\n") + 1}
            for path, meta in (getattr(run, "files", {}) or {}).items()
        ]
    if not items:
        return "(the workspace is empty)"
    shown = [f"- {item['path']} ({item.get('lines', 0)} lines)" for item in items[:40]]
    if len(items) > 40:
        shown.append(f"… and {len(items) - 40} more")
    return "\n".join(shown)


def _read(run, path: str) -> str:
    try:
        return run.fm.read(path)
    except AttributeError:
        return str((getattr(run, "files", {}).get(path) or {}).get("content") or "")
    except Exception as exc:  # noqa: BLE001 - a refused path is an observation
        raise agent_loop.ToolError(str(exc)) from exc


def _execute(run, tool: str, args: dict) -> str:
    """Run one read-only tool. Returns an observation; raises ToolError on a refusal."""
    if tool == "list":
        return _tree(run)
    if tool == "read":
        path = str(_first(args, ("path", "file", "target"))).strip()
        if not path:
            raise agent_loop.ToolError("read needs a path")
        content = _read(run, path)
        if not content:
            return f"{path} is empty"
        return _clip(content, 6000)
    if tool == "search":
        query = str(_first(args, ("query", "pattern", "text"))).strip()
        if not query:
            raise agent_loop.ToolError("search needs a non-empty query")
        hits: list[str] = []
        for item in run.fm.tree() if hasattr(run, "fm") else []:
            path = str(item["path"])
            if path.startswith(".") or item.get("bytes", 0) > 256 * 1024:
                continue
            try:
                body = _read(run, path)
            except Exception:  # noqa: BLE001
                continue
            for number, line in enumerate(body.splitlines(), start=1):
                if query in line:
                    hits.append(f"{path}:{number}: {line.strip()[:180]}")
                    if len(hits) >= 20:
                        break
            if len(hits) >= 20:
                break
        return "\n".join(hits) if hits else f"no literal match for {query!r}"
    raise agent_loop.ToolError(f"unknown tool {tool!r}; use one of {', '.join(TOOLS)}")


async def run_subagent(
    run,
    agent,
    task: str,
    max_steps: int = SUBAGENT_STEPS,
    words: int = 150,
) -> Report:
    """Run one isolated subtask and return its answer.

    The parent's context is untouched: `messages` is local, the ledger is local, and only
    `Report.text` crosses back. A provider error ends the subtask with `complete=False`
    and whatever was learned, which is more useful to the parent than an exception.
    """
    report = Report(task=task)
    messages = [
        {
            "role": "system",
            "content": SYSTEM.format(task=task.strip()[:2000], tree=_tree(run), words=words),
        },
        {"role": "user", "content": "Begin. Call one tool."},
    ]
    await run.emit(
        {
            "type": "message",
            "from": agent.id,
            "to": "all",
            "kind": "work",
            "text": f"{agent.name} delegated a subtask: {task.strip()[:140]}",
        }
    )

    for step in range(1, max_steps + 1):
        report.steps = step
        try:
            text, usage = await llm.call(agent, messages)
        except llm.ProviderError as exc:
            report.ledger.append(f"{step}. provider error → {exc}")
            break
        if usage is not None:
            report.usage = usage
            await run.emit(run.track_usage(agent, "subagent", usage))

        call, error = parse(text)
        if call is None or str(call.get("tool")) not in TOOLS:
            # A tool this subagent does not have is a refusal, not a parse failure: the
            # model asked for something real and the harness said no. Record it, so the
            # transcript shows an attempted write rather than a model that never tried.
            attempted = _attempted_tool(text)
            if attempted:
                await run.emit(
                    {
                        "type": "tool",
                        "tool": f"subagent.{attempted}",
                        "args": {},
                        "result": f"refused: a subagent may only use {', '.join(TOOLS)}",
                        "ms": 0,
                        "ok": False,
                        "error": f"{attempted} is not available to a subagent",
                        "agent": agent.id,
                        "phase": "subagent",
                    }
                )
                report.ledger.append(f"{step}. {attempted} → refused (not a subagent tool)")
                messages.append({"role": "assistant", "content": text})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"Refused: {attempted} is not one of your tools. You may only use "
                            f"{', '.join(TOOLS)}. If a change is needed, describe it in your answer."
                        ),
                    }
                )
                continue
            # One correction, then stop: a subagent that cannot speak the protocol is
            # burning the parent's budget to learn nothing.
            if step == 1:
                messages.append({"role": "assistant", "content": text})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"That was not a valid step ({error or 'unknown tool'}). Reply with exactly one "
                            f'JSON object, for example {{"tool":"read","args":{{"path":"index.html"}}}}'
                        ),
                    }
                )
                continue
            report.ledger.append(f"{step}. invalid reply → stopped")
            break

        tool = str(call["tool"])
        args = dict(call.get("args") or {})
        if tool == ANSWER:
            report.text = str(_first(args, ("text", "answer", "summary"))).strip()
            report.complete = bool(report.text)
            report.ledger.append(f"{step}. answer → {len(report.text)} chars")
            break

        observation = ""
        try:
            async with tool_log.call(run, f"subagent.{tool}", args, agent=agent.id, phase="subagent") as logged:
                try:
                    observation = _execute(run, tool, args)
                except Exception as exc:  # noqa: BLE001 - a refused tool is an observation
                    logged.fail(str(exc))
                    observation = f"ERROR: {type(exc).__name__}: {exc}"
                logged.set_result(observation)
        except Exception as exc:  # noqa: BLE001 - thrash refusal, never a crash
            observation = f"ERROR: {exc}. Change the call instead of repeating it."
        report.ledger.append(f"{step}. {tool} → {' '.join(observation.splitlines()[:1])[:120]}")
        messages.append({"role": "assistant", "content": text})
        messages.append({"role": "user", "content": f"Observation:\n{_clip(observation, 3000)}"})

    await run.emit(
        {
            "type": "message",
            "from": agent.id,
            "to": "all",
            "kind": "work",
            "text": (
                f"{agent.name}'s subagent {'answered' if report.complete else 'stopped without an answer'} "
                f"after {report.steps} step(s)."
            ),
        }
    )
    return report


def as_context(report: Report, chars: int = 1200) -> str:
    """The subagent's answer as a prompt block, fenced because a file wrote it."""
    if not report.text:
        return ""
    body = report.text if len(report.text) <= chars else report.text[: chars - 40] + "\n…[truncated]"
    return f"Subagent finding on {report.task.strip()[:120]}:\n{body}"


def parse(text: str) -> tuple[dict | None, str]:
    """Extract one tool call, validated against *this* module's tool set.

    `agent_loop.parse_step` rejects any tool outside its own list, and `answer` is not in
    it — the repair loop has no reason to end early. Reusing it directly made every
    subagent answer read as an invalid step, so the tolerance is borrowed and the
    vocabulary is local.
    """
    call, error = agent_loop.parse_step(text)
    if call is not None and str(call.get("tool")) in TOOLS:
        return call, ""
    # `answer` is not in the shared vocabulary, so recover it from the same object the
    # shared parser already found rather than re-implementing the tolerant scan.
    for raw in agent_loop._objects(text or ""):  # noqa: SLF001 - one shared scanner, not a copy
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        tool = str(obj.get("tool") or obj.get("action") or "").strip().lower()
        if tool not in TOOLS:
            continue
        args = obj.get("args")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        if not isinstance(args, dict):
            args = {key: value for key, value in obj.items() if key not in ("thought", "tool", "action")}
        return {"thought": str(obj.get("thought") or "").strip(), "tool": tool, "args": args}, ""
    return None, error or f"no tool call from {', '.join(TOOLS)}"


def dump(report: Report) -> str:
    """A stable JSON line for the transcript, when a caller wants the whole report."""
    return json.dumps(report.public(), default=str)
