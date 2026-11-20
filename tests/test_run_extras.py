"""The bookkeeping around a run: failure log, regressions, receipt, progress, memory, MCP, fallback.

None of these changes what a run builds; they change whether the next person can tell what
happened. The tests are as mechanical as the code: a row appears, a case is replayed, a
receipt names the right files, the checklist ticks, memory survives, an MCP tool that writes
is refused.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_harness.modules import failures, llm, mcp, memory, progress, receipt  # noqa: E402


class Budget:
    def as_dict(self) -> dict:
        return {"tokens": 100, "usd": 0.5, "calls": 3}


class FakeRun:
    """Exactly the surface the receipt, progress and memory modules read."""

    def __init__(self, workspace: Path, prompt: str = "build a counter page") -> None:
        self.workspace = workspace
        self.run_id = "abcdef123456"
        self.prompt = prompt
        self.created = "2026-11-20T10:00:00+00:00"
        self.depth = "deep"
        self.done = True
        self.budget = Budget()
        self.verification = {
            "ok": True,
            "stage": "post-build",
            "errors": 0,
            "warnings": 1,
            "issues": [],
        }
        self.files = {
            "index.html": {"agent": "a1", "content": "<html></html>", "done": True},
            "app.js": {"agent": "a2", "content": "const x = 1;\n", "done": True, "revised": True},
        }
        self.units = [
            SimpleNamespace(file="index.html", title="Markup", agent="a1"),
            SimpleNamespace(file="app.js", title="Wiring", agent="a2"),
        ]
        self.events = [
            {"type": "tool", "tool": "file_manager.write", "ok": True},
            {"type": "tool", "tool": "process.run", "ok": False},
            {"type": "verify", "stage": "post-build", "ok": True, "errors": 0, "warnings": 1, "checked": 2},
            {"type": "approval", "commands": ["npm install --no-audit"]},
        ]

    def file_summary(self) -> list[dict]:
        return [
            {"path": path, "agent": meta.get("agent"), "lines": 2, "done": True, "bytes": 12}
            for path, meta in self.files.items()
        ]

    def metrics(self) -> dict:
        return {"totals": {"total_tokens": 1234, "calls": 3, "ms": 4500, "cost_usd": 0.0123}}


# ---------- failures ----------


def test_a_blocked_run_writes_one_classified_row(tmp_path, monkeypatch):
    log = tmp_path / "failures.md"
    monkeypatch.setattr(failures, "LOG", log)
    report = {
        "ok": False,
        "issues": [{"file": "app.js", "level": "error", "code": "missing_id", "detail": "queries '#ghost'"}],
    }
    assert failures.record(FakeRun(tmp_path), report) is True
    body = log.read_text(encoding="utf-8")
    assert failures.classify(report) == "wiring"
    assert "app.js" in body and "wiring" in body
    # One row per failure, and the header is written once.
    failures.record(FakeRun(tmp_path), report)
    after = log.read_text(encoding="utf-8")
    assert after.count("| date |") == 1
    assert [line for line in after.splitlines() if line.startswith("| 20")] != []
    assert after.count("`abcdef123456`") == 2


def test_a_promoted_run_replays_as_a_regression(tmp_path, monkeypatch):
    store = tmp_path / "regressions.jsonl"
    monkeypatch.setattr(failures, "REGRESSIONS", store)
    report = {
        "ok": False,
        "issues": [{"file": "app.js", "level": "error", "code": "missing_id", "detail": "queries '#ghost'"}],
    }
    assert failures.promote(FakeRun(tmp_path), report) is True
    entries = failures.load_regressions()
    assert entries and entries[0]["files"]["app.js"]
    assert entries[0]["name"].startswith("regression-")


def test_classification_falls_back_to_the_message():
    assert failures.classify({}, "Run stopped by its budget — 20000 tokens") == "budget"
    assert failures.classify({}, "Run stopped at its time limit") == "timeout"
    assert failures.classify({}, "something else") == "unknown"


# ---------- receipt ----------


def test_the_receipt_names_files_checks_tools_and_approvals(tmp_path):
    run = FakeRun(tmp_path)
    path = receipt.write(run)
    assert path is not None and path.name == "RECEIPT.md"
    body = path.read_text(encoding="utf-8")
    assert "Run receipt" in body
    assert "`index.html`" in body and "`app.js`" in body
    assert "post-build" in body
    assert "1 failed" in body  # the process.run that did not succeed
    assert "npm install --no-audit" in body
    assert "1,234 tokens" in body


def test_a_blocked_receipt_records_the_open_issue(tmp_path):
    run = FakeRun(tmp_path)
    run.done = False
    run.verification = {
        "ok": False,
        "stage": "post-repair",
        "errors": 1,
        "issues": [{"file": "styles.css", "level": "error", "code": "empty", "detail": "still empty"}],
    }
    receipt.write(run)
    body = (tmp_path / "RECEIPT.md").read_text(encoding="utf-8")
    assert "stopped short" in body
    assert "styles.css" in body and "still empty" in body


# ---------- progress ----------


def test_the_checklist_starts_open_and_ticks_per_file(tmp_path):
    run = FakeRun(tmp_path)
    progress.write(run, "a counter", run.units)
    body = (tmp_path / progress.FILENAME).read_text(encoding="utf-8")
    assert body.count("- [ ]") == 2
    progress.tick(run, "index.html")
    done, open_ = progress.state(run)
    assert done == ["index.html"] and open_ == ["app.js"]
    assert "1/2 planned files" in progress.orient(run)
    assert "app.js" in progress.orient(run)


# ---------- memory ----------


def test_memory_survives_a_run_and_is_read_back(tmp_path, monkeypatch):
    path = tmp_path / "memory.md"
    monkeypatch.setattr(memory, "PATH", path)
    run = FakeRun(tmp_path)
    assert memory.append(run, "shipped") is True
    loaded = memory.load()
    assert "build a counter page" in loaded and "shipped" in loaded
    memory.append(run, "stopped short")
    assert memory.load().count("## ") == 2


# ---------- MCP ----------

SERVER = r"""
import json, sys
def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()
for line in sys.stdin:
    message = json.loads(line)
    mid, method = message.get("id"), message.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": "2024-11-05", "capabilities": {}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [
            {"name": "read_file", "description": "read one file", "annotations": {"readOnlyHint": True}},
            {"name": "write_file", "description": "write one file", "annotations": {"readOnlyHint": False}},
        ]}})
    elif method == "tools/call":
        send({"jsonrpc": "2.0", "id": mid, "result": {"content": [
            {"type": "text", "text": message["params"]["name"] + " ok"}
        ]}})
