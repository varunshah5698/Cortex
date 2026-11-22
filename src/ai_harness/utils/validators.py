"""Small validators used across the harness."""

from __future__ import annotations

import re
from pathlib import Path

SAFE_NAME = re.compile(r"^[\w./-]+$")
# A provider that turns a tool call into text can leave its transport framing in the
# generated content. DeepSeek's V4 models emit `<｜DSML｜ ...>` markers this way; Vite
# warns about the stray `<` but still builds, so the file looks shipped with the model's
# plumbing baked in. Strip only that known marker, not arbitrary angle brackets.
PROTOCOL_TAG = re.compile(r"</?[｜|]DSML[｜|][^>\n]*>\s*", re.IGNORECASE)
# A model that frames its answer with file markers can leave one in the content: the
# closing `<<<END FILE>>>` was written as the last line of two components in a real build,
# a syntax error in both. The stream router drops these now; this is the second line of
# defence for any path that writes text without going through it.
FILE_MARKER = re.compile(r"^\s*<<<\s*(?:END\s*)?FILE\b[^>]*>>>\s*$", re.IGNORECASE | re.MULTILINE)


def strip_protocol(text: str) -> str:
    """Remove a provider's tool-call framing and stray file markers from file content."""
    return FILE_MARKER.sub("", PROTOCOL_TAG.sub("", text))


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
