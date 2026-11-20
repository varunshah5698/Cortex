"""Accounts and sessions for the public site.

The deployment is public, so the harness needs a real gate rather than the old demo
login. Passwords are hashed with scrypt and a per-user salt; sessions are random tokens
stored hashed in SQLite, delivered in an HttpOnly cookie. The database lives beside the
workspace so a local checkout keeps its accounts and a Render disk can persist them.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]{2,}$")
SESSION_SECONDS = 7 * 24 * 60 * 60
PBKDF2_ITERATIONS = 390_000
PBKDF2_LEN = 32


class AuthError(Exception):
    """A user-facing signup/login failure with the HTTP status it should carry."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AuthStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    password_hash BLOB NOT NULL,
                    salt BLOB NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at REAL NOT NULL
                )
                """
            )
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _conn(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _hash(password: str, salt: bytes) -> bytes:
        # PBKDF2-HMAC-SHA256 is in the stdlib on every runtime this project ships on.
        # scrypt is not exposed by the Python 3.9 OpenSSL build used in the venv.
        return hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            PBKDF2_ITERATIONS,
            dklen=PBKDF2_LEN,
        )

    def _public_user(self, row: sqlite3.Row) -> dict:
        return {"id": row["id"], "name": row["name"], "email": row["email"], "created": row["created_at"]}

    def _session(self, user_id: str) -> tuple[str, dict]:
        token = secrets.token_urlsafe(32)
        expires = time.time() + SESSION_SECONDS
        with self._conn() as db:
            db.execute("DELETE FROM sessions WHERE expires_at <= ?", (time.time(),))
            db.execute(
                "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (_token_hash(token), user_id, _now_iso(), expires),
            )
            row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise AuthError("Account not found.", 401)
        return token, self._public_user(row)

    def signup(self, name: str, email: str, password: str) -> tuple[str, dict]:
        name = (name or "").strip()
        email = (email or "").strip().lower()
        if not 1 <= len(name) <= 80:
            raise AuthError("Enter your name (up to 80 characters).")
        if len(email) > 254 or not EMAIL.match(email):
            raise AuthError("Enter a valid email address.")
        if len(password) < 8:
            raise AuthError("Use a password with at least 8 characters.")
        if len(password) > 200:
            raise AuthError("That password is too long.")
        salt = os.urandom(16)
        digest = self._hash(password, salt)
        user_id = uuid.uuid4().hex
        try:
            with self._conn() as db:
                db.execute(
                    "INSERT INTO users (id, name, email, password_hash, salt, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (user_id, name, email, digest, salt, _now_iso()),
                )
        except sqlite3.IntegrityError:
            raise AuthError("An account with that email already exists.", 409) from None
        return self._session(user_id)

    def login(self, email: str, password: str) -> tuple[str, dict]:
        email = (email or "").strip().lower()
        if not email or not password:
            raise AuthError("Enter your email and password.", 401)
        with self._conn() as db:
            row = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if row is None:
            raise AuthError("Email or password is incorrect.", 401)
        supplied = self._hash(password, row["salt"])
        if not hmac.compare_digest(supplied, row["password_hash"]):
            raise AuthError("Email or password is incorrect.", 401)
        return self._session(row["id"])

    def user_for_token(self, token: str | None) -> dict | None:
        if not token:
            return None
        with self._conn() as db:
            row = db.execute(
                """
                SELECT users.* FROM sessions
                JOIN users ON users.id = sessions.user_id
                WHERE sessions.token_hash = ? AND sessions.expires_at > ?
                """,
                (_token_hash(token), time.time()),
            ).fetchone()
        return self._public_user(row) if row is not None else None

    def logout(self, token: str | None) -> None:
        if not token:
            return
        with self._conn() as db:
            db.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))
