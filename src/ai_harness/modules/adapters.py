"""Provider adapters: one place that knows each provider's wire protocol.

Each protocol translates the same two inputs — messages and a JSON-mode flag — into its
own request and translates the response stream back into `("think" | "token", text)`
chunks. The harness's agent loop writes files; it does not call tools, so no tool-call
plumbing lives here.

Also here: one pooled HTTP client (no TLS handshake per call), a per-(provider, model)
token bucket so a run cannot hammer a provider, and `Retry-After` propagation so the
retry policy can respect what the provider asked for.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator

import httpx

from ..config.settings import (
    HTTP_TIMEOUT_READ,
    JSON_MODE,
    MAX_OUTPUT_TOKENS,
    PROVIDER_CONCURRENCY,
    PROVIDER_RATE_LIMIT,
)
from ..models import Specialist
from ..utils.netguard import UnsafeEndpoint, ensure_allowed
from .usage import StreamStats, Usage

TIMEOUT = httpx.Timeout(connect=20.0, read=HTTP_TIMEOUT_READ, write=60.0, pool=20.0)


class ProviderError(RuntimeError):
    """A provider call failed. `status` and `retry_after` drive the retry policy."""

    def __init__(self, message: str, *, status: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


# ---------- shared client ----------

_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    """One pooled client for the process; connections survive between calls."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=TIMEOUT,
            limits=httpx.Limits(max_connections=24, max_keepalive_connections=12, keepalive_expiry=30.0),
            follow_redirects=False,
            headers={"user-agent": "cortex-harness/0.4"},
        )
    return _client


async def aclose() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


# ---------- rate limiting ----------


class _Bucket:
    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self.tokens = float(per_minute)
        self.updated = time.monotonic()

    async def take(self) -> None:
        if self.per_minute <= 0:
            return
        while True:
            now = time.monotonic()
            self.tokens = min(self.per_minute, self.tokens + (now - self.updated) * self.per_minute / 60.0)
            self.updated = now
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return
            await asyncio.sleep(min(5.0, (1.0 - self.tokens) * 60.0 / self.per_minute))


_buckets: dict[tuple[str, str], _Bucket] = {}
# A local daemon runs one generation at a time. Four contract calls fired at once used to
# queue inside Ollama (and could take the daemon down with them); this lock makes the
# harness wait its turn instead.
_local_locks: dict[tuple[str, str], asyncio.Lock] = {}
# Hosted gateways can cap concurrency per account+model. `HARNESS_PROVIDER_CONCURRENCY`
# sets how many requests one model may have in flight; 0 leaves it to the provider.
_slots: dict[tuple[str, str], asyncio.Semaphore] = {}


class _UnboundSlot:
    """A no-op slot for a synchronous caller with no running loop.

    `asyncio.Semaphore` binds to the loop that exists when it is constructed. On 3.9,
    constructing one outside a loop (or after `asyncio.run` closed the last one) raises.
    There is no request to gate in that context; the real async path still gets a
    semaphore, and a test that only inspects the policy still sees a non-None answer.
    """

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


_UNBOUND_SLOT = _UnboundSlot()


async def _throttle(spec: Specialist) -> None:
    key = ((spec.baseUrl or "").rstrip("/"), spec.model or "")
    bucket = _buckets.get(key)
    if bucket is None:
        bucket = _buckets[key] = _Bucket(PROVIDER_RATE_LIMIT)
    await bucket.take()


def _request_slot(spec: Specialist) -> asyncio.Lock | asyncio.Semaphore | _UnboundSlot | None:
    """The concurrency limit for one endpoint+model, or None for the provider's own.

    A local daemon is always one at a time. A hosted gateway uses the configured
    semaphore, because a 429 is cheaper to prevent than to retry.
    """
    key = ((spec.baseUrl or "").rstrip("/"), spec.model or "")
    if spec.is_local:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return _UNBOUND_SLOT
        lock = _local_locks.get(key)
        if lock is None:
            lock = _local_locks[key] = asyncio.Lock()
        return lock
    if PROVIDER_CONCURRENCY <= 0:
        return None
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _UNBOUND_SLOT
    slot = _slots.get(key)
    if slot is None:
        slot = _slots[key] = asyncio.Semaphore(PROVIDER_CONCURRENCY)
    return slot


# ---------- shared helpers ----------


def _base(spec: Specialist) -> str:
    return (spec.baseUrl or "").strip().rstrip("/")