"""


@pytest.fixture()
def fake_server(tmp_path, monkeypatch):
    script = tmp_path / "server.py"
    script.write_text(SERVER, encoding="utf-8")
    config = tmp_path / "harness.mcp.json"
    config.write_text(
        json.dumps(
            {
                "servers": {
                    "fake": {
                        "command": sys.executable,
                        "args": [str(script)],
                        "allow": ["read_file", "write_file"],
                        "allow_exec": True,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(mcp, "CONFIG", config)
    return config


def test_read_only_tool_is_called_over_stdio(fake_server):
    assert asyncio.run(mcp.call("fake", "read_file", {})) == "read_file ok"


def test_a_writing_tool_is_refused_even_when_allow_listed(fake_server):
    with pytest.raises(mcp.MCPError, match="read-only"):
        asyncio.run(mcp.call("fake", "write_file", {}))


def test_inventory_says_which_tools_are_callable(fake_server):
    rows = asyncio.run(mcp.inventory("fake"))
    verdicts = {row["name"]: row["callable"] for row in rows}
    assert verdicts == {"read_file": True, "write_file": False}


def test_starting_a_server_needs_explicit_approval(tmp_path, monkeypatch):
    config = tmp_path / "harness.mcp.json"
    config.write_text(
        json.dumps({"servers": {"fake": {"command": sys.executable, "args": ["-c", "pass"]}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(mcp, "CONFIG", config)
    with pytest.raises(mcp.MCPError, match="allow_exec"):
        asyncio.run(mcp.call("fake", "read_file", {}))


# ---------- fallback model ----------


def test_fallback_uses_the_configured_model_and_keeps_the_provider(monkeypatch):
    from ai_harness.models import Specialist

    monkeypatch.setattr(llm, "FALLBACK_MODEL", "backup-model")
    monkeypatch.setattr(llm, "FALLBACK_BASE_URL", "")
    spec = Specialist(id="a", name="A", model="main-model", baseUrl="https://example.test/v1", apiKey="k")
    target = llm.fallback_for(spec)
    assert target is not None
    assert target.model == "backup-model"
    assert target.baseUrl == spec.baseUrl


# ---------- resume and the preview probe ----------


def test_resume_skips_planning_and_concludes_from_disk(tmp_path, monkeypatch):
    """A resumed run keeps its files, never re-plans, and still reaches the gate."""
    import ai_harness.core.state as state_mod
    from ai_harness.core.orchestrator import Orchestrator
    from ai_harness.core.state import RunState
    from ai_harness.models import Specialist

    monkeypatch.setattr(state_mod, "RUNS_ROOT", tmp_path)
    workspace = tmp_path / "resume1"
    workspace.mkdir()
    (workspace / "index.html").write_text("<html><body><h1>Hi</h1></body></html>\n", encoding="utf-8")
    (workspace / ".harness-plan.json").write_text(
        json.dumps(
            {
                "objective": "a page",
                "files": ["index.html"],
                "units": [
                    {
                        "id": "u1",
                        "title": "Markup",
                        "role": "frontend",
                        "file": "index.html",
                        "objective": "markup",
                        "agent": "a1",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    spec = Specialist(id="a1", name="A", role="reviewer", model="m", baseUrl="https://example.test/v1", apiKey="k")
    run = RunState(run_id="resume1", prompt="build a page", specialists=[spec], fresh=False)

    async def fake_call(agent, messages, **kwargs):  # noqa: ANN001 - the llm.call shape
        return "APPROVED", llm.Usage()

    monkeypatch.setattr(llm, "call", fake_call)
    asyncio.run(Orchestrator(run).resume_all())

    kinds = [event["type"] for event in run.events]
    assert run.events[0]["type"] == "start" and run.events[0].get("resumed") is True
    assert "plan" in kinds and "scaffold" not in kinds
    assert "done" in kinds and run.done is True
    assert (workspace / "RECEIPT.md").is_file()


def test_preview_probe_without_a_service_is_an_honest_failure():
    from ai_harness.modules import probe

    report = asyncio.run(probe.check(None))
    assert report["ok"] is False
    assert report["issues"][0]["code"] == "not_serving"


def test_no_fallback_when_it_would_be_the_same_model(monkeypatch):
    from ai_harness.models import Specialist

    monkeypatch.setattr(llm, "FALLBACK_MODEL", "main-model")
    assert llm.fallback_for(Specialist(id="a", name="A", model="main-model")) is None
    monkeypatch.setattr(llm, "FALLBACK_MODEL", "")
    assert llm.fallback_for(Specialist(id="a", name="A", model="main-model")) is None
