"""Filesystem tool: scaffold the tree first, then write into it, always inside the workspace."""

from __future__ import annotations

import shutil
from pathlib import Path

from ..utils.validators import UnsafePath, safe_relative


class FileManager:
    """Every write goes through here. The sandbox is the run workspace."""

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.workspace.mkdir(parents=True, exist_ok=True)

    def scaffold(self, paths: list[str]) -> list[str]:
        """Create the empty tree before any model writes a line."""
        created: list[str] = []
        for path in paths:
            try:
                target = safe_relative(path, self.workspace)
            except UnsafePath:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                target.write_text("", encoding="utf-8")
            created.append(str(target.relative_to(self.workspace)).replace("\\", "/"))
        return created

    def write(self, path: str, content: str) -> int:
        target = safe_relative(path, self.workspace)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return content.count("\n") + 1

    def read(self, path: str) -> str:
        target = safe_relative(path, self.workspace)
        if not target.exists():
            return ""
        return target.read_text(encoding="utf-8", errors="replace")

    def exists(self, path: str) -> bool:
        try:
            return safe_relative(path, self.workspace).exists()
        except UnsafePath:
            return False

    def can_write(self, path: str) -> bool:
        """True when a path resolves inside the sandbox, so a bad marker is refused early."""
        try:
            safe_relative(path, self.workspace)
            return True
        except UnsafePath:
            return False

    def tree(self) -> list[dict]:
        files: list[dict] = []
        for path in sorted(self.workspace.rglob("*")):
            if path.is_dir() or path.name.startswith("."):
                continue
            rel = str(path.relative_to(self.workspace)).replace("\\", "/")
            files.append(
                {
                    "path": rel,
                    "lines": path.read_text(encoding="utf-8", errors="replace").count("\n") + 1,
                    "bytes": path.stat().st_size,
                }
            )
        return files

    def reset(self) -> None:
        for child in self.workspace.iterdir():
            if child.name.startswith(".harness-memory"):
                continue
            shutil.rmtree(child) if child.is_dir() else child.unlink()
