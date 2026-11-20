"""The bridge from a Convex (Google) session to a harness session.

There are two auth stores and they are not the same one: the browser holds a Convex
session token, but `/api` only trusts the `cortex_session` cookie the Python side issues.
A Google sign-in completes at Convex, so without this bridge the person is signed in to
the data layer and 401 on every build, roster and run call — signed in and unable to do
anything.

These tests pin the two properties that matter: the token is *verified* against Convex
rather than trusted, and an account that arrives this way gets a real session.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from ai_harness.auth import AuthError, AuthStore  # noqa: E402


@pytest.fixture()
def store(tmp_path):
    return AuthStore(tmp_path / "auth.db")


def test_an_external_login_creates_an_account_and_a_session(store):
    token, user = store.login_external("ada@example.com", "Ada Lovelace")

    assert token
    assert user["email"] == "ada@example.com"
    assert user["name"] == "Ada Lovelace"
    # The session is real: the same token resolves back to the same person.
    assert store.user_for_token(token)["email"] == "ada@example.com"


def test_a_second_external_login_reuses_the_same_account(store):
    _, first = store.login_external("ada@example.com", "Ada")
    _, second = store.login_external("ada@example.com", "Ada")

    assert first["id"] == second["id"]


def test_a_google_login_links_an_existing_password_account(store):
    # Someone who signed up with a password and later uses Google is one person, not two.
    password_token, created = store.signup("Ada", "ada@example.com", "correct horse battery")
    _, linked = store.login_external("ada@example.com", "Ada")

    assert linked["id"] == created["id"]
    # ...and the password still works, so linking did not break the first way in.
    assert store.login("ada@example.com", "correct horse battery")[1]["id"] == created["id"]
    assert store.user_for_token(password_token)["id"] == created["id"]


def test_an_external_account_cannot_be_logged_into_with_a_guessed_password(store):
    store.login_external("ada@example.com", "Ada")

    # The row has a password hash, but it is a random value nobody was ever told. An empty
    # or guessed password must not open it.
    for guess in ("", "password", "ada@example.com"):
        with pytest.raises(AuthError):
            store.login("ada@example.com", guess)


def test_an_external_login_refuses_an_unusable_email(store):
    for bad in ("", "   ", "not-an-email", "a@b"):
        with pytest.raises(AuthError):
            store.login_external(bad, "Nobody")


def test_an_external_login_without_a_name_falls_back_to_the_email(store):
    _, user = store.login_external("ada@example.com")

    assert user["name"] == "ada"


def test_a_session_from_an_external_login_can_be_logged_out(store):
    token, _ = store.login_external("ada@example.com", "Ada")
    store.logout(token)

    assert store.user_for_token(token) is None
