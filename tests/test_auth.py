"""Real accounts: signup, login, session cookie and the public-site gate."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from ai_harness import main  # noqa: E402
from ai_harness.auth import AuthStore  # noqa: E402


def _client(tmp_path: Path, monkeypatch) -> TestClient:
    monkeypatch.setattr(main, "AUTH", AuthStore(tmp_path / "auth.db"))
    main._ATTEMPTS.clear()
    return TestClient(main.app, base_url="http://127.0.0.1:5178")


def test_signup_sets_a_session_and_me_reads_it(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)

    created = client.post(
        "/api/auth/signup",
        json={"name": "Ada Lovelace", "email": "ada@example.com", "password": "correct horse"},
    )
    assert created.status_code == 200, created.text
    assert created.json()["user"]["email"] == "ada@example.com"
    assert "cortex_session" in client.cookies

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["name"] == "Ada Lovelace"
    assert client.get("/api/roster").status_code == 200


def test_protected_api_needs_a_session(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)

    blocked = client.get("/api/runs")
    assert blocked.status_code == 401
    assert "www-authenticate" not in blocked.headers

    client.post(
        "/api/auth/signup",
        json={"name": "Ada", "email": "ada@example.com", "password": "correct horse"},
    )
    assert client.get("/api/runs").status_code == 200


def test_duplicate_email_and_wrong_password_are_refused(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    payload = {"name": "Ada", "email": "ada@example.com", "password": "correct horse"}
    assert client.post("/api/auth/signup", json=payload).status_code == 200

    duplicate = client.post(
        "/api/auth/signup",
        json={**payload, "name": "Someone else"},
    )
    assert duplicate.status_code == 409

    wrong = client.post("/api/auth/login", json={"email": "ada@example.com", "password": "not-the-password"})
    assert wrong.status_code == 401

    right = client.post("/api/auth/login", json={"email": "ada@example.com", "password": "correct horse"})
    assert right.status_code == 200


def test_logout_revokes_the_session(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    client.post(
        "/api/auth/signup",
        json={"name": "Ada", "email": "ada@example.com", "password": "correct horse"},
    )
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/runs").status_code == 401
