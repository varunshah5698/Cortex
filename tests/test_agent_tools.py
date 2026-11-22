"""The two capabilities the workshop has that the repair loop did not: regex search and a
contained shell.

No provider is called and no real command is spawned where it matters: the shell tests
patch `shell.run` or assert on the refusal path, because the point of the module is that
an unapproved command never reaches a process at all.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_harness.modules import agent_loop, code_search, shell  # noqa: E402
from ai_harness.tools.file_manager import FileManager  # noqa: E402


class FakeRun:
    def __init__(self, workspace: Path, files: dict[str, str]) -> None:
        self.workspace = workspace
        self.run_id = "shell-test"
        self.prompt = "Build a counter page"
        self.specialists: list = []
        self.files = {path: {"agent": "vega", "content": body, "done": True} for path, body in files.items()}
        self.events: list[dict] = []
        self.fm = FileManager(workspace)
        for path, body in files.items():
            self.fm.write(path, body)

    async def emit(self, event: dict) -> None:
        self.events.append(event)

    def track_usage(self, agent, phase, usage):  # noqa: ANN001
        return {"type": "usage", "agent": agent.id, "phase": phase, **usage.as_dict()}


def _state(tmp_path: Path, files: dict[str, str]) -> agent_loop.LoopState:
    run = FakeRun(tmp_path, files)
    return agent_loop.LoopState(run=run, agent=None, path="index.html", findings="", check=None)  # type: ignore[arg-type]


# ---------- code search ----------


def test_grep_finds_a_regex_the_literal_search_cannot(tmp_path: Path) -> None:
    state = _state(
        tmp_path,
        {
            "app.py": "def build_page():\n    return 1\n\ndef render():\n    return 2\n",
            "notes.md": "build_page is the entry point\n",
        },
    )
    observation = state.execute("grep", {"pattern": r"def \w+\("})
    assert "app.py:1: def build_page():" in observation
    assert "app.py:4: def render():" in observation
    # The literal search would have needed the exact text; the regex found both.
    literal = state.execute("search", {"query": r"def \w+\("})
    assert "no literal match" in literal


def test_grep_filters_by_extension(tmp_path: Path) -> None:
    state = _state(
        tmp_path,
        {"app.py": "build_page()\n", "bundle.js": "build_page()\n", "package-lock.json": "build_page()\n"},
    )
    observation = state.execute("grep", {"pattern": "build_page", "extension": "py"})
    assert "app.py:1" in observation
    assert "bundle.js" not in observation
    assert "package-lock.json" not in observation


def test_grep_is_case_insensitive_unless_asked(tmp_path: Path) -> None:
    state = _state(tmp_path, {"app.py": "Build_Page()\n"})
    assert "app.py:1" in state.execute("grep", {"pattern": "build_page"})
    assert "no match" in state.execute("grep", {"pattern": "build_page", "case_sensitive": True})


def test_grep_refuses_an_invalid_pattern_as_an_observation(tmp_path: Path) -> None:
    state = _state(tmp_path, {"app.py": "x = 1\n"})
    with pytest.raises(agent_loop.ToolError) as exc:
        state.execute("grep", {"pattern": "def ("})
    assert "invalid regular expression" in str(exc.value)


def test_grep_skips_dotfiles_and_harness_bookkeeping(tmp_path: Path) -> None:
    state = _state(tmp_path, {"app.py": "target\n", ".env": "target\n", "todo.md": "target\n"})
    observation = state.execute("grep", {"pattern": "target"})
    assert "app.py:1" in observation
    assert ".env" not in observation
    assert "todo.md" not in observation


def test_grep_kills_a_catastrophic_pattern_at_the_budget(tmp_path: Path, monkeypatch) -> None:
    # `(a+)+$` against a line that does not match backtracks exponentially, and `re` cannot
    # be interrupted; the tool has to kill the process running it or the run stalls here.
    monkeypatch.setattr(code_search, "REGEX_BUDGET_SECONDS", 0.2)
    state = _state(tmp_path, {"app.py": "a" * 32 + "!\n"})
    started = time.monotonic()

    with pytest.raises(agent_loop.ToolError) as exc:
        state.execute("grep", {"pattern": "(a+)+$"})

    assert time.monotonic() - started < 10
    assert "search budget" in str(exc.value)


def test_code_search_caps_the_matches(tmp_path: Path) -> None:
    body = "\n".join("needle" for _ in range(200))
    state = _state(tmp_path, {"big.txt": body})
    observation = state.execute("grep", {"pattern": "needle"})
    assert observation.count("big.txt:") == code_search.MAX_MATCHES
    assert "stopped at" in observation


# ---------- shell ----------


def test_shell_refuses_a_pipeline_before_it_parses() -> None:
    with pytest.raises(shell.ShellDenied) as exc:
        shell.argv_for("ls; rm -rf ~")
    assert "metacharacter" in str(exc.value)


@pytest.mark.parametrize("command", ["ls | wc -l", "cat x > y", "echo `whoami`", "ls && rm x", "cat $(pwd)"])
def test_shell_refuses_every_way_to_smuggle_a_second_command(command: str) -> None:
    with pytest.raises(shell.ShellDenied):
        shell.argv_for(command)


def test_shell_refuses_a_denied_command(monkeypatch) -> None:
    monkeypatch.setattr(shell, "available", lambda: True)
    with pytest.raises(shell.ShellDenied) as exc:
        import asyncio

        asyncio.run(shell.run("rm -rf /", Path("/tmp"), "run-1"))
    assert "refused by policy" in str(exc.value)


def test_shell_refuses_an_ask_command_rather_than_approving_it(monkeypatch) -> None:
    # `npm install` is ASK in permissions.py: safe in the runner, where a person approves
    # it, and refused here, where nobody can.
    monkeypatch.setattr(shell, "available", lambda: True)
    with pytest.raises(shell.ShellDenied) as exc:
        import asyncio

        asyncio.run(shell.run("npm install", Path("/tmp"), "run-1"))
    assert "ask" in str(exc.value)


def test_shell_reports_itself_unavailable_without_a_sandbox(monkeypatch) -> None:
    monkeypatch.setattr(shell, "ENABLED", True)
    monkeypatch.setattr(shell.sandbox, "available", lambda: False)
    assert not shell.available()
    assert "containment" in shell.unavailable_reason()


def test_shell_is_off_by_default() -> None:
    # The module reads the environment at import; the default must be off, because a
    # capability that spawns processes is opt-in.
    assert shell.ENABLED is False or shell.available() is False


def test_the_loop_exposes_the_new_tools_to_the_model() -> None:
    assert "grep" in agent_loop.TOOLS
    assert "shell" in agent_loop.TOOLS
    # Every tool this module added must have a native spec, or a provider that speaks
    # native tools silently loses a capability the prompt advertises. Scoped to the tools
    # added here rather than the whole set: the loop is being extended by other work in
    # this tree, and this test should fail on a missing spec, not on someone else's tool.
    native = {spec["function"]["name"] for spec in agent_loop.TOOL_SPECS}
    for tool in ("grep", "shell"):
        assert tool in native, f"{tool} is in TOOLS but has no native tool spec"


def test_the_aliases_point_at_real_tools() -> None:
    for alias, target in agent_loop.TOOL_ALIASES.items():
        assert target in agent_loop.TOOLS, f"{alias} points at unknown tool {target}"
