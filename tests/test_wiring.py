"""Deterministic wiring: the markup is the interface, so the scripts adapt to it.

Two models writing two halves of one page disagree on names — the markup declares
`btnStartPause`, the script queries `startBtn` — and a weak model repeats that on every
rewrite. `review.wire` closes the gap without a model, so this test pins the behaviour:
renames where a declared id already carries the tokens, real elements where nothing does,
and a markup that finally loads the script and stylesheet the run wrote.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import ai_harness.core.state as state_mod  # noqa: E402
from ai_harness.core.state import RunState  # noqa: E402
from ai_harness.models import Specialist  # noqa: E402
from ai_harness.nodes import review, verify  # noqa: E402

MARKUP = """<!doctype html>
<html lang="en">
<head><title>Focus Timer</title></head>
<body>
  <div id="controls">
    <button id="btnStartPause">Start</button>
    <button id="btnReset">Reset</button>
  </div>
  <ul id="historyList"></ul>
</body>
</html>
"""

SCRIPT = """const start = document.getElementById('startBtn');
const reset = document.getElementById('resetBtn');
const status = document.getElementById('statusMessage');
const history = document.getElementById('historyList');
start.addEventListener('click', () => {});
reset.addEventListener('click', () => {});
status.textContent = 'ready';
history.innerHTML = '';
"""


def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RunState:
    monkeypatch.setattr(state_mod, "RUNS_ROOT", tmp_path)
    agent = Specialist(id="vega", name="Vega", role="frontend", model="stub")
    run = RunState(run_id="wiring", prompt="build a timer", specialists=[agent], depth="quick")
    run.files["index.html"] = {"agent": "vega", "content": MARKUP, "done": True}
    run.files["app.js"] = {"agent": "atlas", "content": SCRIPT, "done": True}
    run.files["style.css"] = {"agent": "orion", "content": "body { color: #eee; }\n", "done": True}
    return run


def test_wire_maps_selectors_and_loads_assets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = _run(tmp_path, monkeypatch)
    assert verify.check(run.files)["ok"] is False, "the input must be the broken case"

    changes = asyncio.run(review.wire(run))

    script = run.files["app.js"]["content"]
    markup = run.files["index.html"]["content"]

    assert changes, "wiring must report what it changed"
    # resetBtn has an exact token twin; startBtn fits the toggle button; nothing fits
    # statusMessage, so it becomes a real element instead of a guess.
    assert "getElementById('btnReset')" in script
    assert "getElementById('btnStartPause')" in script
    assert "getElementById('statusMessage')" in script
    assert 'id="statusMessage"' in markup
    assert "historyList" in script and markup.count('id="historyList"') == 1
    # A script that drives this markup must actually be loaded, and so must the stylesheet.
    assert '<script src="app.js" defer></script>' in markup
    assert '<link rel="stylesheet" href="style.css">' in markup
    assert run.fm.read("index.html") == markup
    assert run.fm.read("app.js") == script

    report = verify.check(run.files)
    assert report["ok"] is True, report["issues"]


def test_wire_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = _run(tmp_path, monkeypatch)
    asyncio.run(review.wire(run))
    wired_markup = run.files["index.html"]["content"]
    wired_script = run.files["app.js"]["content"]

    assert asyncio.run(review.wire(run)) == []
    assert run.files["index.html"]["content"] == wired_markup
    assert run.files["app.js"]["content"] == wired_script


def test_wire_refuses_a_script_that_is_not_this_page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A script querying ids the markup never had anything to do with is a defect the

    verifier must keep reporting — the harness does not invent a DOM for it, or the
    broken-wiring eval case would stop meaning anything.
    """
    run = _run(tmp_path, monkeypatch)
    run.files["app.js"] = {
        "agent": "atlas",
        "content": 'const ghost = document.getElementById("ghost");\nghost.textContent = "1";\n',
        "done": True,
    }

    changes = asyncio.run(review.wire(run))
    markup = run.files["index.html"]["content"]
    assert 'id="ghost"' not in markup, changes
    assert "<script" not in markup, "an unrelated script must not be wired in"
    assert verify.check(run.files)["ok"] is False


def test_wire_leaves_a_wired_page_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = _run(tmp_path, monkeypatch)
    run.files["index.html"] = {
        "agent": "vega",
        "content": MARKUP.replace("</head>", '  <link rel="stylesheet" href="style.css">\n</head>')
        .replace('<ul id="historyList"></ul>', '<ul id="historyList"></ul>\n  <p id="statusMessage"></p>')
        .replace("</body>", '  <script src="app.js"></script>\n</body>'),
        "done": True,
    }
    wired_script = SCRIPT.replace("startBtn", "btnStartPause").replace("resetBtn", "btnReset")
    run.files["app.js"] = {"agent": "atlas", "content": wired_script, "done": True}

    assert asyncio.run(review.wire(run)) == []
    assert verify.check(run.files)["ok"] is True
