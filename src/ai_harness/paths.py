"""Path containment shared by the API and the artifact origin.

Both surfaces read a run's workspace, so both must resolve a run id and a relative path
the same way. Framework-free on purpose: each app maps `UnsafePath` to its own status.
"""

from __future__ import annotations

import re
from pathlib import Path

from .config.settings import RUNS_ROOT
from .utils.validators import UnsafePath, safe_relative

# One plain segment: no `..`, no slash, no dotfile-only name.
RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def run_dir(run_id: str) -> Path:
    """Resolve a run id under RUNS_ROOT, refusing anything that is not one segment."""
    if not RUN_ID.match(run_id or ""):
        raise UnsafePath(f"invalid run id: {run_id!r}")
    root = Path(RUNS_ROOT).resolve()
    target = (root / run_id).resolve()
    if target == root or not target.is_relative_to(root):
        raise UnsafePath(f"run id escapes the runs root: {run_id!r}")
    return target


def contained(workspace: Path, path: str) -> Path:
    """Resolve `path` inside `workspace` by real path, not by string prefix."""
    return safe_relative(path, workspace)
