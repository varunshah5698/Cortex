"""Provider-agnostic streaming client: policy around `modules/adapters.py`.

Every node talks to this module, not to a provider. It validates the endpoint, enforces
the budget, prices the call, replays an identical deterministic request from the cache
when asked, and retries only what is retryable.

`TEMPERATURE` and `CACHE_ENABLED` are module globals on purpose: the tests and an
operator patch them, and `cacheable()` reads them at call time rather than freezing
them into a default argument.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
from collections.abc import AsyncIterator

from ..config.settings import CACHE_ENABLED, FALLBACK_BASE_URL, FALLBACK_KIND, FALLBACK_MODEL, TEMPERATURE
from ..models import Specialist
from ..utils.logger import event_log
from . import budget as pricing
from . import runtime
from .adapters import ProviderError, aclose
from .adapters import stream as _adapter_stream
from .cache import CACHE
from .retry import with_retries
from .usage import StreamStats, Usage, estimate_tokens

__all__ = [
    "CACHE_ENABLED",
    "ProviderError",
    "StreamStats",
    "TEMPERATURE",
    "Usage",
    "aclose_client",
    "cacheable",
    "call",
    "stream",
]


def fallback_for(spec: Specialist) -> Specialist | None:
    """The configured fallback model as a specialist, or None when it would be the same model."""
    if not FALLBACK_MODEL or FALLBACK_MODEL == spec.model:
        return None
    return dataclasses.replace(
        spec,
        model=FALLBACK_MODEL,
        baseUrl=FALLBACK_BASE_URL or spec.baseUrl,
        kind=FALLBACK_KIND or spec.kind,
    )


def cacheable() -> bool:
    """Only a deterministic (temperature 0) request may ever be replayed."""
    return CACHE_ENABLED and TEMPERATURE == 0


async def stream(
    spec: Specialist,
    messages: list[dict[str, str]],
    think: bool = True,
    stats: StreamStats | None = None,
    json_mode: bool = False,
    temperature: float | None = None,
) -> AsyncIterator[tuple[str, str]]:
    """Yield ("think" | "token", text) and record usage into `stats`."""
    stats = stats or StreamStats()
    # Time and money are the same kind of bound, so they are checked in the same place:
    # before the call, never after it.
    runtime.check_deadline()
    budget = runtime.current_budget()
    if budget is not None:
        budget.check()

    started = time.perf_counter()
    stats.usage.calls += 1
    async for kind, text in _adapter_stream(
        spec,
        messages,
        think=think,
        temperature=TEMPERATURE if temperature is None else temperature,
        json_mode=json_mode,
        stats=stats,
    ):
        yield (kind, text)

    # A provider that omits a count gets an estimate, and the ledger says so.
    if not stats.usage.prompt_tokens:
        stats.usage.estimated = True
        stats.usage.prompt_tokens = sum(estimate_tokens(str(m.get("content", ""))) for m in messages)
    if not stats.usage.completion_tokens:
        stats.usage.estimated = True
        stats.usage.completion_tokens = estimate_tokens(stats.text + "".join(stats.thinking))

    stats.usage.ms += int((time.perf_counter() - started) * 1000)
    stats.usage.thinking_chars += len("".join(stats.thinking))
    stats.usage.cost_usd = pricing.estimate(spec, stats.usage.prompt_tokens, stats.usage.completion_tokens)
    if budget is not None:
        budget.spend(stats.usage.prompt_tokens + stats.usage.completion_tokens, stats.usage.cost_usd)


async def call(
    spec: Specialist,
    messages: list[dict[str, str]],
    think: bool = False,
    on_think=None,
    json_mode: bool = False,
    temperature: float | None = None,
) -> tuple[str, Usage]:
    """Collect a full text response, through the cache and the retry policy."""
    # Before the cache, because a hit returns without ever reaching `stream`'s check.
    runtime.check_deadline()
    effective = TEMPERATURE if temperature is None else temperature
    if cacheable():
        hit = CACHE.get(spec, messages, effective, json_mode)
        if hit is not None:
            await runtime.emit(
                {
                    "type": "message",
                    "from": spec.id,
                    "text": f"cache hit · {spec.model} · reused an identical request, no provider call",
                    "kind": "cache",
                }
            )
            return hit["text"], Usage(cached=1)

    async def attempt(target: Specialist) -> tuple[str, Usage]:
        stats = StreamStats()
        async for kind, text in stream(
            target,
            messages,
            think=think,
            stats=stats,
            json_mode=json_mode,
            temperature=effective,
        ):
            if kind == "think" and on_think is not None:
                result = on_think(text)
                if asyncio.iscoroutine(result):
                    await result
        return "".join(stats.content).strip(), stats.usage

    async def collect() -> tuple[str, Usage]:
        return await attempt(spec)

    async def on_retry(attempt: int, exc: BaseException, delay: float) -> None:
        run = runtime.current_run()
        event_log(
            getattr(run, "run_id", "anonymous"),
            "retry",
            agent=spec.id,
            attempt=attempt,
            delay_s=delay,
            error=str(exc)[:200],
        )
        await runtime.emit(
            {
                "type": "retry",
                "agent": spec.id,
                "attempt": attempt,
                "delay_ms": int(delay * 1000),
                "message": str(exc)[:200],
            }
        )

    try:
        text, usage = await with_retries(collect, on_retry=on_retry)
    except ProviderError as exc:
        # Retries are exhausted. A configured fallback is one more model, not one more
        # attempt at the same outage: the run continues instead of returning empty.
        target = fallback_for(spec)
        if target is None:
            raise
        await runtime.emit(
            {
                "type": "fallback",
                "agent": spec.id,
                "from": spec.model,
                "to": target.model,
                "message": f"{spec.model} failed after retries ({str(exc)[:160]}); continuing on {target.model}",
            }
        )
        event_log(
            getattr(runtime.current_run(), "run_id", "anonymous"),
            "fallback",
            agent=spec.id,
            **{"from": spec.model, "to": target.model},
        )
        text, usage = await with_retries(lambda: attempt(target), on_retry=on_retry)
    if cacheable() and text:
        CACHE.set(spec, messages, effective, {"text": text, "usage": usage}, json_mode)
    return text, usage


async def aclose_client() -> None:
    """Release the pooled HTTP connections (called on shutdown)."""
    await aclose()
