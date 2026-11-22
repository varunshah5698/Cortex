"""The workshop's `code_search` tool: pattern search with a real regex engine.

`code_search_tool.go` shells out to ripgrep. That is the right instinct — a coding agent
that can only match literal substrings cannot answer "where is this function defined" —
but a harness should not add a binary dependency to get it, and `rg --type go` is a
mapping this project would have to maintain. Python's own `re` over the run's files is
enough for the size of tree a run produces, and it keeps the search inside the same
containment as every other read.

What this adds over `agent_loop`'s `search`, which stays as the cheap literal path:

* a **regex** pattern, so `def \\w+\\(` is one call instead of a guess;
* an **extension** filter, so a match in a lockfile does not bury the one in the source;
* **case sensitivity** as an explicit choice rather than the caller's accident;
* a hard cap on matches, because a broad pattern over a generated tree returns thousands
  of lines and the model pays for every one of them.

Every path resolves through the caller's `FileManager`, so this reads exactly what the
loop is already allowed to read and nothing else.
"""

from __future__ import annotations

import re
import signal
import threading
from dataclasses import dataclass

MAX_MATCHES = 60
MAX_LINE = 200
MAX_FILE_BYTES = 256 * 1024
# A pattern a model wrote can backtrack catastrophically; a search is not worth a stall.
REGEX_BUDGET_SECONDS = 2.0


class _BudgetExceeded(Exception):
    """Raised from the SIGALRM handler to stop a pattern that will not finish."""


@dataclass(frozen=True)
class Hit:
    path: str
    line: int
    text: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.text}"


class SearchError(RuntimeError):
    """The pattern could not be used. The message is an observation, not a crash."""


def compile_pattern(pattern: str, case_sensitive: bool = False) -> re.Pattern[str]:
    """Compile a model-supplied pattern, refusing one that cannot run."""
    if not pattern:
        raise SearchError("pattern is required")
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        return re.compile(pattern, flags)
    except re.error as exc:
        raise SearchError(f"invalid regular expression {pattern!r}: {exc}") from exc


def _wanted(path: str, extension: str) -> bool:
    if not extension:
        return True
    suffix = extension if extension.startswith(".") else f".{extension}"
    return path.lower().endswith(suffix.lower())


def search(
    files: list[dict],
    read,
    pattern: str,
    scope: str = ".",
    extension: str = "",
    case_sensitive: bool = False,
    limit: int = MAX_MATCHES,
) -> list[Hit]:
    """Search the run's files. `read(path)` must resolve through the caller's sandbox.

    `files` is the `FileManager.tree()` shape: dicts with `path` and `bytes`. Dotfiles and
    the harness's own bookkeeping are the caller's to exclude; this function searches
    exactly the list it is given.
    """
    regex = compile_pattern(pattern, case_sensitive)
    prefix = scope.strip().strip("./")
    hits: list[Hit] = []

    # A model-supplied regex can backtrack for minutes on one line. An alarm interrupts
    # the C match loop; it is only available on the main thread, which is where the
    # harness runs its tools.
    previous_handler = previous_timer = None
    # shortcut: only the main thread can take SIGALRM; harness tools run on the loop thread
    armed = threading.current_thread() is threading.main_thread()

    def expire(_signum, _frame):
        raise _BudgetExceeded

    if armed:
        previous_handler = signal.signal(signal.SIGALRM, expire)
        previous_timer = signal.getitimer(signal.ITIMER_REAL)
        signal.setitimer(signal.ITIMER_REAL, REGEX_BUDGET_SECONDS)
    try:
        for item in files:
            path = str(item.get("path") or "")
            if not path or not _wanted(path, extension):
                continue
            if prefix and not path.startswith(prefix):
                continue
            if int(item.get("bytes") or 0) > MAX_FILE_BYTES:
                continue
            try:
                body = read(path)
            except Exception:  # noqa: BLE001 - an unreadable file is skipped, not fatal
                continue
            for number, line in enumerate(body.splitlines(), start=1):
                if regex.search(line):
                    hits.append(Hit(path=path, line=number, text=line.strip()[:MAX_LINE]))
                    if len(hits) >= limit:
                        return hits
    except _BudgetExceeded as exc:
        raise SearchError(
            f"pattern {pattern!r} took longer than {REGEX_BUDGET_SECONDS:g}s; simplify it or narrow the scope"
        ) from exc
    finally:
        if armed:
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)
            signal.signal(signal.SIGALRM, previous_handler)
    return hits


def render(hits: list[Hit], pattern: str, scope: str, limit: int = MAX_MATCHES) -> str:
    """The observation a model sees: matches, or a sentence saying there were none."""
    if not hits:
        return f"no match for {pattern!r} under {scope}"
    body = "\n".join(hit.render() for hit in hits)
    if len(hits) >= limit:
        body += f"\n…[stopped at {limit} matches; narrow the pattern or the path]"
    return body
