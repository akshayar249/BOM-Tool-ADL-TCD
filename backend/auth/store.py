"""Database-backed users and sessions for MachVault sign-in."""

from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import create_engine, text

SESSION_COOKIE = "mv_session"
SESSION_DAYS = 7
_PBKDF2_ROUNDS = 200_000


def _engine():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is not configured")
    return create_engine(database_url, pool_pre_ping=True)


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split("$", 1)
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return secrets.compare_digest(digest.hex(), digest_hex)


def _public_user(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "username": row["username"],
        "display_name": row["display_name"],
        "role": row["role"],
    }


def ensure_auth_schema() -> None:
    engine = _engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS app_users (
                    id SERIAL PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    display_name TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'User',
                    password_hash TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS app_sessions (
                    token TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    expires_at TIMESTAMPTZ NOT NULL
                )
                """
            )
        )
        existing = conn.execute(
            text("SELECT id FROM app_users WHERE lower(username) = lower(:username)"),
            {"username": "Admin"},
        ).first()
        if existing is None:
            conn.execute(
                text(
                    """
                    INSERT INTO app_users (username, display_name, role, password_hash)
                    VALUES (:username, :display_name, :role, :password_hash)
                    """
                ),
                {
                    "username": "Admin",
                    "display_name": "Admin",
                    "role": "Administrator",
                    "password_hash": hash_password("Admin"),
                },
            )


def authenticate(username: str, password: str) -> dict[str, Any] | None:
    engine = _engine()
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT id, username, display_name, role, password_hash
                FROM app_users
                WHERE lower(username) = lower(:username)
                """
            ),
            {"username": username.strip()},
        ).mappings().first()
    if not row or not verify_password(password, row["password_hash"]):
        return None
    return _public_user(row)


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
    engine = _engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO app_sessions (token, user_id, expires_at)
                VALUES (:token, :user_id, :expires_at)
                """
            ),
            {"token": token, "user_id": user_id, "expires_at": expires},
        )
    return token


def user_for_token(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    engine = _engine()
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT u.id, u.username, u.display_name, u.role
                FROM app_sessions s
                JOIN app_users u ON u.id = s.user_id
                WHERE s.token = :token AND s.expires_at > now()
                """
            ),
            {"token": token},
        ).mappings().first()
    if not row:
        return None
    return _public_user(row)


def delete_session(token: str | None) -> None:
    if not token:
        return
    engine = _engine()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM app_sessions WHERE token = :token"), {"token": token})
