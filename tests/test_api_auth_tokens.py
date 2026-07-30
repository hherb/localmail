# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

import base64
import re

import psycopg

from localmail.api.auth import (
    generate_token,
    hash_token,
    issue_token,
    verify_token,
    TOKEN_TTL_DAYS,
)


def _make_user(conn: psycopg.Connection, username: str = "alice") -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash) VALUES (%s, %s) RETURNING id",
            (username, "$argon2id$dummy"),
        )
        row = cur.fetchone()
        assert row is not None
        conn.commit()
        return row[0]


def test_generate_token_is_url_safe_base64() -> None:
    tok = generate_token()
    assert re.fullmatch(r"[A-Za-z0-9_-]+", tok)
    raw = base64.urlsafe_b64decode(tok + "=" * (-len(tok) % 4))
    assert len(raw) == 32


def test_generate_token_unique() -> None:
    assert generate_token() != generate_token()


def test_hash_token_is_deterministic_sha256() -> None:
    h1 = hash_token("abc")
    h2 = hash_token("abc")
    assert h1 == h2
    assert len(h1) == 32  # SHA-256 = 32 bytes
    assert isinstance(h1, bytes)


def test_issue_and_verify_token_roundtrip(db_conn: psycopg.Connection) -> None:
    uid = _make_user(db_conn)
    tok, expires_at = issue_token(db_conn, uid)
    db_conn.commit()
    assert isinstance(tok, str)
    assert expires_at is not None
    user = verify_token(db_conn, tok)
    assert user is not None
    assert user.id == uid
    assert user.username == "alice"


def test_verify_token_returns_none_for_unknown(db_conn: psycopg.Connection) -> None:
    _make_user(db_conn)
    assert verify_token(db_conn, "totally-bogus-token") is None


def test_verify_token_returns_none_for_expired(db_conn: psycopg.Connection) -> None:
    uid = _make_user(db_conn)
    tok = generate_token()
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_tokens (token_sha256, user_id, expires_at) "
            "VALUES (%s, %s, now() - interval '1 hour')",
            (hash_token(tok), uid),
        )
    db_conn.commit()
    assert verify_token(db_conn, tok) is None


def test_verify_token_returns_none_for_disabled_user(db_conn: psycopg.Connection) -> None:
    uid = _make_user(db_conn)
    tok, _ = issue_token(db_conn, uid)
    with db_conn.cursor() as cur:
        cur.execute("UPDATE api_users SET disabled_at = now() WHERE id = %s", (uid,))
    db_conn.commit()
    assert verify_token(db_conn, tok) is None


def test_verify_token_rejects_token_issued_before_session_revocation(
    db_conn: psycopg.Connection,
) -> None:
    """Regression: revoke-sessions / revoke-admin-sessions must also cut off
    bearer tokens, not just admin cookies. A token issued before
    sessions_invalidated_at stops authenticating; one issued after survives."""
    uid = _make_user(db_conn)
    old_tok, _ = issue_token(db_conn, uid)
    db_conn.commit()
    assert verify_token(db_conn, old_tok) is not None

    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE api_users SET sessions_invalidated_at = now() WHERE id = %s",
            (uid,),
        )
    db_conn.commit()

    # The pre-revocation token is now invalid...
    assert verify_token(db_conn, old_tok) is None
    # ...but a freshly-issued token (created_at after the cutoff) works.
    new_tok, _ = issue_token(db_conn, uid)
    db_conn.commit()
    assert verify_token(db_conn, new_tok) is not None


def test_verify_token_updates_last_used_at(db_conn: psycopg.Connection) -> None:
    uid = _make_user(db_conn)
    tok, _ = issue_token(db_conn, uid)
    db_conn.commit()
    verify_token(db_conn, tok)
    db_conn.commit()
    with db_conn.cursor() as cur:
        cur.execute("SELECT last_used_at FROM api_tokens WHERE token_sha256 = %s", (hash_token(tok),))
        row = cur.fetchone()
        assert row is not None
        assert row[0] is not None


def test_verify_token_reports_is_admin_flag(db_conn: psycopg.Connection) -> None:
    from localmail.api.auth import hash_password

    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash, is_admin) "
            "VALUES ('root', %s, TRUE), ('peon', %s, FALSE) RETURNING id",
            (hash_password("pw"), hash_password("pw")),
        )
    with db_conn.cursor() as cur:
        cur.execute("SELECT id, username FROM api_users ORDER BY username")
        rows = cur.fetchall()
    ids = {username: uid for uid, username in rows}
    admin_tok, _ = issue_token(db_conn, ids["root"])
    peon_tok, _ = issue_token(db_conn, ids["peon"])
    db_conn.commit()

    admin_user = verify_token(db_conn, admin_tok)
    peon_user = verify_token(db_conn, peon_tok)
    assert admin_user is not None and admin_user.is_admin is True
    assert peon_user is not None and peon_user.is_admin is False
