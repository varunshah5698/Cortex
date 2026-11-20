"""Tier-2 verification: run the artifact's own toolchain over the tree the run wrote.

`nodes/verify.py` reads the tree and reports contradictions between files. It cannot know
that a TypeScript file has a type error or that a Python file does not parse — for that
you need the compiler the project itself declares. This module is that second sensor:

* Python is compiled in-process with ``compile(source, path, "exec")``. No subprocess, no
  imports, no execution: a syntax error is a fact about the text.
* TypeScript is checked with the project's own ``node_modules/.bin/tsc --noEmit`` when a
  run installed TypeScript into its workspace. That binary only exists after somebody
  approved the install, so running it adds no new privilege.

Both are bounded: a timeout, no network, and the same stripped environment every other
model-started process gets (`modules/permissions.safe_env`). A tree that cannot be
checked (no tsconfig, no node_modules) says so in its result instead of reporting a pass
it did not earn.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
from pathlib import Path

from ..config.settings import ROOT
from . import permissions

# `src/App.tsx(12,7): error TS2322: Type 'string' is not assignable to type 'number'.`
TS_ERROR = re.compile(r"^(?P<file>[^\s(]+)\((?P<line>\d+),(?P<col>\d+)\): error (?P<code>TS\d+): (?P<detail>.+)$")
TYPECHECK_TIMEOUT = 120.0
TS_BUILDS = ("node_modules/typescript/bin/tsc", "node_modules/.bin/tsc")
# The harness's own esbuild (a Vite dependency) parses TS/JSX without the run's install.
ESBUILD_BUILDS = ("node_modules/.bin/esbuild", "node_modules/esbuild/bin/esbuild")
JS_LOADERS = {".tsx": "tsx", ".jsx": "jsx", ".ts": "ts", ".mts": "ts", ".js": "js", ".mjs": "js"}


def python_syntax(path: str, content: str) -> dict | None:
    """One issue when `content` is not valid Python, otherwise None.

    Compiling is not importing: nothing in the file runs, so a model's top-level side
    effects cannot fire while the harness is judging it.
    """
    try:
        compile(content, path, "exec", dont_inherit=True)
    except SyntaxError as exc:
        where = f" (line {exc.lineno})" if exc.lineno else ""
        return {
            "file": path,
            "level": "error",
            "code": "syntax_error",
            "detail": f"does not compile: {exc.msg}{where}",
        }
    except ValueError as exc:  # null bytes and the like
        return {"file": path, "level": "error", "code": "syntax_error", "detail": f"does not compile: {exc}"}
    return None


def _esbuild() -> Path | None:
    for relative in ESBUILD_BUILDS:
        candidate = ROOT / relative
        if candidate.is_file():
            return candidate
    return None


def javascript_syntax(path: str, content: str) -> dict | None:
    """Parse one JS/TS/JSX/TSX file with the harness's esbuild.

    This is the pre-install sensor: `tsc` only exists after a run installs it, but a
    truncated JSX file is already wrong. esbuild transforms stdin, so nothing executes.
    """
    suffix = Path(path).suffix.lower()
    loader = JS_LOADERS.get(suffix)
    binary = _esbuild()
    if not loader or binary is None:
        return None
    try:
        result = subprocess.run(  # noqa: S603 - fixed binary, no shell, bounded input
            [str(binary), f"--loader={loader}", "--format=esm", "--log-level=error"],
            input=content,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode == 0:
        return None
    detail = (result.stderr or result.stdout or "does not parse").strip().splitlines()[-1]
    return {
        "file": path,
        "level": "error",
        "code": "syntax_error",
        "detail": f"does not parse: {detail[:300]}",
    }


def syntax_issues(files: dict[str, dict]) -> list[dict]:
    """Every .py or JS/TS file in the tree that does not parse. Deterministic."""
    issues: list[dict] = []
    for path, meta in files.items():
        suffix = Path(path).suffix.lower()
        if suffix != ".py" and suffix not in JS_LOADERS:
            continue
        content = str(meta.get("content") or "")
        if not content.strip():
            continue  # an empty file is already reported by the structural checks
        issue = python_syntax(path, content) if suffix == ".py" else javascript_syntax(path, content)
        if issue:
            issues.append(issue)
    return issues


def _binary(workspace: Path) -> Path | None:
    for relative in TS_BUILDS:
        candidate = workspace / relative
        if candidate.is_file():
            return candidate
    return None


async def typescript(workspace: Path) -> dict:
    """Type-check the tree with its own `tsc`, when one was installed.

    Returns ``{"checked", "issues", "note"}``. ``checked`` is False for a tree with no
    TypeScript, which is not a failure — it is a sensor that does not apply.
    """
    config = workspace / "tsconfig.json"
    binary = _binary(workspace)
    if not config.is_file():
        return {"checked": False, "issues": [], "note": "no tsconfig.json"}
    if binary is None:
        return {"checked": False, "issues": [], "note": "typescript is not installed in this workspace"}

    try:
        process = await asyncio.create_subprocess_exec(
            str(binary),
            "--noEmit",
            "--pretty",
            "false",
            cwd=str(workspace),
            env=permissions.safe_env({"CI": "1", "NO_COLOR": "1"}),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except (OSError, ValueError) as exc:
        return {"checked": False, "issues": [], "note": f"could not start tsc: {exc}"}

    try:
        raw, _ = await asyncio.wait_for(process.communicate(), TYPECHECK_TIMEOUT)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return {"checked": False, "issues": [], "note": f"tsc did not finish within {TYPECHECK_TIMEOUT:.0f}s"}

    text = raw.decode("utf-8", "replace")
    issues: list[dict] = []
    for line in text.splitlines():
        match = TS_ERROR.match(line.strip())
        if not match:
            continue
        issues.append(
            {
                "file": match.group("file"),
                "level": "error",
                "code": "type_error",
                "detail": f"{match.group('code')} at {match.group('line')}:{match.group('col')} — {match.group('detail')[:240]}",
            }
        )
    return {
        "checked": True,
        "issues": issues[:20],
        "note": f"tsc --noEmit exited {process.returncode}",
    }


async def check(workspace: Path, files: dict[str, dict]) -> dict:
    """Both sensors, as verify-shaped issues plus a line explaining what ran."""
    issues = syntax_issues(files)
    typed = await typescript(workspace)
    issues.extend(typed["issues"])
    notes = [f"{len(issues)} toolchain issue(s)"]
    if typed["checked"]:
        notes.append(typed["note"])
    else:
        notes.append(f"type check skipped: {typed['note']}")
    return {"issues": issues, "checked": typed["checked"], "note": "; ".join(notes)}
