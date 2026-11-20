"""Roster: specialists live on the server, so keys are not re-posted on every request.

The browser used to keep every API key in localStorage and send the whole roster — keys
included — with every build and every chat turn. Keys now live in one 0600 file under
the workspace (or `HARNESS_ROSTER_FILE`), runs reference specialists by id, and the API
only ever returns a redacted view: `has_key: true`, never the key itself.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from pathlib import Path

from ..models import Specialist

MAX_SPECIALISTS = 16


class RosterStore:
    def __init__(self, path: Path):
        self.path = path

    # ---------- read ----------

    def load(self) -> list[Specialist]:
        if not self.path.is_file():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        entries = raw.get("specialists") if isinstance(raw, dict) else raw
        if not isinstance(entries, list):
            return []
        out: list[Specialist] = []
        seen: set[str] = set()
        for item in entries[:MAX_SPECIALISTS]:
            if not isinstance(item, dict):
                continue
            spec_id = str(item.get("id") or "").strip()
            if not spec_id or spec_id in seen:
                continue
            seen.add(spec_id)
            out.append(
                Specialist(
                    id=spec_id,
                    name=str(item.get("name") or spec_id)[:80],
                    role=str(item.get("role") or "general"),
                    kind=str(item.get("kind") or "openai"),
                    baseUrl=str(item.get("baseUrl") or item.get("base_url") or ""),
                    apiKey=str(item.get("apiKey") or item.get("api_key") or ""),
                    model=str(item.get("model") or ""),
                )
            )
        return out

    def public(self) -> list[dict]:
        return [redacted(spec) for spec in self.load()]

    def get(self, spec_id: str) -> Specialist | None:
        return next((spec for spec in self.load() if spec.id == spec_id), None)

    def select(self, ids: list[str]) -> list[Specialist]:
        """Resolve ids in request order, skipping unknown or unconfigured ones."""
        by_id = {spec.id: spec for spec in self.load()}
        return [by_id[spec_id] for spec_id in ids if spec_id in by_id]

    # ---------- write ----------

    def save(
        self,
        specialists: list[Specialist],
        replace: bool = True,
        clear_keys: Iterable[str] = (),
    ) -> list[Specialist]:
        """Store the roster atomically. Returns what was stored.

        `replace=False` merges by id instead of replacing: a browser that knows about
        one specialist must not be able to delete the rest — and their keys — just by
        starting a run. Only the workshop's explicit save replaces.

        A blank key means "unchanged", so deleting a stored key has to be said out loud:
        `clear_keys` names the ids whose key is dropped even though the payload is empty.
        """
        drop = {spec_id for spec_id in clear_keys if spec_id}
        if not replace:
            incoming = {spec.id: spec for spec in specialists[:MAX_SPECIALISTS] if spec.id}
            kept = [spec for spec in self.load() if spec.id not in incoming]
            specialists = kept + list(incoming.values())

        deduped: dict[str, Specialist] = {}
        for spec in specialists[:MAX_SPECIALISTS]:
            if not spec.id:
                continue
            # A blank key in the payload means "unchanged", so a masked edit cannot
            # silently erase a key the browser never received. A key typed now wins over
            # a clear request; `clear_keys` is only consulted when nothing was typed.
            if not spec.apiKey:
                previous = deduped.get(spec.id) or self.get(spec.id)
                spec = Specialist(
                    id=spec.id,
                    name=spec.name,
                    role=spec.role,
                    kind=spec.kind,
                    baseUrl=spec.baseUrl,
                    apiKey="" if spec.id in drop else (previous.apiKey if previous else ""),
                    model=spec.model,
                )
            deduped[spec.id] = spec

        stored = list(deduped.values())
        payload = {
            "version": 1,
            "specialists": [
                {
                    "id": spec.id,
                    "name": spec.name,
                    "role": spec.role,
                    "kind": spec.kind,
                    "baseUrl": spec.baseUrl,
                    "apiKey": spec.apiKey,
                    "model": spec.model,
                }
                for spec in stored
            ],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)
        return stored


def redacted(spec: Specialist) -> dict:
    return {
        "id": spec.id,
        "name": spec.name,
        "role": spec.role,
        "kind": spec.kind,
        "baseUrl": spec.baseUrl,
        "model": spec.model,
        "has_key": bool(spec.apiKey),
        "configured": spec.ready,
    }