def _messages(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    return [{"role": str(m.get("role", "user")), "content": str(m.get("content", ""))} for m in messages]


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after", "").strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


async def _raise_for(response: httpx.Response, spec: Specialist) -> None:
    detail = (await response.aread()).decode("utf-8", "ignore")[:400]
    raise ProviderError(
        f"{spec.name}: HTTP {response.status_code} {detail}".strip(),
        status=response.status_code,
        retry_after=_retry_after(response),
    )


async def _sse(response: httpx.Response) -> AsyncIterator[dict]:
    """Yield the JSON payload of each `data:` frame, ignoring comments and heartbeats."""
    async for raw in response.aiter_lines():
        line = raw.strip()
        if not line or line.startswith(":") or not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event


# ---------- OpenAI-compatible ----------


async def _openai(
    spec: Specialist,
    messages: list[dict[str, str]],
    think: bool,
    temperature: float,
    json_mode: bool,
    stats: StreamStats,
) -> AsyncIterator[tuple[str, str]]:
    url = f"{_base(spec)}/chat/completions"
    body: dict = {
        "model": spec.model,
        "messages": _messages(messages),
        "stream": True,
        "temperature": temperature,
        "stream_options": {"include_usage": True},
    }
    if json_mode and JSON_MODE:
        body["response_format"] = {"type": "json_object"}

    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if spec.apiKey:
        headers["Authorization"] = f"Bearer {spec.apiKey}"
        headers["x-api-key"] = spec.apiKey

    # Some OpenAI-compatible servers reject `response_format`. Ask once with it, then
    # once without, rather than failing the run over an optional field. 501 is Ollama's
    # MLX runner saying "structured output is unavailable"; 400/422 are the usual
    # OpenAI-compatible deployments that never implemented the field.
    for attempt in range(2 if "response_format" in body else 1):
        async with client().stream("POST", url, headers=headers, json=body) as response:
            if response.status_code >= 400:
                if attempt == 0 and "response_format" in body and response.status_code in (400, 422, 501):
                    await response.aread()
                    body.pop("response_format", None)
                    continue
                await _raise_for(response, spec)
            async for event in _sse(response):
                if event.get("error"):
                    raise ProviderError(f"{spec.name}: {event['error']}")
                usage = event.get("usage")
                if usage:
                    stats.usage.prompt_tokens = int(usage.get("prompt_tokens") or 0)
                    stats.usage.completion_tokens = int(usage.get("completion_tokens") or 0)
                delta = ((event.get("choices") or [{}])[0]).get("delta") or {}
                reasoning = delta.get("reasoning_content") or delta.get("reasoning") or delta.get("thinking")
                if reasoning:
                    stats.thinking.append(str(reasoning))
                    yield ("think", str(reasoning))
                content = delta.get("content")
                if content:
                    stats.content.append(str(content))
                    yield ("token", str(content))
            return


# ---------- Anthropic native ----------


async def _anthropic(
    spec: Specialist,
    messages: list[dict[str, str]],
    think: bool,
    temperature: float,
    json_mode: bool,
    stats: StreamStats,
) -> AsyncIterator[tuple[str, str]]:
    system = "\n\n".join(str(m.get("content", "")) for m in messages if m.get("role") == "system")
    if json_mode:
        system = (system + "\n\nReply with a single valid JSON object and nothing else.").strip()
    turns = [
        {"role": m["role"], "content": str(m.get("content", ""))}
        for m in messages
        if m.get("role") in ("user", "assistant") and str(m.get("content", "")).strip()
    ]
    body: dict = {
        "model": spec.model,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "messages": turns or [{"role": "user", "content": "Continue."}],
        "stream": True,
        "temperature": temperature,
    }
    if system:
        body["system"] = system

    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "anthropic-version": "2023-06-01",
    }
    if spec.apiKey:
        headers["x-api-key"] = spec.apiKey
        headers["Authorization"] = f"Bearer {spec.apiKey}"

    async with client().stream("POST", f"{_base(spec)}/messages", headers=headers, json=body) as response:
        if response.status_code >= 400:
            await _raise_for(response, spec)
        async for event in _sse(response):
            kind = event.get("type")
            if kind == "error":
                error = event.get("error") or {}
                raise ProviderError(f"{spec.name}: {error.get('message', 'stream error')}")
            if kind == "message_start":
                usage = (event.get("message") or {}).get("usage") or {}
                stats.usage.prompt_tokens = int(usage.get("input_tokens") or 0)
            elif kind == "content_block_delta":
                delta = event.get("delta") or {}
                if delta.get("type") == "text_delta" and delta.get("text"):
                    stats.content.append(str(delta["text"]))
                    yield ("token", str(delta["text"]))
                elif delta.get("type") == "thinking_delta" and delta.get("thinking"):
                    stats.thinking.append(str(delta["thinking"]))
                    yield ("think", str(delta["thinking"]))
            elif kind == "message_delta":
                usage = event.get("usage") or {}
                if usage:
                    stats.usage.completion_tokens = int(usage.get("output_tokens") or 0)
            elif kind == "message_stop":
                return


# ---------- Google Gemini native ----------


