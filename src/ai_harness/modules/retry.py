"""Retry policy: retry the weather, never the credentials.

A 429 or a 503 is weather — the same request usually succeeds a second later. A 400 or a
401 is configuration: retrying only spends time to produce the same error.

Classification is status-first. When the adapter captured an HTTP status, that status
alone decides: listed statuses are retried, everything else is final. The message hints
exist only for failures with no status at all — transport errors and error events a
provider streams mid-response. A `Retry-After` sets the floor for the backoff.
"""

from __future__ import annotations

import asyncio
import random

from ..config.settings import RETRY_ATTEMPTS, RETRY_BASE_DELAY, RETRY_MAX_DELAY
from .budget import BudgetExceeded

# 408 timeout, 409/425 conflict/too-early, 429 rate limit, 5xx weather, 529 overloaded.
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}

# No status available: transport failures, and providers that stream an error event.
# A gateway can stream a 429 as an error frame in a 200 response (River does), so the
# rate-limit wording must be recognizable without an HTTP status.
RETRYABLE_HINTS = (
    "timeout",
    "timed out",
    "connect",
    "reset",
    "readerror",
    "read error",
    "server disconnected",
    "overloaded",
    "temporarily",
    "unavailable",
    "rate limit",
    "rate_limit",
    "ratelimit",
    "too many requests",
    "account_rate",
)

FATAL_HINTS = ("invalid api key", "no base url", "refusing", "not found")


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, BudgetExceeded):
        # A budget stop must never be retried, whatever its message happens to say.
        return False
    status = getattr(exc, "status", None)
    if status is not None:
        # A 402 with Retry-After is a free tier saying "wait", not a payment refusal
        # (Pollinations sends exactly this). Without the header it stays final.
        if status == 402 and getattr(exc, "retry_after", None):
            return True
        return status in RETRYABLE_STATUS
    text = f"{type(exc).__name__}: {exc}".lower()
    if any(hint in text for hint in FATAL_HINTS):
        return False
    return any(hint in text for hint in RETRYABLE_HINTS)


def _delay(attempt: int, base_delay: float, exc: BaseException) -> float:
    delay = min(RETRY_MAX_DELAY, base_delay * (2 ** (attempt - 1)))
    requested = getattr(exc, "retry_after", None)
    numeric = isinstance(requested, float) or isinstance(requested, int)
    if numeric and requested > 0:
        # The provider's instruction is a floor, not a suggestion: jitter downward
        # below `Retry-After` would ignore it and retry too early.
        return max(float(requested), delay * random.uniform(0.8, 1.2))
    # Jitter keeps a four-agent burst from retrying in lockstep.
    return delay * random.uniform(0.8, 1.2)


async def with_retries(factory, attempts: int = RETRY_ATTEMPTS, base_delay: float = RETRY_BASE_DELAY, on_retry=None):
    """Await ``factory()``, retrying transient failures with exponential backoff."""
    attempts = max(1, attempts)
    for attempt in range(1, attempts + 1):
        try:
            return await factory()
        except Exception as exc:  # noqa: BLE001 - classification decides what re-raises
            if attempt >= attempts or not is_retryable(exc):
                raise
            delay = _delay(attempt, base_delay, exc)
            if on_retry is not None:
                result = on_retry(attempt, exc, delay)
                if asyncio.iscoroutine(result):
                    await result
            await asyncio.sleep(delay)
    raise RuntimeError("retry loop exited without a result")  # pragma: no cover
