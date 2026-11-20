"""The repair loop: one JSON action per turn, a flat state block, and a check-gated finish.

No provider is called. The scripted `llm.call` is the same offline-model idea the eval
suite uses, so the test exercises the parser, the tools, the ledger and the gate rather
than a mock of them.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_harness.models import Specialist  # noqa: E402
from ai_harness.modules import agent_loop, thrash  # noqa: E402
from ai_harness.modules.llm import Usage  # noqa: E402
from ai_harness.tools.file_manager import FileManager  # noqa: E402


class FakeRun:
    def __init__(self, workspace: Path, files: dict[str, str]) -> None:
        self.workspace = workspace
        self.run_id = "agent-loop-test"
        self.prompt = "Build a counter page"
        self.specialists: list[Specialist] = []
        self.files = {path: {"agent": "vega", "content": body, "done": True} for path, body in files.items()}
        self.events: list[dict] = []
        self.fm = FileManager(workspace)
        for path, body in files.items():
            self.fm.write(path, body)

    async def emit(self, event: dict) -> None:
        self.events.append(event)

    def track_usage(self, agent: Specialist, phase: str, usage: Usage) -> dict:
        return {"type": "usage", "agent": agent.id, "phase": phase, **usage.as_dict()}


class Script:
    """Answer each call from a list, and keep the prompts for assertions."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.messages: list[list[dict]] = []

    async def __call__(self, agent, messages, **kwargs):  # noqa: ANN001
        self.messages.append(messages)
        text = self.replies.pop(0) if self.replies else '{"tool":"done","args":{}}'
        return text, Usage(prompt_tokens=10, completion_tokens=5, calls=1, ms=1)


def _checker(run: FakeRun, clean_when: str):
    async def check() -> dict:
        content = run.fm.read("index.html")
        clean = clean_when in content
        issues = (
            []
            if clean
            else [
                {
                    "file": "index.html",
                    "level": "error",
                    "code": "missing_id",
                    "detail": "queries '#ghost' but index.html declares no element with that id",
                }
            ]
        )
        return {
            "ok": clean,
            "issues": issues,
            "errors": 0 if clean else 1,
            "warnings": 0,
            "checked": 1,
        }

    return check


def _run(monkeypatch, script: Script) -> None:
    monkeypatch.setattr(agent_loop.llm, "call", script)


def test_repair_reads_edits_checks_and_finishes(tmp_path: Path, monkeypatch) -> None:
    broken = (
        "<html><body><button id=\"go\">Go</button><script>document.getElementById('ghost')</script></body></html>\n"
    )
    fixed = "<html><body><button id=\"go\">Go</button><script>document.getElementById('go')</script></body></html>\n"
    run = FakeRun(tmp_path, {"index.html": broken})
    agent = Specialist(id="vega", name="Vega", role="frontend", model="stub")
    run.specialists = [agent]
    thrash.reset(run.run_id)

    script = Script(
        [
            '{"thought":"look at the file","tool":"read","args":{"path":"index.html"}}',
            '{"thought":"point the selector at the real id","tool":"edit","args":{"path":"index.html",'
            '"find":"getElementById(\'ghost\')","replace":"getElementById(\'go\')"}}',
            '{"thought":"check the tree","tool":"check"}',
            '{"thought":"clean now","tool":"done","args":{"summary":"selector fixed"}}',
        ]
    )
    _run(monkeypatch, script)

    changed = asyncio.run(
        agent_loop.repair(run, agent, "index.html", "missing id", _checker(run, "getElementById('go')"))
    )

    assert changed is True
    assert run.fm.read("index.html") == fixed
    assert len(script.messages) == 4
    tools = [event["tool"] for event in run.events if event.get("type") == "tool"]
    assert "agent.edit" in tools and "agent.check" in tools, tools
    assert any(event.get("type") == "file_end" and event.get("repaired") for event in run.events), run.events
    # The next turn sees the action ledger and the edited file, not a growing chat.
    second = script.messages[1][0]["content"]
    assert "ACTION LEDGER" in second and "read index.html" in second
    assert "getElementById('ghost')" in second


def test_invalid_json_costs_one_correction_not_the_run(tmp_path: Path, monkeypatch) -> None:
    run = FakeRun(tmp_path, {"index.html": "<html><body>ok</body></html>\n"})
    agent = Specialist(id="vega", name="Vega", role="frontend", model="stub")
    run.specialists = [agent]
    thrash.reset(run.run_id)
    script = Script(["I think the file is fine.", '{"tool":"done","args":{}}'])
    _run(monkeypatch, script)

    changed = asyncio.run(agent_loop.repair(run, agent, "index.html", "nothing", _checker(run, "<body>ok")))

    assert changed is False
    assert len(script.messages) == 2
    assert "INVALID STEP" in script.messages[1][0]["content"]


def test_done_is_refused_while_the_check_fails(tmp_path: Path, monkeypatch) -> None:
    run = FakeRun(tmp_path, {"index.html": "<html><body>still broken</body></html>\n"})
    agent = Specialist(id="vega", name="Vega", role="frontend", model="stub")
    run.specialists = [agent]
    thrash.reset(run.run_id)
    script = Script(['{"tool":"done","args":{}}', '{"tool":"done","args":{}}', '{"tool":"done","args":{}}'])
    _run(monkeypatch, script)

    changed = asyncio.run(
        agent_loop.repair(run, agent, "index.html", "still failing", _checker(run, "impossible"), max_steps=2)
    )

    assert changed is False
    assert len(script.messages) == 2
    assert "DONE REFUSED" in script.messages[1][0]["content"]


