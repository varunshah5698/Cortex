"""A deterministic stand-in for the model.

Every provider call the harness makes is answered from the case, so a scripted eval costs
nothing, needs no key, and produces the same result twice. The replies are keyed off the
prompt contract each node uses, which also means an eval run exercises the real planner,
executor, verifier and reviewer code rather than a mock of them.
"""

from __future__ import annotations

import json
import re

from ..modules.usage import StreamStats, Usage
from .cases import Case

MARKER = re.compile(r"<<<FILE\s+(.+?)>>>")

# Distinctive lines from prompts.py, checked in this order. A contract that changes
# wording without changing intent still matches its neighbour, so this stays stable.
PROBES: tuple[tuple[str, str], ...] = (
    ("PLANNER", "You plan software work"),
    ("CONTRACT", "The planned files:"),
    ("REVIEW", "Reply with 3-5 numbered findings"),
    ("REVIEW_QUESTION", "one precise question about how they will fix it"),
    ("AUTHOR_REPLY", "Answer in under 60 words"),
    ("REWRITE_REASON", "Think in stages: 1. ACCEPT"),
    ("SYNTHESIS", "what the team built, who wrote which file"),
    ("RESEARCH_QUERY", "Turn this request into up to"),
    ("RESEARCH_SUMMARY", "Using only the sources below"),
    ("REASON_CRITIC", "Attack the plan"),
    ("REASON", "Think before writing. Use these stages"),
    ("CODE", "Output format, exactly:"),
)


def prompt_kind(system: str) -> str:
    for name, probe in PROBES:
        if probe in system:
            return name
    return ""


class Scripted:
    """Answers every call for one case. `calls` makes the cost visible in the result."""

    def __init__(self, case: Case):
        self.case = case
        self.calls = 0

    # ---------- text ----------

    def _broken_file(self) -> str:
        return next(
            (path for path, body in self.case.code.items() if "ghost" in body),
            self.case.files[-1] if self.case.files else "app.js",
        )

    def review_text(self) -> str:
        if self.case.expect_ok:
            return "APPROVED"
        path = self._broken_file()
        return (
            f"- {path}: queries an id the markup never declares, so the page does nothing.\n"
            "- index.html: the handler is never wired to a real element."
        )

    def code_for(self, system: str) -> str:
        match = MARKER.search(system)
        path = match.group(1).strip() if match else (self.case.files[0] if self.case.files else "index.html")
        body = self.case.code.get(path, "")
        # An empty body is a case about a file nobody wrote; the fence is still opened so
        # the executor sees the file was attempted rather than silently skipped.
        return f"<<<FILE {path}>>>\n```\n{body}\n```\n"

    def _reply(self, system: str) -> str:
        kind = prompt_kind(system)
        self.calls += 1
        if kind == "PLANNER":
            return json.dumps(self.case.plan)
        if kind == "CONTRACT":
            return "Publishes the ids and classes in this file; consumes none."
        if kind == "RESEARCH_SUMMARY":
            return "No external API is required; the request is self-contained."
        if kind == "REVIEW":
            return self.review_text()
        if kind == "REVIEW_QUESTION":
            return "Which id will the handler query instead?"
        if kind == "AUTHOR_REPLY":
            return "An id the markup actually declares."
        if kind == "SYNTHESIS":
            return f"Wrote {len(self.case.files)} files for the request."
        if kind == "CODE":
            return self.code_for(system)
        if kind == "REASON":
            return "restate: build this file. options: one. decision: the simple one."
        if kind == "REASON_CRITIC":
            return "VERDICT: SOUND\nFINDINGS: 1. Keep the ids in the markup and query only those."
        if kind == "REWRITE_REASON":
            return "accept: the finding. plan: rewrite against the real ids."
        if kind == "RESEARCH_QUERY":
            return ""
        return ""

    # ---------- provider surface ----------

    async def call(self, spec, messages, think=False, on_think=None, json_mode=False, temperature=None):
        system = str((messages[0] if messages else {}).get("content", ""))
        text = self._reply(system)
        if on_think is not None and prompt_kind(system) in ("REASON", "REWRITE_REASON"):
            on_think(text)
        return text, Usage(prompt_tokens=len(system) // 4, completion_tokens=len(text) // 4, calls=1, ms=1)

    async def stream(self, spec, messages, think=True, stats=None, json_mode=False, temperature=None):
        system = str((messages[0] if messages else {}).get("content", ""))
        kind = prompt_kind(system)
        stats = stats or StreamStats()

        if kind in ("REASON", "REWRITE_REASON"):
            thought = self._reply(system)
            stats.thinking.append(thought)
            yield ("think", thought)
            stats.usage.calls += 1
            return

        if kind == "REVIEW":
            stats.thinking.append("reading every file")
            yield ("think", "reading every file")

        text = self._reply(system)
        stats.content.append(text)
        stats.usage.calls += 1
        for index in range(0, len(text), 200):
            yield ("token", text[index : index + 200])
