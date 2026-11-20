"""Provider keys belong to a provider, not to a browser-generated roster row.

A roster row is a draft: reset the team, replace the roster, or copy a specialist and
the same endpoint must still find its key. These tests pin that, and that the secret is
never written into the roster file.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_harness.models import Specialist  # noqa: E402
from ai_harness.modules.roster import RosterStore, provider_identity  # noqa: E402


def spec(spec_id="vega", api_key="", kind="openai", base="https://api.openai.com/v1"):
    return Specialist(
        id=spec_id,
        name=spec_id.title(),
        role="frontend",
        kind=kind,
        baseUrl=base,
        apiKey=api_key,
        model="gpt-4.1-mini",
    )


def test_a_key_survives_a_replaced_roster_and_a_new_row_id(tmp_path):
    store = RosterStore(tmp_path / "roster.json", tmp_path / "keys.json")
    store.save([spec(api_key="sk-live")])

    # an explicit workshop save replaces the roster; the keyring is not the roster
    store.save([])
    assert store.load() == []

    # the provider comes back under a new id and no key in the payload
    store.save([spec(spec_id="new-id")])
    stored = store.get("new-id")
    assert stored is not None and stored.apiKey == "sk-live"
    assert store.public()[0]["has_key"] is True


def test_the_roster_file_never_contains_the_secret(tmp_path):
    roster, keys = tmp_path / "roster.json", tmp_path / "keys.json"
    store = RosterStore(roster, keys)
    store.save([spec(api_key="sk-secret")])

    assert "sk-secret" not in roster.read_text(encoding="utf-8")
    assert json.loads(keys.read_text(encoding="utf-8"))[provider_identity(spec())] == "sk-secret"
    assert (keys.stat().st_mode & 0o777) == 0o600


def test_clearing_a_key_drops_it_for_that_provider_only(tmp_path):
    store = RosterStore(tmp_path / "roster.json", tmp_path / "keys.json")
    openai = spec(api_key="sk-openai")
    groq = spec(spec_id="groq", api_key="gsk-groq", kind="groq", base="https://api.groq.com/openai/v1")
    store.save([openai, groq])

    store.save([spec(), groq], clear_keys=["vega"])
    assert store.get("vega").apiKey == ""
    assert store.get("groq").apiKey == "gsk-groq"

    # a row re-added later does not resurrect a cleared key
    store.save([])
    store.save([spec(spec_id="again")])
    assert store.get("again").apiKey == ""


def test_a_key_from_an_older_roster_file_migrates_to_the_keyring(tmp_path):
    roster, keys = tmp_path / "roster.json", tmp_path / "keys.json"
    roster.write_text(
        json.dumps(
            {
                "version": 1,
                "specialists": [
                    {
                        "id": "vega",
                        "name": "Vega",
                        "role": "frontend",
                        "kind": "openai",
                        "baseUrl": "https://api.openai.com/v1",
                        "apiKey": "sk-old",
                        "model": "gpt-4.1-mini",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    store = RosterStore(roster, keys)
    assert store.get("vega").apiKey == "sk-old"
    assert json.loads(keys.read_text(encoding="utf-8"))[provider_identity(spec())] == "sk-old"


def test_the_keyring_is_a_denied_path_for_model_tools():
    from ai_harness.modules.permissions import DENY_PATHS

    assert ".harness-keys.json" in DENY_PATHS


def test_a_keyless_hosted_provider_is_ready_without_a_key():
    free = Specialist(
        id="free",
        name="Free",
        role="general",
        baseUrl="https://text.pollinations.ai/openai/v1",
        model="openai",
    )
    assert free.ready is True
    paid = Specialist(id="paid", name="Paid", baseUrl="https://api.openai.com/v1", model="gpt-4.1-mini")
    assert paid.ready is False
