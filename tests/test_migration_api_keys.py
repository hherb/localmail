# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Migration 0036's invariants, which no Python code can route around."""
from __future__ import annotations

import hashlib

import psycopg
import pytest


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


def _sha(s: str) -> bytes:
    return hashlib.sha256(s.encode()).digest()


def test_an_api_key_may_have_no_expiry(db_conn):
    uid = _user(db_conn, "bot", is_service=True)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_tokens (token_sha256, user_id, expires_at, api_key_name) "
            "VALUES (%s, %s, NULL, 'bot')",
            (_sha("k1"), uid),
        )
    db_conn.commit()


def test_a_session_token_may_not_have_no_expiry(db_conn):
    """The load-bearing half: dropping NOT NULL alone would allow an immortal
    login token, produced by a one-line bug, with nothing failing."""
    uid = _user(db_conn, "human")
    with pytest.raises(psycopg.errors.CheckViolation):
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO api_tokens (token_sha256, user_id, expires_at) "
                "VALUES (%s, %s, NULL)",
                (_sha("k2"), uid),
            )
    db_conn.rollback()


def test_one_key_per_service_user(db_conn):
    uid = _user(db_conn, "bot", is_service=True)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_tokens (token_sha256, user_id, expires_at, api_key_name) "
            "VALUES (%s, %s, NULL, 'bot')",
            (_sha("k1"), uid),
        )
    with pytest.raises(psycopg.errors.UniqueViolation):
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO api_tokens (token_sha256, user_id, expires_at, api_key_name) "
                "VALUES (%s, %s, NULL, 'bot-2')",
                (_sha("k3"), uid),
            )
    db_conn.rollback()


def test_a_user_may_hold_many_session_tokens(db_conn):
    """The unique index is partial; it must not constrain ordinary tokens."""
    uid = _user(db_conn, "human")
    with db_conn.cursor() as cur:
        for i in range(3):
            cur.execute(
                "INSERT INTO api_tokens (token_sha256, user_id, expires_at) "
                "VALUES (%s, %s, now() + interval '1 day')",
                (_sha(f"s{i}"), uid),
            )
    db_conn.commit()


def test_is_service_defaults_false(db_conn):
    uid = _user(db_conn, "human")
    with db_conn.cursor() as cur:
        cur.execute("SELECT is_service FROM api_users WHERE id = %s", (uid,))
        row = cur.fetchone()
    assert row is not None
    assert row[0] is False