async def _google(
    spec: Specialist,
    messages: list[dict[str, str]],
    think: bool,
    temperature: float,
    json_mode: bool,
    stats: StreamStats,
) -> AsyncIterator[tuple[str, str]]:
    url = f"{_base(spec)}/models/{spec.model}:streamGenerateContent?alt=sse"
    system = "\n\n".join(str(m.get("content", "")) for m in messages if m.get("role") == "system")
    contents = [
        {
            "role": "model" if m.get("role") == "assistant" else "user",
            "parts": [{"text": str(m.get("content", ""))}],
        }
        for m in messages
        if m.get("role") in ("user", "assistant") and str(m.get("content", "")).strip()
    ]
    generation: dict = {"temperature": temperature, "maxOutputTokens": MAX_OUTPUT_TOKENS}
    if json_mode:
        generation["responseMimeType"] = "application/json"
    body: dict = {
        "contents": contents or [{"role": "user", "parts": [{"text": "Continue."}]}],
        "generationConfig": generation,
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}

    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if spec.apiKey:
        headers["x-goog-api-key"] = spec.apiKey

    async with client().stream("POST", url, headers=headers, json=body) as response:
        if response.status_code >= 400:
            await _raise_for(response, spec)
        async for event in _sse(response):
            if event.get("error"):
                raise ProviderError(f"{spec.name}: {event['error']}")
            meta = event.get("usageMetadata") or {}
            if meta:
                stats.usage.prompt_tokens = int(meta.get("promptTokenCount") or stats.usage.prompt_tokens)
                stats.usage.completion_tokens = int(meta.get("candidatesTokenCount") or stats.usage.completion_tokens)
            for candidate in event.get("candidates") or []:
                for part in (candidate.get("content") or {}).get("parts") or []:
                    text = part.get("text")
                    if text:
                        stats.content.append(str(text))
                        yield ("token", str(text))


# ---------- Ollama native ----------


def _native_ollama_url(base: str) -> str:
    clean = base.rstrip("/")
    if clean.endswith("/v1"):
        clean = clean[:-3]
    return f"{clean}/api/chat"


async def _ollama(
    spec: Specialist,
    messages: list[dict[str, str]],
    think: bool,
    temperature: float,
    json_mode: bool,
    stats: StreamStats,
) -> AsyncIterator[tuple[str, str]]:
    body: dict = {
        "model": spec.model,
        "messages": _messages(messages),
        "stream": True,
        "think": bool(think),
        # A multi-file build calls the same model for minutes; do not pay a reload between
        # calls. One hour is longer than any bounded run.
        "keep_alive": "1h",
        "options": {"temperature": temperature, "num_predict": MAX_OUTPUT_TOKENS},
    }
    if json_mode:
        body["format"] = "json"
    headers = {"Content-Type": "application/json", "Accept": "application/x-ndjson"}

    # Ollama's MLX runner answers 501 "structured output is unavailable" when asked for
    # `format`. JSON mode is an optimisation, not a requirement, so ask once with it and
    # once without instead of failing a supported local provider over an optional field.
    for attempt in range(2 if "format" in body else 1):
        async with client().stream("POST", _native_ollama_url(_base(spec)), headers=headers, json=body) as response:
            if response.status_code >= 400:
                if attempt == 0 and "format" in body and response.status_code in (400, 422, 501):
                    await response.aread()
                    body.pop("format", None)
                    continue
                await _raise_for(response, spec)
            async for raw in response.aiter_lines():
                line = raw.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("error"):
                    raise ProviderError(f"{spec.name}: {event['error']}")
                message = event.get("message") or {}
                if message.get("thinking"):
                    stats.thinking.append(str(message["thinking"]))
                    yield ("think", str(message["thinking"]))
                if message.get("content"):
                    stats.content.append(str(message["content"]))
                    yield ("token", str(message["content"]))
                if event.get("done"):
                    if event.get("prompt_eval_count") is not None:
                        stats.usage.prompt_tokens = int(event.get("prompt_eval_count") or 0)
                        stats.usage.completion_tokens = int(event.get("eval_count") or 0)
            return


# ---------- dispatch ----------

HANDLERS = {
    "anthropic": _anthropic,
    "google": _google,
    "ollama": _ollama,
    "openai": _openai,
}


def protocol(spec: Specialist) -> str:
    """Which wire protocol this specialist speaks."""
    kind = (spec.kind or "").lower()
    if kind == "anthropic":
        return "anthropic"
    if kind in ("google", "gemini"):
        return "google"
    if kind == "ollama":
        return "ollama"
    return "openai"


