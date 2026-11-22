"""Containment for the processes the runner starts.

`modules/permissions.py` decides *whether* a command may run; this module limits what
that command can reach once it does. The distinction matters: allow/ask/deny is a filter
over command strings, and `npm install` runs whatever install script a model put in
`package.json`. The guide's Appendix A says the same thing — a pattern rule is not a
sandbox — so the step that executes model-authored code is wrapped in a real one.

On macOS that is `sandbox-exec` with a generated profile:

* **writes** are denied anywhere under the owner's home except the run workspace and the
  package-manager caches (`~/Library`, `~/.npm`, `~/.cache`), so an install cannot drop a
  file into Documents, Desktop or a sibling project;
* **reads** of key material (`~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.netrc`, `~/.config/gh`,
  `~/.docker/config.json`) are denied outright, so a dependency's postinstall script
  cannot exfiltrate a key it found on disk.

Everywhere else — another platform, `HARNESS_SANDBOX=0`, or a machine without
`sandbox-exec` — the child runs unsandboxed and `describe()` says so. The harness never
claims containment it does not have; the service log carries the sentence either way.

A profile that cannot be prepared is *not* another such fallback: `prefix` raises
`SandboxError` and the command does not start. A child running with the owner's access
while the log says it is contained is exactly the failure this module exists to prevent.
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

ENABLED = os.getenv("HARNESS_SANDBOX", "1").strip().lower() not in ("0", "false", "no", "off")
HOME = Path.home()

# Writes are denied under HOME, then allowed back for these subtrees.
CACHE_WRITES = ("Library", ".npm", ".cache", ".yarn", ".local/share")
# Key material a model-written build has no reason to read.
SECRET_READS = (
    ".ssh",
    ".aws",
    ".gnupg",
    ".netrc",
    ".config/gh",
    ".docker/config.json",
    ".kube",
)
SYSTEM_WRITES = ("/private/tmp", "/tmp", "/dev", "/var/folders")


class SandboxError(RuntimeError):
    """Containment was required and could not be set up. The command must not run."""


def available() -> bool:
    """True when a real containment mechanism exists here. Checked, never assumed."""
    return ENABLED and sys.platform == "darwin" and shutil.which("sandbox-exec") is not None


def _sub(path: Path | str) -> str:
    return f'(subpath "{path}")'


def profile(workspace: Path) -> str:
    """The sandbox profile for one run's workspace.

    ``(allow default)`` then narrow denials: a profile that fails to parse would let
    `sandbox-exec` refuse to start, which the runner reports as a dead server rather than
    silently running unprotected.
    """
    allow_writes = [_sub(workspace), *(_sub(HOME / part) for part in CACHE_WRITES)]
    allow_writes += [_sub(path) for path in SYSTEM_WRITES]
    deny_reads = " ".join(_sub(HOME / part) for part in SECRET_READS)
    return "\n".join(
        [
            "(version 1)",
            "(allow default)",
            f"(deny file-write* {_sub(HOME)})",
            f"(allow file-write* {' '.join(allow_writes)})",
            f"(deny file-read* {deny_reads})",
        ]
    )


def _profile_dir() -> Path:
    """A private directory for this account's profiles.

    The temp directory is shared. A fixed `harness-sandbox-<run>.sb` there let a file
    owned by another user shadow ours, so the write failed and the run silently fell
    back to no containment. One directory per uid, mode 0700 and owned by us, is ours
    alone; a directory we cannot vouch for is an error, not a fallback.
    """
    getuid = getattr(os, "getuid", None)
    uid = getuid() if callable(getuid) else None
    name = "harness-sandbox" if uid is None else f"harness-sandbox-{uid}"
    path = Path(tempfile.gettempdir()) / name
    try:
        path.mkdir(mode=0o700, exist_ok=True)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or (uid is not None and info.st_uid != uid):
            raise SandboxError(f"{path} is not a directory owned by this account")
        if stat.S_IMODE(info.st_mode) & 0o077:
            path.chmod(0o700)
    except SandboxError:
        raise
    except OSError as exc:
        raise SandboxError(f"cannot prepare the sandbox profile directory {path}: {exc}") from exc
    return path


def _write_profile(path: Path, text: str) -> None:
    """Write the profile atomically: `sandbox-exec` must never read a half-written one."""
    try:
        fd, scratch = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    except OSError as exc:
        raise SandboxError(f"cannot create the sandbox profile {path}: {exc}") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(scratch, path)
    except OSError as exc:
        try:
            os.unlink(scratch)
        except OSError:
            pass
        raise SandboxError(f"cannot write the sandbox profile {path}: {exc}") from exc


def _profile_file(run_id: str, workspace: Path) -> Path:
    """The profile for this run, written only when its content changed."""
    # A run id is a name, never a path: `a/b` must not escape the profile directory.
    run_name = Path(run_id).name or "run"
    path = _profile_dir() / f"{run_name}.sb"
    text = profile(workspace)
    try:
        current = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        current = None
    except OSError as exc:
        raise SandboxError(f"cannot read the sandbox profile {path}: {exc}") from exc
    if current != text:
        _write_profile(path, text)
    return path


def prefix(run_id: str, workspace: Path) -> list[str]:
    """`sandbox-exec -f profile --` when containment is available, else nothing.

    A profile that cannot be prepared raises `SandboxError` rather than yielding an
    empty prefix: the caller must not start the command unprotected.
    """
    if not available():
        return []
    return ["sandbox-exec", "-f", str(_profile_file(run_id, workspace)), "--"]


def describe() -> str:
    """A sentence for the service log: what is actually containing this process."""
    if available():
        return "sandbox: writes limited to the run workspace and caches; key stores unreadable"
    if not ENABLED:
        return "sandbox: disabled by HARNESS_SANDBOX — processes run with the owner's access"
    if sys.platform != "darwin":
        return f"sandbox: not available on {sys.platform} — processes run with the owner's access"
    return "sandbox: sandbox-exec is missing — processes run with the owner's access"
