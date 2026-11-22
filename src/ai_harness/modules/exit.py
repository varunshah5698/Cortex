"""Typed exit statuses: every run ends with a name, not a shrug.

The most portable idea in mini-swe-agent is not its loop — it is that a run cannot
simply *stop*. It ends through one of a small set of named exceptions (`Submitted`,
`LimitsExceeded`, `TimeExceeded`, `FormatError`, `RepeatedFormatError`,
`UserInterruption`), each carrying the message that explains it, and the loop catches
them in one place and appends that message to the transcript. The control flow and the
record of *why* the run ended are the same object, so they cannot disagree.

Cortex already detects every one of those conditions — a budget stop, a deadline, a
cancelled run, a verifier that refused `done` — but records them as `done: bool` plus
free text, so "did this run succeed, and if not, why" is a question you answer by
reading the transcript. This module makes it a field.

The statuses are few and mutually exclusive on purpose. A run that ran out of money and
never reached verification ended for the first reason; reporting both would be reporting
a cause and its consequence as peers.
"""

from __future__ import annotations

# The run reached its goal and the gate accepted it.
SUBMITTED = "submitted"
# The verifier refused `done`: the tree does not pass its own checks.
UNVERIFIED = "unverified"
# A budget (tokens, dollars or calls) stopped the run.
LIMITS = "limits"
# The wall clock stopped the run.
TIME = "time"
# The provider failed in a way retries and the configured fallback could not fix.
PROVIDER = "provider"
# The model could not speak the tool protocol, repeatedly.
FORMAT = "format"
# A person stopped it.
CANCELLED = "cancelled"
# It stopped for a reason nothing above names.
UNKNOWN = "unknown"

ALL = (SUBMITTED, UNVERIFIED, LIMITS, TIME, PROVIDER, FORMAT, CANCELLED, UNKNOWN)

# Exactly one status counts as success, on purpose: a run is done when its own checks say
# so, and every other ending is a run that stopped short of that.
SUCCESS = frozenset({SUBMITTED})

# Ordered because the first match wins, and the order is the precedence: money and time
# are checked before the provider, because a provider error that arrives after the budget
# is exhausted is a symptom of the stop, not its cause.
_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (CANCELLED, ("cancelled", "canceled", "interrupted", "user interruption")),
    (LIMITS, ("budget", "token budget", "spend budget", "call budget", "limits exceeded")),
    (TIME, ("time limit", "time exceeded", "timed out", "deadline", "wall clock")),
    (FORMAT, ("format error", "repeatedformaterror", "invalid step", "no json object")),
    (PROVIDER, ("provider", "connection", "unavailable", "rate limit", "authentication")),
)


class RunExit(RuntimeError):
    """A run stopping on purpose, carrying the status and the words that explain it.

    Raised where the condition is detected and caught once, at the top of the run, so a
    node never has to thread a "did we stop and why" flag back up the call stack. The
    message is the submission text: what the run has to say for itself.
    """

    def __init__(self, status: str, message: str = "", submission: str = "") -> None:
        self.status = status if status in ALL else UNKNOWN
        self.submission = submission or message
        super().__init__(message or self.status)

    def as_dict(self) -> dict:
        return {"exit_status": self.status, "submission": self.submission}


def classify(exc: BaseException | None = None, message: str = "") -> str:
    """The status for a failure, from its class name and its words.

    Name-and-message rather than `isinstance` on purpose: the budget, deadline, provider
    and cancellation exceptions live in four modules that already import each other, and
    an import here would be the fifth edge in that cycle for no gain. The class name is
    the more stable signal of the two, so it is tried first.
    """
    if exc is None and not message:
        return UNKNOWN
    name = type(exc).__name__ if exc is not None else ""
    text = f"{name}: {exc or message}".lower()
    for status, hints in _HINTS:
        if any(hint in text for hint in hints):
            return status
    return UNKNOWN


def of(done: bool, report: dict | None = None, message: str = "") -> str:
    """The status of a run that has finished, from its own gate and its last check.

    A run that emitted `done` without a clean check did not submit — it stopped. That is
    the distinction this function exists to keep: `done` is an event, `submitted` is a
    claim the checks agreed with.
    """
    if done:
        if report is not None and report.get("ok") is not True:
            return UNVERIFIED
        return SUBMITTED
    return classify(None, message)


def describe(status: str) -> str:
    """One clause for a receipt or a transcript line."""
    return {
        SUBMITTED: "submitted — the goal gate and the checks both passed",
        UNVERIFIED: "stopped — the verifier refused done",
        LIMITS: "stopped — a budget was exhausted",
        TIME: "stopped — the wall clock ran out",
        PROVIDER: "stopped — the provider failed past retries and the fallback",
        FORMAT: "stopped — the model could not speak the tool protocol",
        CANCELLED: "stopped — a person cancelled it",
        UNKNOWN: "stopped — no cause was recorded",
    }.get(status, f"stopped — unrecognised status {status!r}")
