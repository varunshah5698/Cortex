"""Adapter fallbacks: an optional structured-output field must not fail a run.

Ollama's MLX runner rejects JSON mode with 501 "structured output is unavailable".
The adapters ask once with the field, then retry without it; these tests pin that
behaviour for both wire protocols without touching a provider.
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_harness.models import Specialist  # noqa: E402
from ai_harness.modules import adapters  # noqa: E402
from ai_harness.modules.usage import StreamStats  # noqa: E402


class _Response:
    def __init__(self, status_code, lines=(), body=b""):
        self.status_code = status_code
        self._lines = list(lines)
        self._body = body

    async def aread(self):
        return self._body

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _Client:
    """Stands in for the pooled httpx client; records each request body."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.bodies = []

    def stream(self, method, url, headers=None, json=None):
        self.bodies.append(dict(json))  # snapshot: the adapter mutates the retry body
        return self._responses.pop(0)


def _spec(kind):
    return Specialist(
        id="scout",
        name="Scout",
        role="planner",
        kind=kind,
        baseUrl="http://127.0.0.1:11434/v1",
        model="gemma4:e4b",
    )


def test_hosted_concurrency_is_per_model_and_off_by_default(monkeypatch):
    hosted = Specialist(
        id="lyra",
        name="Lyra",
        role="reviewer",
        kind="custom",
        baseUrl="https://api.river.ai/v1",
        model="deepseek-ai/DeepSeek-V4.1-Flash",
    )
    monkeypatch.setattr(adapters, "PROVIDER_CONCURRENCY", 0)
    assert adapters._request_slot(hosted) is None

    monkeypatch.setattr(adapters, "PROVIDER_CONCURRENCY", 1)
    assert adapters._request_slot(hosted) is not None
    assert adapters._request_slot(hosted) is adapters._request_slot(hosted)
    assert adapters._request_slot(_spec("ollama")) is not None


def test_ollama_retries_without_format_when_structured_output_is_unavailable(monkeypatch):
    fake = _Client(
        [
            _Response(501, body=b'{"error":"structured output is unavailable"}'),
            _Response(
                200,
                lines=[
                    json.dumps({"message": {"content": "{}"}}),
                    json.dumps({"done": True}),
                ],
            ),
        ]
    )
    monkeypatch.setattr(adapters, "client", lambda: fake)

    stats = StreamStats()

    async def run():
        return [
            chunk
            async for chunk in adapters.stream(
                _spec("ollama"),
                [{"role": "user", "content": "hi"}],
                json_mode=True,
                stats=stats,
            )
        ]

    assert asyncio.run(run()) == [("token", "{}")]
    assert len(fake.bodies) == 2
    assert fake.bodies[0]["format"] == "json"
    assert "format" not in fake.bodies[1]


def test_openai_retries_without_response_format_when_structured_output_is_unavailable(monkeypatch):
    monkeypatch.setattr(adapters, "JSON_MODE", True)
    fake = _Client(
        [
            _Response(501, body=b'{"error":{"message":"structured output is unavailable"}}'),
            _Response(
                200,
                lines=[
                    'data: {"choices":[{"delta":{"content":"{}"}}]}',
                    "data: [DONE]",
                ],
            ),
        ]
    )
    monkeypatch.setattr(adapters, "client", lambda: fake)

    stats = StreamStats()

    async def run():
        return [
            chunk
            async for chunk in adapters.stream(
                _spec("openai"),
                [{"role": "user", "content": "hi"}],
                json_mode=True,
                stats=stats,
            )
        ]

    assert asyncio.run(run()) == [("token", "{}")]
    assert len(fake.bodies) == 2
    assert fake.bodies[0]["response_format"] == {"type": "json_object"}
    assert "response_format" not in fake.bodies[1]
