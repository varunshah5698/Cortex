"""Money: what a call costs, and the cap that stops a run.

Two halves of one concern, which is why they share a module: `estimate` prices token
usage from a model table, `Budget` owns the ceiling a run may not cross. A harness that
reports tokens but never money hides where the spend went; one that reports money but
never stops hides the runaway until the invoice.

Only models the roster actually offers are priced. Free models are detected from the
endpoint, not the name: a local Ollama/vLLM server is free, but the same llama weights
served by Groq or OpenRouter are not. Anything unlisted falls back to the defaults
below, so an unknown hosted model is estimated conservatively rather than reported free.

Prices are USD per 1M tokens, matched by substring, longest pattern first, so
``gpt-4o-mini`` never resolves to the ``gpt-4o`` row. Override without touching code::

    HARNESS_PRICES='{"my-model": [0.2, 0.8]}'
    HARNESS_PRICE_INPUT=0.5 HARNESS_PRICE_OUTPUT=1.5

Limits of 0 mean "no cap", which is what tests and local-only runs want. The budget is
checked *before* every provider request and settled *after* it, so the last call may
overshoot slightly but the run stops immediately after.

An unlisted model is still charged at the defaults — a run must stay bounded — but it is
never *silently* charged: `row_for` names the row (`"default"`), `unpriced` lists those
models, `/api/health` reports them under `pricing.unpriced`, and the live eval suite
refuses to run until one exists. A dollar cap computed from a guessed price is not a
cap.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass

from ..models import Specialist


class BudgetExceeded(RuntimeError):
    """Raised at the tool boundary, never swallowed by a node's error handling."""


# (substring, usd per 1M input tokens, usd per 1M output tokens)
TABLE: list[tuple[str, float, float]] = [
    # The offline eval model: no socket, no tokens, no bill. The non-loopback base URL
    # keeps the full pipeline (research and contracts) in the eval, so it needs an
    # explicit free row rather than the local-endpoint exemption.
    ("scripted", 0.0, 0.0),
    ("gpt-4.1-mini", 0.40, 1.60),
    ("gpt-4.1", 2.00, 8.00),
    ("gpt-4o-mini", 0.15, 0.60),
    ("gpt-4o", 2.50, 10.00),
    ("claude-3-5-haiku", 0.80, 4.00),
    ("claude-sonnet-4", 3.00, 15.00),
    ("claude-opus-4", 15.00, 75.00),
    ("gemini-2.0-flash", 0.10, 0.40),
    ("gemini-2.5-pro", 1.25, 10.00),
    # The eval suite's deterministic stand-in: it never reaches a provider.
    ("scripted", 0.0, 0.0),
]

DEFAULT_INPUT = float(os.getenv("HARNESS_PRICE_INPUT", "0.5"))
DEFAULT_OUTPUT = float(os.getenv("HARNESS_PRICE_OUTPUT", "1.5"))


