# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""verify_token over the two credential kinds: an immortal API key and an
ordinary session token."""
from __future__ import annotations

import psycopg

from localmail.api.auth import hash_token, issue_token, verify_token


def _user(conn: psycopg.Connection, username: str, *, is_service: bool = False) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_users (username, password_hash, is_service) "
            "VALUES (%s, 'x', %s) RETURNING id",
            (username, is_service),
        )
        row = cur.fetchone()
    assert row is not None
    return int(row[0])


def _mint_key(conn: psycopg.Connection, uid: int, name: str, raw: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_tokens (token_sha256, user_id, expires_at, api_key_name) "
            "VALUES (%s, %s, NULL, %s)",
            (hash_token(raw), uid, name),
        )


def test_a_key_with_no_expiry_authenticates(db_conn):
    uid = _user(db_conn, "bot", is_service=True)
    _mint_key(db_conn, uid, "bot", "lmk_raw")
    db_conn.commit()
    user = verify_token(db_conn, "lmk_raw")
    assert user is not None
    assert user.id == uid
    assert user.is_api_key is True


def test_a_session_token_reports_is_api_key_false(db_conn):
    uid = _user(db_conn, "human")
    tok, _ = issue_token(db_conn, uid)
    db_conn.commit()
    user = verify_token(db_conn, tok)
    assert user is not None
    assert user.is_api_key is False


def test_an_expired_session_token_is_still_rejected(db_conn):
    uid = _user(db_conn, "human")
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_tokens (token_sha256, user_id, expires_at) "
            "VALUES (%s, %s, now() - interval '1 second')",
            (hash_token("stale"), uid),
        )
    db_conn.commit()
    assert verify_token(db_conn, "stale") is None


def test_session_revocation_kills_the_key(db_conn):
    uid = _user(db_conn, "bot", is_service=True)
    _mint_key(db_conn, uid, "bot", "lmk_raw")
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE api_users SET sessions_invalidated_at = now() + interval '1 second' "
            "WHERE id = %s",
            (uid,),
        )
    db_conn.commit()
    assert verify_token(db_conn, "lmk_raw") is None


def test_disabling_the_principal_kills_the_key(db_conn):
    uid = _user(db_conn, "bot", is_service=True)
    _mint_key(db_conn, uid, "bot", "lmk_raw")
    with db_conn.cursor() as cur:
        cur.execute("UPDATE api_users SET disabled_at = now() WHERE id = %s", (uid,))
    db_conn.commit()
    assert verify_token(db_conn, "lmk_raw") is None


def test_deleting_the_token_row_kills_the_key(db_conn):
    uid = _user(db_conn, "bot", is_service=True)
    _mint_key(db_conn, uid, "bot", "lmk_raw")
    db_conn.commit()
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM api_tokens WHERE user_id = %s", (uid,))
    db_conn.commit()
    assert verify_token(db_conn, "lmk_raw") is None


def test_the_mcp_verifier_accepts_a_key(db_conn):
    """/mcp is one of the two surfaces a key exists to reach."""
    import anyio

    from localmail.mcp.auth import LocalmailTokenVerifier

    uid = _user(db_conn, "bot", is_service=True)
    _mint_key(db_conn, uid, "bot", "lmk_raw")
    db_conn.commit()

    class _OneConnPool:
        def connection(self):
            from contextlib import contextmanager

            @contextmanager
            def _cm():
                yield db_conn

            return _cm()

    verifier = LocalmailTokenVerifier(_OneConnPool())  # type: ignore[arg-type]
    access = anyio.run(verifier.verify_token, "lmk_raw")
    assert access is not None
    assert access.subject == str(uid)
