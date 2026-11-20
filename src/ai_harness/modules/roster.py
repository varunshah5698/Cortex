"""Roster: specialists live on the server, so keys are not re-posted on every request.

The browser used to keep every API key in localStorage and send the whole roster — keys
included — with every build and every chat turn. Keys now live in one 0600 file
(`HARNESS_KEYS_FILE`), runs reference specialists by id, and the API only ever returns a
redacted view: `has_key: true`, never the key itself.

Keys are stored **per provider identity** (`kind|baseUrl`), not per roster row. A row is
an editable draft: reset the team, duplicate it, or replace the roster and the key for
that provider is still there when the row comes back. Only `clearKey` deletes one. This
is deliberate: tying a key to a browser-generated UUID is how keys disappeared whenever
the roster was reset.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from pathlib import Path

from ..models import Specialist

MAX_SPECIALISTS = 16


def provider_identity(spec: Specialist) -> str:
    """The account a key belongs to: same provider and endpoint, same credential."""
    kind = (spec.kind or "openai").strip().lower()
    base = (spec.baseUrl or "").strip().rstrip("/").lower()
    return f"{kind}|{base}"


class RosterStore:
    def __init__(self, path: Path, keys_path: Path | None = None):
        self.path = path
        self.keys_path = keys_path or path.with_name(".harness-keys.json")

    # ---------- keyring ----------

    def _keys(self) -> dict[str, str]:
        if not self.keys_path.is_file():
            return {}
        try:
            raw = json.loads(self.keys_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {str(key): str(value) for key, value in raw.items() if value}

    def _store_keys(self, keys: dict[str, str]) -> None:
        """Write the keyring atomically; never let a roster save carry a secret."""
        self.keys_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.keys_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(keys, indent=2, sort_keys=True), encoding="utf-8")
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        os.replace(tmp, self.keys_path)

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
        keys = self._keys()
        migrated: dict[str, str] = {}
        out: list[Specialist] = []
        seen: set[str] = set()
        for item in entries[:MAX_SPECIALISTS]:
            if not isinstance(item, dict):
                continue
            spec_id = str(item.get("id") or "").strip()
            if not spec_id or spec_id in seen:
                continue
            seen.add(spec_id)
            spec = Specialist(
                id=spec_id,
                name=str(item.get("name") or spec_id)[:80],
                role=str(item.get("role") or "general"),
                kind=str(item.get("kind") or "openai"),
                baseUrl=str(item.get("baseUrl") or item.get("base_url") or ""),
                apiKey=str(item.get("apiKey") or item.get("api_key") or ""),
                model=str(item.get("model") or ""),
            )
            # A roster written before the keyring existed still works: move its key to
            # the identity store, where replacing the row can no longer lose it.
            legacy = spec.apiKey
            if legacy:
                identity = provider_identity(spec)
                if not keys.get(identity):
                    migrated[identity] = legacy
                    keys[identity] = legacy
            else:
                spec.apiKey = keys.get(provider_identity(spec), "")
            out.append(spec)
        if migrated:
            try:
                self._store_keys(keys)
            except OSError:
                pass
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

        Keys are per provider, not per row: replacing or deleting a specialist only drops
        a key when that is the explicit request. A row that comes back with the same
        endpoint finds its key again.
        """
        drop = {spec_id for spec_id in clear_keys if spec_id}
        previous = {spec.id: spec for spec in self.load()}
        keys = self._keys()

        if not replace:
            incoming = {spec.id: spec for spec in specialists[:MAX_SPECIALISTS] if spec.id}
            kept = [spec for spec in self.load() if spec.id not in incoming]
            specialists = kept + list(incoming.values())

        deduped: dict[str, Specialist] = {}
        for spec in specialists[:MAX_SPECIALISTS]:
            if not spec.id:
                continue
            identity = provider_identity(spec)
            if spec.apiKey:
                # A key typed now wins over a clear request.
                keys[identity] = spec.apiKey
            elif spec.id in drop:
                keys.pop(identity, None)
                before = previous.get(spec.id)
                if before is not None:
                    keys.pop(provider_identity(before), None)
            # A blank key means "unchanged": the keyring already answers for this
            # identity, so nothing has to be copied into the row.
            deduped[spec.id] = Specialist(
                id=spec.id,
                name=spec.name,
                role=spec.role,
                kind=spec.kind,
                baseUrl=spec.baseUrl,
                apiKey="",
                model=spec.model,
            )

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
                    "apiKey": "",
                    "model": spec.model,
                }
                for spec in stored
            ],
        }
        self._store_keys(keys)
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
