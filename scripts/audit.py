#!/usr/bin/env python3
"""The monthly subtraction pass: what should be deleted, not what should be added.

Every harness grows rules, prompts and tools faster than it sheds them, and a rule nobody
applies is worse than no rule — it reads as policy while changing nothing. This reports
three things a maintainer can act on in ten minutes:

* **stale docs** — Markdown nobody has touched in `HARNESS_DOCS_STALE_DAYS` (default 90);
  each one is either still true (touch it after a read) or wrong (delete it);
* **unreferenced prompts** — templates in `modules/prompts.py` that no other module names,
  which are almost always a prompt whose caller was rewritten;
* **failure components** — the counts from `failures.md`, because the component that keeps
  failing is the one whose check or prompt deserves the next change.

Run `python scripts/audit.py` for the report, `--fail` to make drift an error (useful in a
scheduled job), and remember the intended outcome is fewer lines.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from ai_harness.modules import prompts  # noqa: E402 - path must be set first

DOC_SUFFIXES = (".md",)
SKIP_DOCS = {"node_modules", ".venv", "dist", "workspace", "logs", "evals"}


def stale_docs(days: int) -> list[tuple[str, int]]:
    """Docs older than `days`, newest first. Only this project's own Markdown."""
    cutoff = time.time() - days * 86_400
    found: list[tuple[str, int]] = []
    for path in ROOT.rglob("*.md"):
        if any(part in SKIP_DOCS for part in path.parts) or path.name.startswith("RECEIPT"):
            continue
        try:
            modified = path.stat().st_mtime
        except OSError:
            continue
        if modified < cutoff:
            found.append((str(path.relative_to(ROOT)), int((time.time() - modified) / 86_400)))
    return sorted(found, key=lambda item: item[1], reverse=True)


def unused_prompts() -> list[str]:
    """Prompt templates nothing else references."""
    sources = [
        path
        for path in (ROOT / "src").rglob("*.py")
        if path.name != "prompts.py" and "modules/prompts.py" not in str(path)
    ]
    bodies = {path: path.read_text(encoding="utf-8", errors="replace") for path in sources}
    unused: list[str] = []
    for name in dir(prompts):
        if name.startswith("_") or not name.isupper():
            continue
        if not isinstance(getattr(prompts, name), str):
            continue
        pattern = re.compile(rf"\bprompts\.{name}\b|\b{name}\b")
        if not any(pattern.search(body) for body in bodies.values()):
            unused.append(name)
    return sorted(unused)


def failure_components() -> Counter[str]:
    """How many recorded failures each component owns, from `failures.md`."""
    log = ROOT / "failures.md"
    if not log.is_file():
        return Counter()
    counts: Counter[str] = Counter()
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) >= 3 and cells[0] and cells[0] != "date" and not cells[0].startswith("-"):
            counts[cells[2]] += 1
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="audit.py", description="Report what the harness no longer earns.")
    parser.add_argument("--days", type=int, default=90, help="age after which a doc is stale (default 90)")
    parser.add_argument("--fail", action="store_true", help="exit 1 when there is drift to look at")
    args = parser.parse_args(argv)

    stale = stale_docs(args.days)
    unused = unused_prompts()
    components = failure_components()

    print("== stale docs ==")
    for path, age in stale:
        print(f"  {age:>4}d  {path}")
    if not stale:
        print("  none — every doc has been read recently")

    print("\n== unreferenced prompt templates ==")
    for name in unused:
        print(f"  {name} — defined in modules/prompts.py, named nowhere else")
    if not unused:
        print("  none")

    print("\n== failure components ==")
    for component, count in components.most_common():
        print(f"  {count:>3}  {component}")
    if not components:
        print("  none recorded yet")

    drift = bool(stale or unused)
    print(f"\n{'drift to review' if drift else 'nothing to subtract today'}")
    return 1 if (drift and args.fail) else 0


if __name__ == "__main__":
    raise SystemExit(main())
