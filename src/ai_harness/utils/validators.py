"""Small validators used across the harness."""

from __future__ import annotations

import re
from pathlib import Path

SAFE_NAME = re.compile(r"^[\w./-]+$")


class UnsafePath(ValueError):
    """Raised when a path would escape the workspace."""


def safe_relative(path: str, workspace: Path) -> Path:
    """Resolve a relative path inside `workspace`, refusing traversal."""
    raw = (path or "").strip().replace("\\", "/")
    while raw.startswith("./"):
        raw = raw[2:]
    if not raw or raw.startswith("/") or ".." in Path(raw).parts or not SAFE_NAME.match(raw):
        raise UnsafePath(f"invalid path: {path!r}")
    root = workspace.resolve()
    target = (root / raw).resolve()
    if target != root and not target.is_relative_to(root):
        raise UnsafePath(f"path escapes the workspace: {path!r}")
    return target


def slugify(value: str, fallback: str = "file") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or fallback


def strip_fences(text: str) -> str:
    body = text.strip()
    match = re.match(r"^```[a-zA-Z0-9]*\n(.*?)```$", body, flags=re.S)
    return match.group(1) if match else text