async def reachable(spec: Specialist) -> tuple[bool, str]:
    """Can this provider answer right now? A local daemon that is down is not a retry."""
    base = _base(spec)
    if not base or not spec.model:
        return False, "no base URL or model"
    if not spec.is_local:
        return True, ""
    url = _native_ollama_url(base).replace("/api/chat", "/api/tags") if protocol(spec) == "ollama" else f"{base}/models"
    try:
        response = await client().get(url, timeout=3.0)
        return response.status_code < 500, f"HTTP {response.status_code}"
    except httpx.HTTPError as exc:
        return False, type(exc).__name__


async def warm(spec: Specialist, timeout: float = 180.0) -> tuple[bool, str]:
    """Load a local model before the run needs it, so the first file is not the wait.

    Only native Ollama exposes a load endpoint; an OpenAI-compatible local server either
    loads on the first request or is fast enough that a preflight would be noise.
    """
    if protocol(spec) != "ollama":
        return True, ""
    url = _native_ollama_url(_base(spec)).replace("/api/chat", "/api/generate")
    body = {
        "model": spec.model,
        "prompt": "ok",
        "stream": False,
        "keep_alive": "1h",
        "options": {"num_predict": 1},
    }
    try:
        response = await client().post(url, json=body, timeout=timeout)
        return response.status_code < 400, f"HTTP {response.status_code}"
    except httpx.HTTPError as exc:
        return False, type(exc).__name__


async def stream(
    spec: Specialist,
    messages: list[dict[str, str]],
    *,
    think: bool = True,
    temperature: float = 0.35,
    json_mode: bool = False,
    stats: StreamStats,
) -> AsyncIterator[tuple[str, str]]:
    """Yield ("think" | "token", text); usage lands in `stats`."""
    base = _base(spec)
    if not base or not spec.model:
        raise ProviderError(f"{spec.name} has no base URL or model")
    try:
        await ensure_allowed(base)
    except UnsafeEndpoint as exc:
        raise ProviderError(f"{spec.name}: refusing {base} — {exc}") from exc
    await _throttle(spec)

    handler = HANDLERS[protocol(spec)]

    async def produce() -> AsyncIterator[tuple[str, str]]:
        async for item in handler(spec, messages, think, temperature, json_mode, stats):
            yield item

    slot = _request_slot(spec)
    try:
        if slot is None:
            async for item in produce():
                yield item
        else:
            async with slot:
                async for item in produce():
                    yield item
    except httpx.HTTPError as exc:
        # A read timeout mid-stream used to reach the run as a bare `ReadTimeout:` with
        # no agent and no endpoint; name both so the UI says what actually failed.
        raise ProviderError(f"{spec.name}: {type(exc).__name__} from {base}") from exc


async def openai_tool_call(
    spec: Specialist,
    messages: list[dict[str, str]],
    tools: list[dict],
    temperature: float,
) -> tuple[dict | None, str, Usage]:
    """One non-streaming native tool call on an OpenAI-compatible endpoint.

    Native tools are an optimisation, not a requirement: the loop still speaks the text
    protocol. A provider that rejects the `tools` field raises a 4xx and the caller
    falls back for the rest of the loop.
    """
    base = _base(spec)
    if not base or not spec.model:
        raise ProviderError(f"{spec.name} has no base URL or model")
    try:
        await ensure_allowed(base)
    except UnsafeEndpoint as exc:
        raise ProviderError(f"{spec.name}: refusing {base} — {exc}") from exc
    await _throttle(spec)
    body: dict = {
        "model": spec.model,
        "messages": _messages(messages),
        "tools": tools,
        "tool_choice": "auto",
        "temperature": temperature,
        "stream": False,
    }
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if spec.apiKey:
        headers["Authorization"] = f"Bearer {spec.apiKey}"
        headers["x-api-key"] = spec.apiKey

    slot = _request_slot(spec)
    try:
        if slot is None:
            response = await client().post(f"{base}/chat/completions", headers=headers, json=body)
        else:
            async with slot:
                response = await client().post(f"{base}/chat/completions", headers=headers, json=body)
    except httpx.HTTPError as exc:
        raise ProviderError(f"{spec.name}: {type(exc).__name__} from {base}") from exc
    if response.status_code >= 400:
        await _raise_for(response, spec)
    try:
        payload = response.json()
    except ValueError as exc:
        raise ProviderError(f"{spec.name}: the tool-call response was not JSON") from exc

    usage = payload.get("usage") or {}
    stats = Usage(
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        calls=1,
    )
    choice = (payload.get("choices") or [{}])[0] or {}
    message = choice.get("message") or {}
    content = str(message.get("content") or "").strip()
    calls = message.get("tool_calls") or []
    if not calls:
        return None, content, stats
    function = (calls[0] or {}).get("function") or {}
    name = str(function.get("name") or "").strip()
    raw_args = function.get("arguments") or "{}"
    try:
        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
    except json.JSONDecodeError:
        args = {"_raw": raw_args}
    if not isinstance(args, dict):
        args = {}
    return {"tool": name, "args": args, "thought": content}, content, stats
