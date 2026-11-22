"""Testing a provider must check the model, not just that the endpoint answered.

A row naming `eepseek-ai/DeepSeek-V4.1-Flash` (missing the leading `d`) passed "Test
connection" because the test only asked whether `GET /models` returned 200. The first real
call then failed with `model_not_found`, which reads as the provider being broken when the
row is what was wrong. These pin the check that closes that gap.

No network: `httpx.AsyncClient` is replaced with a stub that serves a fixed model list.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_harness import main as harness_main  # noqa: E402

SERVED = [
    "GLM-5.3",
    "Qwen/Qwen3.5-9B",
    "deepseek-ai/DeepSeek-V4.1-Flash",
    "deepseek-ai/DeepSeek-V4.1-Flash-1017",
    "moonshotai/Kimi-K3",
]


class _Response:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _Client:
    def __init__(self, status_code: int = 200, payload: dict | None = None) -> None:
        self._status = status_code
        self._payload = payload if payload is not None else {"data": [{"id": name} for name in SERVED]}

    async def __aenter__(self) -> _Client:
        return self

    async def __aexit__(self, *args) -> None:  # noqa: ANN002
        return None

    async def get(self, url: str, headers: dict | None = None) -> _Response:  # noqa: ANN001
        return _Response(self._status, self._payload)


def _patch(monkeypatch, **kwargs) -> None:
    monkeypatch.setattr(harness_main.httpx, "AsyncClient", lambda **_: _Client(**kwargs))


def _call(model: str) -> dict:
    payload = harness_main.ProviderCheck(id="orion", baseUrl="https://api.river.ai/v1", kind="custom", model=model)
    response = asyncio.run(harness_main.providers_test(payload))
    return json.loads(response.body)


def test_a_typoed_model_is_refused_with_the_closest_match(monkeypatch) -> None:
    """The exact failure that reached the user: reachable endpoint, wrong model name."""
    _patch(monkeypatch)

    result = _call("eepseek-ai/DeepSeek-V4.1-Flash")

    assert result["ok"] is False
    assert "does not serve model" in result["detail"]
    assert "deepseek-ai/DeepSeek-V4.1-Flash" in result["detail"], "the fix must be named"
    assert result["count"] == len(SERVED)


def test_a_served_model_passes(monkeypatch) -> None:
    _patch(monkeypatch)

    result = _call("deepseek-ai/DeepSeek-V4.1-Flash")

    assert result["ok"] is True
    assert "is available" in result["detail"]


def test_a_blank_model_still_tests_reachability(monkeypatch) -> None:
    """A row with no model yet is a connection test, not a failure."""
    _patch(monkeypatch)

    result = _call("")

    assert result["ok"] is True
    assert result["count"] == len(SERVED)


def test_a_provider_that_publishes_no_list_cannot_be_second_guessed(monkeypatch) -> None:
    """Some OpenAI-compatible servers return an empty or non-standard `/models`.

    With nothing to compare against, the test must not invent a rejection — it reports
    reachability, which is all it can honestly claim.
    """
    _patch(monkeypatch, payload={"data": []})

    result = _call("anything/at-all")

    assert result["ok"] is True


def test_a_401_is_still_a_key_problem(monkeypatch) -> None:
    _patch(monkeypatch, status_code=401, payload={})

    result = _call("deepseek-ai/DeepSeek-V4.1-Flash")

    assert result["ok"] is False
    assert "401" in result["detail"]
