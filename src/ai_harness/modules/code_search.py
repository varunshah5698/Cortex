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

The pattern is model-authored, and `re` cannot be interrupted: `(a+)+$` against a short
line backtracks for minutes, and a thread stuck in that match could only be abandoned
while it kept burning a core. So the match runs in a child process and is killed at
`REGEX_BUDGET_SECONDS`. A bad pattern costs one observation, not the run.
"""

from __future__ import annotations

import multiprocessing
import re
from dataclasses import dataclass

MAX_MATCHES = 60
MAX_LINE = 200
MAX_FILE_BYTES = 256 * 1024
# A pattern a model wrote can backtrack catastrophically; a search is not worth a stall.
REGEX_BUDGET_SECONDS = 2.0


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


def _corpus(files: list[dict], read, scope: str, extension: str) -> list[tuple[str, str]]:
    """The readable candidates, as plain strings so a child process needs no callbacks."""
    prefix = scope.strip().strip("./")
    corpus: list[tuple[str, str]] = []
    for item in files:
        path = str(item.get("path") or "")
        if not path or not _wanted(path, extension):
            continue
        if prefix and not path.startswith(prefix):
            continue
        if int(item.get("bytes") or 0) > MAX_FILE_BYTES:
            continue
        try:
            corpus.append((path, read(path)))
        except Exception:  # noqa: BLE001 - an unreadable file is skipped, not fatal
            continue
    return corpus


def _matches(
    corpus: list[tuple[str, str]],
    pattern: str,
    case_sensitive: bool,
    limit: int,
    out,
) -> None:
    """The worker: match, then hand back plain tuples. Module level so `spawn` can import it."""
    regex = re.compile(pattern, 0 if case_sensitive else re.IGNORECASE)
    hits: list[tuple[str, int, str]] = []
    for path, body in corpus:
        for number, line in enumerate(body.splitlines(), start=1):
            if regex.search(line):
                hits.append((path, number, line.strip()[:MAX_LINE]))
                if len(hits) >= limit:
                    out.send(hits)
                    return
    out.send(hits)


def _bounded(
    corpus: list[tuple[str, str]], pattern: str, case_sensitive: bool, limit: int
) -> list[tuple[str, int, str]]:
    """Match in a child process, and kill it rather than wait past the budget."""
    context = _context()
    receiver, sender = context.Pipe(duplex=False)
    worker = context.Process(target=_matches, args=(corpus, pattern, case_sensitive, limit, sender), daemon=True)
    worker.start()
    sender.close()
    try:
        if not receiver.poll(REGEX_BUDGET_SECONDS):
            raise SearchError(
                f"pattern {pattern!r} exceeded the {REGEX_BUDGET_SECONDS:g}s search budget; "
                "simplify it (nested quantifiers such as (a+)+ backtrack catastrophically)"
            )
        try:
            return receiver.recv()
        except EOFError as exc:  # the worker died before answering
            raise SearchError(f"pattern {pattern!r} could not be run: the search worker failed") from exc
    finally:
        if worker.is_alive():
            # SIGKILL, not terminate(): a regex match runs entirely in C, so an inherited
            # SIGTERM handler only runs once the match returns — which is never, here.
            worker.kill()
            worker.join(1.0)
        receiver.close()


def _context():
    """Fork where the platform has it: the corpus is already readable in the child, so a
    search costs a few milliseconds instead of re-importing the package under `spawn`."""
    try:
        return multiprocessing.get_context("fork")
    except ValueError:  # pragma: no cover - Windows
        return multiprocessing.get_context()


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
    compile_pattern(pattern, case_sensitive)  # an invalid pattern is refused before any fork
    corpus = _corpus(files, read, scope, extension)
    if not corpus:
        return []
    return [
        Hit(path=path, line=number, text=text)
        for path, number, text in _bounded(corpus, pattern, case_sensitive, limit)
    ]


def render(hits: list[Hit], pattern: str, scope: str, limit: int = MAX_MATCHES) -> str:
    """The observation a model sees: matches, or a sentence saying there were none."""
    if not hits:
        return f"no match for {pattern!r} under {scope}"
    body = "\n".join(hit.render() for hit in hits)
    if len(hits) >= limit:
        body += f"\n…[stopped at {limit} matches; narrow the pattern or the path]"
    return body
