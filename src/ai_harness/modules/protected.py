"""Files that judge a run cannot be edited by it.

The guide's non-negotiable rule is "never edit a test to make it pass". Prose in a prompt
does not enforce that: a revision pass is a model rewriting a file with the failing output
in front of it, and the cheapest way to turn a red test green is to weaken the test. So
the check lives where the write happens.

The line is drawn at *when*, not at *what*: a run's first write of a test file is the
build doing its job, and it is allowed. Once that file has content, a later write that
comes from a revision pass is refused, because by then the test is a contract and the code
is what is wrong. A test the plan merely declared and nobody wrote is still writable.
"""

from __future__ import annotations

import fnmatch
import posixpath

TEST_PATTERNS = (
    "test_*.py",
    "*_test.py",
    "conftest.py",
    "*.test.*",
    "*.spec.*",
    "*_test.go",
    "*.test.ts",
)
TEST_DIRS = ("tests/", "test/", "__tests__/", "spec/")


def is_test(path: str) -> bool:
    """True for anything that looks like a check rather than the thing being checked."""
    clean = posixpath.normpath(path).lstrip("./")
    if any(clean.startswith(directory) for directory in TEST_DIRS):
        return True
    return any(fnmatch.fnmatch(posixpath.basename(clean), pattern) for pattern in TEST_PATTERNS)


def frozen(path: str, mode: str, existing: str) -> bool:
    """True when this write would edit a test that is already written.

    `mode` is the sink's mode: an empty string is the build, `rewrite` is a revision.
    """
    return mode == "rewrite" and bool(existing.strip()) and is_test(path)


def describe(path: str) -> str:
    return f"{path} is a test file and tests are frozen once written — fix the code, not the test"
