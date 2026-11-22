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

from ..config.settings import (
    CACHE_ENABLED,
    FALLBACK_BASE_URL,
    FALLBACK_KIND,
    FALLBACK_MODEL,
    STREAM_SECONDS,
    TEMPERATURE,
)
from ..models import Specialist
from ..utils.logger import event_log
from . import budget as pricing
from . import roulette, runtime
from .adapters import ProviderError, aclose, protocol, reachable, warm
from .adapters import openai_tool_call as _adapter_tool_call
from .adapters import stream as _adapter_stream
from .cache import CACHE
from .retry import with_retries
from .usage import StreamStats, Usage, estimate_tokens

__all__ = [
    "CACHE_ENABLED",
    "NativeToolsUnsupported",
    "ProviderError",
    "StreamStats",
    "TEMPERATURE",
    "Usage",
    "aclose_client",
    "cacheable",
    "call",
    "call_tools",
    "reachable",
    "stream",
    "supports_native_tools",
    "warm",
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


class NativeToolsUnsupported(RuntimeError):
    """This provider does not accept native tools; the loop keeps its text protocol."""


def supports_native_tools(spec: Specialist) -> bool:
    """Only the OpenAI-compatible wire format has a native tool call in this harness.

    Anthropic and Google have their own shapes, and Ollama's native endpoint takes a
    different one; they keep the text protocol rather than growing three more code paths
    for an optimisation.
    """
    return protocol(spec) == "openai"


async def call_tools(
    spec: Specialist,
    messages: list[dict[str, str]],
    tools: list[dict],
    temperature: float | None = None,
) -> tuple[dict | None, str, Usage]:
    """One native tool-call turn, through the same budget, deadline and retry policy."""
    runtime.check_deadline()
    budget = runtime.current_budget()
    if budget is not None:
        budget.check()
    if not supports_native_tools(spec):
        raise NativeToolsUnsupported(f"{spec.name} does not expose the OpenAI tool-call shape")
    # The same draw as `stream`: a native-tool loop is still a loop of provider calls, and
    # a roulette that only applied to the text protocol would be a feature that silently
    # switched itself off for the providers that support tools.
    spec = _draw(spec)
    effective = TEMPERATURE if temperature is None else temperature
    started = time.perf_counter()

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
        call, text, usage = await with_retries(
            lambda: _adapter_tool_call(spec, messages, tools, effective),
            on_retry=on_retry,
        )
    except ProviderError as exc:
        # A provider that does not implement `tools` answers 4xx. That is not a run
        # failure; it is a signal to use the text protocol for the rest of the loop.
        if exc.status in (400, 404, 422, 501):
            raise NativeToolsUnsupported(str(exc)) from exc
        raise

    if not usage.prompt_tokens:
        usage.estimated = True
        usage.prompt_tokens = sum(estimate_tokens(str(m.get("content", ""))) for m in messages)
    usage.ms += int((time.perf_counter() - started) * 1000)
    usage.cost_usd = pricing.estimate(spec, usage.prompt_tokens, usage.completion_tokens)
    if budget is not None:
        budget.spend(usage.prompt_tokens + usage.completion_tokens, usage.cost_usd)
    return call, text, usage


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

    # Roulette is applied here, at the one place every provider call passes through, so
    # no node has to know it exists. With the feature off this is the same object back.
    spec = _draw(spec)

    started = time.perf_counter()
    stats.usage.calls += 1
    # A deadline before a call stops work that has not started. This one stops work that
    # has: a stream that stalls, or a model that will not stop thinking, used to hold the
    # run open until the wall-clock limit — one review call ran for 46 minutes and wrote
    # 8,000 events while the room looked alive. The bound is per call, so a slow-but-
    # working provider still gets its time; only a call that outlives `STREAM_SECONDS` is
    # cut, and it is cut with a ProviderError the nodes already handle.
    deadline = started + STREAM_SECONDS if STREAM_SECONDS else None
    cut = False
    chunks = _adapter_stream(
        spec,
        messages,
        think=think,
        temperature=TEMPERATURE if temperature is None else temperature,
        json_mode=json_mode,
        stats=stats,
    ).__aiter__()

    async def cut_stream() -> None:
        """Report a cut stream once, and close the provider connection."""
        nonlocal cut
        cut = True
        stats.truncated = True
        await runtime.emit(
            {
                "type": "stream-cut",
                "agent": spec.id,
                "model": spec.model,
                "seconds": round(time.perf_counter() - started, 1),
                "message": (
                    f"{spec.name}'s call ran past its {STREAM_SECONDS:.0f}s bound and was cut; "
                    "the text is a prefix, not a complete answer"
                ),
            }
        )
        try:
            await chunks.aclose()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - a closed or already-running generator
            pass

    try:
        # The bound must cover a stream that sends nothing at all, not only a chatty one.
        # Iterating with a per-chunk timeout is what makes the deadline apply to a stalled
        # connection; the in-loop check alone only fires when a chunk finally arrives.
        while True:
            try:
                if deadline is None:
                    kind, text = await chunks.__anext__()
                else:
                    remaining = deadline - time.perf_counter()
                    if remaining <= 0:
                        raise asyncio.TimeoutError
                    kind, text = await asyncio.wait_for(chunks.__anext__(), timeout=remaining)
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                await cut_stream()
                break
            if deadline is not None and time.perf_counter() > deadline:
                await cut_stream()
                break
            yield (kind, text)

    finally:
        # Settlement belongs in `finally`, not after the loop. A caller that breaks out of
        # the stream early — `_bounded_stream` does exactly that when a model will not stop
        # reasoning — closes this generator before any code after the loop ever runs. The
        # tokens were already spent at the provider; charging them only on a clean
        # exhaustion under-counted every reasoning and critique call, and a run could then
        # exceed its own cap by however much it had been cut short.
        await _settle(spec, stats, started, budget, messages, cut)


async def _settle(
    spec: Specialist,
    stats: StreamStats,
    started: float,
    budget,  # noqa: ANN001
    messages: list[dict[str, str]],
    cut: bool = False,
) -> None:
    """Price a finished (or abandoned) stream and charge it to the budget.

    Called from `stream`'s `finally`, so it runs whether the caller exhausted the stream,
    broke out of it, or the provider raised. Charging only on clean exhaustion lost the
    spend for every call a bounded consumer cut short.
    """
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
    stats.usage.truncated = stats.truncated
    if stats.truncated and not cut:
        # Say it once, where the spend is recorded. A truncated generation that nobody
        # mentions is the failure mode this flag exists to end: the file looks written.
        # A cut stream already said so above; the token-ceiling wording would be a lie.
        await runtime.emit(
            {
                "type": "truncated",
                "agent": spec.id,
                "model": spec.model,
                "message": (
                    f"{spec.name}'s output hit the provider's token ceiling; the text is a prefix, "
                    "not a complete answer"
                ),
            }
        )


def _draw(spec: Specialist) -> Specialist:
    """The specialist for this call, after roulette. Identity when the feature is off."""
    if not roulette.enabled():
        return spec
    run = runtime.current_run()
    roster = list(getattr(run, "specialists", None) or [])
    chosen = roulette.pick(spec, roster, str(getattr(run, "run_id", "") or ""))
    if chosen is spec:
        return spec
    # The event is fire-and-forget: a draw must never fail a call that would otherwise
    # succeed, and the transcript is already the place this belongs.
    event = roulette.record(chosen, spec)
    if event is not None:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(runtime.emit(event))
        except RuntimeError:  # pragma: no cover - no loop means no run to report to
            pass
    return roulette.replace(spec, chosen)


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