def _from_env() -> list[tuple[str, float, float]]:
    raw = os.getenv("HARNESS_PRICES", "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, dict):
        return []
    rows: list[tuple[str, float, float]] = []
    for name, pair in data.items():
        try:
            rows.append((str(name).lower(), float(pair[0]), float(pair[1])))
        except (TypeError, ValueError, IndexError):
            continue
    return rows


PRICES: list[tuple[str, float, float]] = sorted(_from_env() + TABLE, key=lambda row: len(row[0]), reverse=True)

# The row a model with no row of its own gets. Kept as a name, not a number, so a caller
# can tell a price from a guess: `row_for` says "default", `is_priced` says False, and
# /api/health and the live eval suite both refuse to pretend otherwise.
DEFAULT_ROW = "default"
LOCAL_ROW = "local"


def row_for(spec: Specialist) -> str:
    """The pricing row this specialist resolves to: a table key, "local", or "default".

    A local endpoint is free whatever its weights are called, which is why the endpoint
    decides that row and not the model name (a llama on Groq is not free; a llama on
    Ollama is).
    """
    if spec.is_local:
        return LOCAL_ROW
    model = (spec.model or "").lower()
    for name, _prompt_price, _completion_price in PRICES:
        if name in model:
            return name
    return DEFAULT_ROW


def is_priced(spec: Specialist) -> bool:
    """False when no row matches this model: the default is a guess, not a price."""
    return row_for(spec) != DEFAULT_ROW


def unpriced(specs: Iterable[Specialist]) -> list[dict]:
    """The specialists that would be charged at the guessed default, one entry per model."""
    found: dict[tuple[str, str], dict] = {}
    for spec in specs:
        if is_priced(spec):
            continue
        entry = found.setdefault(
            (spec.model, spec.baseUrl),
            {"model": spec.model, "baseUrl": spec.baseUrl, "ids": []},
        )
        if spec.id and spec.id not in entry["ids"]:
            entry["ids"].append(spec.id)
    return list(found.values())


def report(specs: Iterable[Specialist]) -> dict:
    """What /api/health shows: every roster model's row, and the ones that have none."""
    specs = list(specs)
    return {
        "default": {"input": DEFAULT_INPUT, "output": DEFAULT_OUTPUT, "unit": "USD per 1M tokens"},
        "models": [
            {"id": spec.id, "model": spec.model, "row": row_for(spec), "priced": is_priced(spec)}
            for spec in specs
            if spec.model or spec.baseUrl
        ],
        "unpriced": unpriced(specs),
    }


def price_for(spec: Specialist) -> tuple[float, float]:
    """(input, output) price per 1M tokens. Goes through the row, so a guess is named."""
    row = row_for(spec)
    if row == LOCAL_ROW:
        return (0.0, 0.0)
    for name, prompt_price, completion_price in PRICES:
        if name == row:
            return (prompt_price, completion_price)
    return (DEFAULT_INPUT, DEFAULT_OUTPUT)


def estimate(spec: Specialist, prompt_tokens: int, completion_tokens: int) -> float:
    """Estimated USD for one call, rounded to a stable precision for the ledger."""
    prompt_price, completion_price = price_for(spec)
    cost = (prompt_tokens / 1_000_000) * prompt_price + (completion_tokens / 1_000_000) * completion_price
    return round(cost, 6)


@dataclass
class Budget:
    max_tokens: int = 0
    max_usd: float = 0.0
    max_calls: int = 0
    tokens: int = 0
    usd: float = 0.0
    calls: int = 0

    def check(self) -> None:
        if self.max_tokens and self.tokens >= self.max_tokens:
            raise BudgetExceeded(f"token budget exhausted: {self.tokens}/{self.max_tokens} tokens used")
        if self.max_usd and self.usd >= self.max_usd:
            raise BudgetExceeded(f"spend budget exhausted: ${self.usd:.4f}/${self.max_usd:.2f} used")
        if self.max_calls and self.calls >= self.max_calls:
            raise BudgetExceeded(f"call budget exhausted: {self.calls}/{self.max_calls} provider calls made")

    def spend(self, tokens: int, usd: float) -> None:
        self.tokens += max(0, int(tokens))
        self.usd = round(self.usd + max(0.0, usd), 6)
        self.calls += 1

    @property
    def remaining_tokens(self) -> int | None:
        return None if not self.max_tokens else max(0, self.max_tokens - self.tokens)

    @property
    def remaining_usd(self) -> float | None:
        return None if not self.max_usd else round(max(0.0, self.max_usd - self.usd), 6)

    def as_dict(self) -> dict:
        return {
            "max_tokens": self.max_tokens,
            "max_usd": self.max_usd,
            "max_calls": self.max_calls,
            "tokens": self.tokens,
            "usd": self.usd,
            "calls": self.calls,
            "remaining_tokens": self.remaining_tokens,
            "remaining_usd": self.remaining_usd,
        }
