"""Tier 4: open the served run in a real browser and report what a person would see.

The static checks read files, the compiler reads types, and the HTTP probe reads the
page and its assets. None of them can see a page that renders but whose button throws, a
console exception that blanks the app, or a request that fails only after hydration. This
tier drives headless Chrome over the same CDP path as `scripts/record_site.mjs`, so it
adds no npm package and no second browser engine.

It is deliberately a *result*, not a harness failure: a broken page is reported as
`ok: False` with issues, and a machine without Chrome is `skipped: True`. The caller
emits it as a `verify` event with `stage: "browser"`; it never flips a run's terminal
`done`, because serving happens after the pipeline's verdict.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
from pathlib import Path

from ..config.settings import (
    BROWSER_CHECK,
    BROWSER_INTERACT,
    BROWSER_TIMEOUT,
    CHROME_PATH,
    NODE_BIN,
    ROOT,
)
from . import permissions


def _skipped(note: str) -> dict:
    return {
        "ok": True,
        "skipped": True,
        "stage": "browser",
        "errors": 0,
        "warnings": 0,
        "checked": 0,
        "issues": [],
        "note": note,
    }


def _report(raw: dict, url: str) -> dict:
    issues = [
        {
            "file": url,
            "level": str(item.get("level") or "error"),
            "code": str(item.get("code") or "browser"),
            "detail": str(item.get("detail") or "")[:500],
        }
        for item in (raw.get("issues") or [])
        if isinstance(item, dict)
    ]
    errors = sum(1 for item in issues if item["level"] == "error")
    return {
        "ok": bool(raw.get("ok")) and errors == 0,
        "skipped": bool(raw.get("skipped")),
        "stage": "browser",
        "errors": errors,
        "warnings": len(issues) - errors,
        "checked": 1,
        "issues": issues[:10],
        "url": url,
        "title": str(raw.get("title") or ""),
        "text_length": int(raw.get("textLength") or 0),
        "interactive": int(raw.get("interactive") or 0),
        "buttons": int(raw.get("buttons") or 0),
        "interactions": raw.get("interactions") or [],
        "console_errors": raw.get("consoleErrors") or [],
        "page_errors": raw.get("pageErrors") or [],
        "http_errors": raw.get("httpErrors") or [],
        "failed_requests": raw.get("failedRequests") or [],
        "note": str(raw.get("error") or raw.get("note") or "")[:500],
    }


async def check(url: str | None, interact: bool | None = None) -> dict:
    """Drive Chrome at `url`. Never raises for a broken page; returns a verify report."""
    if not url:
        return {
            "ok": False,
            "skipped": False,
            "stage": "browser",
            "errors": 1,
            "warnings": 0,
            "checked": 0,
            "issues": [
                {
                    "file": "preview",
                    "level": "error",
                    "code": "not_serving",
                    "detail": "the run has no ready service to open in a browser",
                }
            ],
        }
    if not BROWSER_CHECK:
        return _skipped("browser checks are disabled (HARNESS_BROWSER_CHECK=0)")
    if not shutil.which(NODE_BIN):
        return _skipped(f"node is not installed ({NODE_BIN})")
    if not Path(CHROME_PATH).is_file():
        return _skipped(f"Chrome is not installed at {CHROME_PATH}")

    script = Path(ROOT) / "scripts" / "browser_check.mjs"
    if not script.is_file():
        return _skipped(f"browser check script is missing at {script}")
    command = [NODE_BIN, str(script), url, f"--timeout={int(BROWSER_TIMEOUT * 1000)}"]
    if BROWSER_INTERACT if interact is None else interact:
        command.append("--interact")

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(ROOT),
            env=permissions.safe_env({"HARNESS_CHROME": CHROME_PATH}),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=BROWSER_TIMEOUT + 5)
        except asyncio.TimeoutError:
            # Node's Chrome child is in this process group; killing only Node would leave
            # a headless Chrome holding the profile and a debugging port.
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                process.kill()
            await process.wait()
            return {
                "ok": False,
                "skipped": False,
                "stage": "browser",
                "errors": 1,
                "warnings": 0,
                "checked": 0,
                "issues": [
                    {
                        "file": url,
                        "level": "error",
                        "code": "browser_timeout",
                        "detail": f"Chrome did not finish within {BROWSER_TIMEOUT + 5:.0f}s",
                    }
                ],
            }
    except OSError as exc:
        return _skipped(f"could not start the browser check: {exc}")

    lines = [line for line in stdout.decode("utf-8", "replace").splitlines() if line.strip()]
    raw: dict = {}
    if lines:
        try:
            raw = json.loads(lines[-1])
        except json.JSONDecodeError:
            raw = {}
    if not raw:
        detail = stderr.decode("utf-8", "replace").strip()[-500:] or "browser check produced no report"
        return {
            "ok": False,
            "skipped": False,
            "stage": "browser",
            "errors": 1,
            "warnings": 0,
            "checked": 0,
            "issues": [{"file": url, "level": "error", "code": "browser_failed", "detail": detail}],
        }
    return _report(raw, url)
