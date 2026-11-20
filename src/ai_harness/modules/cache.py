"""Response cache: an identical, *deterministic* request is a wasted request.

Two guards keep this honest, because a cache that replays a sampled generation is a
correctness bug, not an optimisation:

* it is **opt-in** (`HARNESS_CACHE=1`) and, even then, only `llm.call` with
  `HARNESS_TEMPERATURE=0` is cached — see `llm.cacheable()`. At any temperature above
  zero the provider samples, so the same prompt legitimately produces a different file;
* the key includes the temperature, the provider base URL and the model, so an entry
  recorded under one sampling setting can never satisfy a request under another.

It is also deliberately narrow: `llm.stream` never uses it, because a streamed file
must be a live generation and a half-cached stream is how artefacts get corrupted.
Entries are evicted least-recently-used so a long-lived process cannot grow unbounded.
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from typing import Any

from ..config.settings import CACHE_ENTRIES
from ..models import Specialist


def _key(spec: Specialist, messages: list[dict[str, str]], temperature: float, json_mode: bool = False) -> str:
    """Stable hash of everything that changes an answer, sampling included."""
    payload = json.dumps(
        {
            "base": (spec.baseUrl or "").rstrip("/"),
            "kind": spec.kind,
            "model": spec.model,
            "temperature": round(float(temperature), 4),
            "json_mode": bool(json_mode),
            "messages": messages,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ResponseCache:
    def __init__(self, max_entries: int):
        self.max_entries = max(1, max_entries)
        self._store: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get(
        self,
        spec: Specialist,
        messages: list[dict[str, str]],
        temperature: float,
        json_mode: bool = False,
    ) -> dict[str, Any] | None:
        cache_key = _key(spec, messages, temperature, json_mode)
        entry = self._store.get(cache_key)
        if entry is None:
            self.misses += 1
            return None
        self.hits += 1
        self._store.move_to_end(cache_key)
        return entry

    def set(
        self,
        spec: Specialist,
        messages: list[dict[str, str]],
        temperature: float,
        entry: dict[str, Any],
        json_mode: bool = False,
    ) -> None:
        cache_key = _key(spec, messages, temperature, json_mode)
        self._store[cache_key] = entry
        self._store.move_to_end(cache_key)
        while len(self._store) > self.max_entries:
            self._store.popitem(last=False)

    def clear(self) -> None:
        self._store.clear()

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {
            "hits": self.hits,
            "misses": self.misses,
            "entries": len(self._store),
            "hit_rate": round(self.hits / total, 3) if total else 0.0,
        }


CACHE = ResponseCache(max_entries=CACHE_ENTRIES)
