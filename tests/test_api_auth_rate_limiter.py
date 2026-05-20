"""Postgres-backed login rate limiter (#7)."""
from __future__ import annotations

import psycopg
import pytest

from localmail.api import auth as auth_mod


def _count(conn: psycopg.Connection, sql: str, *params) -> int:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        assert row is not None
        return int(row[0])


def test_record_login_attempt_inserts_failure(db_conn: psycopg.Connection) -> None:
    auth_mod._record_login_attempt(db_conn, "alice", "10.0.0.1", "failure")
    db_conn.commit()
    assert _count(db_conn, "SELECT count(*) FROM api_login_attempts") == 1
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT username, ip, outcome FROM api_login_attempts"
        )
        row = cur.fetchone()
        assert row == ("alice", "10.0.0.1", "failure")


def test_record_login_attempt_null_ip(db_conn: psycopg.Connection) -> None:
    auth_mod._record_login_attempt(db_conn, "bob", None, "success")
    db_conn.commit()
    with db_conn.cursor() as cur:
        cur.execute("SELECT ip FROM api_login_attempts WHERE username = 'bob'")
        row = cur.fetchone()
        assert row is not None
        assert row[0] is None


def test_record_login_attempt_rejects_bad_outcome(db_conn: psycopg.Connection) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        auth_mod._record_login_attempt(db_conn, "alice", "1.1.1.1", "garbage")  # type: ignore[arg-type]
    db_conn.rollback()
