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

from ..config.settings import RETRY_ATTEMPTS, RETRY_BASE_DELAY, RETRY_MAX_DELAY
from .budget import BudgetExceeded

# 408 timeout, 409/425 conflict/too-early, 429 rate limit, 5xx weather, 529 overloaded.
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}

# No status available: transport failures, and providers that stream an error event.
RETRYABLE_HINTS = ("timeout", "timed out", "connect", "reset", "overloaded", "temporarily", "unavailable")

FATAL_HINTS = ("invalid api key", "no base url", "refusing", "not found")


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, BudgetExceeded):
        # A budget stop must never be retried, whatever its message happens to say.
        return False
    status = getattr(exc, "status", None)
    if status is not None:
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
        delay = max(delay, min(RETRY_MAX_DELAY, float(requested)))
    return delay


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
