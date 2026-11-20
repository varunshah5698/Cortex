"""Tier 4: a real Chrome sees what the file checks and the HTTP probe cannot."""

from __future__ import annotations

import asyncio
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_harness.config.settings import CHROME_PATH, NODE_BIN  # noqa: E402
from ai_harness.modules import browser  # noqa: E402

pytestmark = pytest.mark.skipif(
    not Path(CHROME_PATH).is_file() or shutil.which(NODE_BIN) is None,
    reason="headless Chrome or node is not installed",
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _serve(root: Path) -> tuple[subprocess.Popen, str]:
    port = _free_port()
    process = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1", "--directory", str(root)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return process, f"http://127.0.0.1:{port}/"


def _wait(url: str) -> None:
    import urllib.request

    for _ in range(50):
        try:
            urllib.request.urlopen(url, timeout=1).read(1)
            return
        except Exception:
            time.sleep(0.05)
    raise AssertionError(f"{url} did not start")


def test_a_working_button_is_a_clean_browser_verdict(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text(
        "<!doctype html><title>Counter</title><body>"
        '<button id="go">Go</button><p id="out">ready</p>'
        "<script>document.getElementById('go').onclick=()=>{document.getElementById('out').textContent='clicked'}</script>"
        "</body>",
        encoding="utf-8",
    )
    process, url = _serve(tmp_path)
    _wait(url)
    try:
        report = asyncio.run(browser.check(url, interact=True))
    finally:
        process.kill()
        process.wait()

    assert report["ok"] is True, report
    assert report["interactions"] and report["interactions"][0]["changed"] is True
    assert report["buttons"] == 1


def test_a_throwing_page_is_a_browser_finding(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text(
        "<!doctype html><title>Broken</title><body><h1>Broken page</h1><script>throw new Error('boom')</script></body>",
        encoding="utf-8",
    )
    process, url = _serve(tmp_path)
    _wait(url)
    try:
        report = asyncio.run(browser.check(url, interact=True))
    finally:
        process.kill()
        process.wait()

    assert report["ok"] is False, report
    assert any(issue["code"] in ("page_error", "console_error") for issue in report["issues"]), report


def test_disabled_browser_checks_are_explicitly_skipped(monkeypatch) -> None:
    monkeypatch.setattr(browser, "BROWSER_CHECK", False)
    report = asyncio.run(browser.check("http://127.0.0.1:9/"))
    assert report["ok"] is True
    assert report["skipped"] is True