def test_a_non_unique_edit_is_refused_and_the_file_is_untouched(tmp_path: Path, monkeypatch) -> None:
    body = "<html><body><p>x</p><p>x</p></body></html>\n"
    run = FakeRun(tmp_path, {"index.html": body})
    agent = Specialist(id="vega", name="Vega", role="frontend", model="stub")
    run.specialists = [agent]
    thrash.reset(run.run_id)
    script = Script(
        [
            '{"tool":"edit","args":{"path":"index.html","find":"x","replace":"y"}}',
            '{"tool":"done","args":{}}',
        ]
    )
    _run(monkeypatch, script)

    changed = asyncio.run(agent_loop.repair(run, agent, "index.html", "fix x", _checker(run, "<body>")))

    assert changed is False
    assert run.fm.read("index.html") == body
    failed = [event for event in run.events if event.get("type") == "tool" and not event.get("ok")]
    assert failed and "appears 2 times" in failed[0]["error"], failed


def test_a_path_outside_the_workspace_is_an_observation_not_a_crash(tmp_path: Path, monkeypatch) -> None:
    run = FakeRun(tmp_path, {"index.html": "<html><body>ok</body></html>\n"})
    agent = Specialist(id="vega", name="Vega", role="frontend", model="stub")
    run.specialists = [agent]
    thrash.reset(run.run_id)
    script = Script(
        [
            '{"tool":"read","args":{"path":"../secret"}}',
            '{"tool":"done","args":{}}',
        ]
    )
    _run(monkeypatch, script)

    changed = asyncio.run(agent_loop.repair(run, agent, "index.html", "nothing", _checker(run, "<body>ok")))

    assert changed is False
    failed = [event for event in run.events if event.get("type") == "tool" and not event.get("ok")]
    assert failed and "invalid path" in failed[0]["error"], failed


def test_the_working_set_keeps_only_the_most_recent_files(tmp_path: Path) -> None:
    run = FakeRun(tmp_path, {"a.txt": "a\n", "b.txt": "b\n", "c.txt": "c\n"})
    agent = Specialist(id="vega", name="Vega", role="frontend", model="stub")
    state = agent_loop.LoopState(run, agent, "a.txt", "nothing", _checker(run, ""))
    state._remember("a.txt", "a\n")  # noqa: SLF001
    state._remember("b.txt", "b\n")  # noqa: SLF001
    state._remember("c.txt", "c\n")  # noqa: SLF001

    assert list(state.active) == ["b.txt", "c.txt"]
    rendered = state.render()
    assert "FILE c.txt" in rendered and "FILE a.txt" not in rendered


def test_render_stays_within_the_context_budget(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(agent_loop, "AGENT_CONTEXT_CHARS", 800)
    run = FakeRun(tmp_path, {"big.js": "const x = 1;\n" * 400})
    agent = Specialist(id="vega", name="Vega", role="frontend", model="stub")
    state = agent_loop.LoopState(run, agent, "big.js", "fix the big file", _checker(run, ""))
    state._remember("big.js", run.fm.read("big.js"))  # noqa: SLF001

    rendered = state.render()

    assert len(rendered) <= 800
    assert "truncated" in rendered


def test_two_identical_checks_stop_a_spinning_loop(tmp_path: Path, monkeypatch) -> None:
    run = FakeRun(tmp_path, {"index.html": "<html><body>broken</body></html>\n"})
    agent = Specialist(id="vega", name="Vega", role="frontend", model="stub")
    run.specialists = [agent]
    thrash.reset(run.run_id)
    script = Script(['{"tool":"check"}'] * 5)
    _run(monkeypatch, script)

    changed = asyncio.run(agent_loop.repair(run, agent, "index.html", "still failing", _checker(run, "impossible")))

    assert changed is False
    assert len(script.messages) == 3  # three checks, then the no-progress stop
    assert any("same issues" in str(event.get("text", "")) for event in run.events)


def test_harness_bookkeeping_cannot_be_rewritten_by_the_loop(tmp_path: Path, monkeypatch) -> None:
    run = FakeRun(tmp_path, {"index.html": "<html><body>ok</body></html>\n"})
    agent = Specialist(id="vega", name="Vega", role="frontend", model="stub")
    run.specialists = [agent]
    thrash.reset(run.run_id)
    script = Script(
        [
            '{"tool":"write","args":{"path":"todo.md","content":"- [x] everything"}}',
            '{"tool":"done","args":{}}',
        ]
    )
    _run(monkeypatch, script)

    changed = asyncio.run(agent_loop.repair(run, agent, "index.html", "nothing", _checker(run, "<body>ok")))

    assert changed is False
    assert not (tmp_path / "todo.md").exists()
    failed = [event for event in run.events if event.get("type") == "tool" and not event.get("ok")]
    assert failed and "harness-owned" in failed[0]["error"], failed


def test_parse_step_tolerates_fences_prose_and_flat_args() -> None:
    call, error = agent_loop.parse_step(
        'Here is the call:\n```json\n{"thought":"read","tool":"read_file","file":"index.html"}\n```'
    )
    assert error == ""
    assert call and call["tool"] == "read" and call["args"]["file"] == "index.html"

    call, error = agent_loop.parse_step('{"tool":"finish","summary":"done"}')
    assert error == ""
    assert call and call["tool"] == "done"

    call, error = agent_loop.parse_step("no json here")
    assert call is None and "JSON" in error
