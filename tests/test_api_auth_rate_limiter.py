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


from localmail.api.errors import RateLimited
from localmail.config import AuthConfig


def _record_many(
    conn: psycopg.Connection,
    *,
    username: str,
    ip: str | None,
    outcome: str,
    n: int,
) -> None:
    for _ in range(n):
        auth_mod._record_login_attempt(conn, username, ip, outcome)  # type: ignore[arg-type]
    conn.commit()


def test_check_passes_on_empty_table(db_conn: psycopg.Connection) -> None:
    cfg = AuthConfig()
    auth_mod._check_login_rate_limits(db_conn, "alice", "1.1.1.1", cfg=cfg)


def test_check_trips_user_cap(db_conn: psycopg.Connection) -> None:
    cfg = AuthConfig(login_per_user_max=3, login_per_user_window_s=60)
    _record_many(db_conn, username="alice", ip="1.1.1.1", outcome="failure", n=3)
    with pytest.raises(RateLimited) as ei:
        auth_mod._check_login_rate_limits(db_conn, "alice", "1.1.1.1", cfg=cfg)
    assert ei.value.cap == "user"
    assert ei.value.retry_after_s == 60


def test_check_trips_ip_cap_across_usernames(db_conn: psycopg.Connection) -> None:
    """The cross-username brute-force case (#7 motivation)."""
    cfg = AuthConfig(login_per_ip_max=5, login_per_ip_window_s=60)
    for u in ("alice", "bob", "carol", "dave", "eve"):
        auth_mod._record_login_attempt(db_conn, u, "1.1.1.1", "failure")
    db_conn.commit()
    with pytest.raises(RateLimited) as ei:
        auth_mod._check_login_rate_limits(db_conn, "frank", "1.1.1.1", cfg=cfg)
    assert ei.value.cap == "ip"
    assert ei.value.retry_after_s == 60


def test_check_trips_global_cap_including_successes(db_conn: psycopg.Connection) -> None:
    cfg = AuthConfig(login_global_max=4, login_global_window_s=60)
    # Mix successes + failures from different IPs — global counts both.
    auth_mod._record_login_attempt(db_conn, "alice", "1.1.1.1", "success")
    auth_mod._record_login_attempt(db_conn, "bob", "2.2.2.2", "failure")
    auth_mod._record_login_attempt(db_conn, "carol", "3.3.3.3", "success")
    auth_mod._record_login_attempt(db_conn, "dave", "4.4.4.4", "failure")
    db_conn.commit()
    with pytest.raises(RateLimited) as ei:
        auth_mod._check_login_rate_limits(db_conn, "eve", "5.5.5.5", cfg=cfg)
    assert ei.value.cap == "global"


def test_check_user_cap_clears_on_success(db_conn: psycopg.Connection) -> None:
    """A successful login clears the per-user counter (preserves prior semantics)."""
    cfg = AuthConfig(login_per_user_max=3)
    _record_many(db_conn, username="alice", ip="1.1.1.1", outcome="failure", n=2)
    auth_mod._record_login_attempt(db_conn, "alice", "1.1.1.1", "success")
    _record_many(db_conn, username="alice", ip="1.1.1.1", outcome="failure", n=2)
    # Only 2 failures *since* last success — under the cap of 3.
    auth_mod._check_login_rate_limits(db_conn, "alice", "1.1.1.1", cfg=cfg)


def test_check_ip_cap_does_not_clear_on_success(db_conn: psycopg.Connection) -> None:
    """Success from one user does NOT unlock the IP for another user's failures."""
    cfg = AuthConfig(login_per_ip_max=4)
    _record_many(db_conn, username="alice", ip="1.1.1.1", outcome="failure", n=2)
    auth_mod._record_login_attempt(db_conn, "alice", "1.1.1.1", "success")
    _record_many(db_conn, username="bob", ip="1.1.1.1", outcome="failure", n=3)
    with pytest.raises(RateLimited) as ei:
        auth_mod._check_login_rate_limits(db_conn, "carol", "1.1.1.1", cfg=cfg)
    assert ei.value.cap == "ip"


def test_check_window_expires(db_conn: psycopg.Connection) -> None:
    """Failures outside the window do not count."""
    cfg = AuthConfig(login_per_user_max=2, login_per_user_window_s=1)
    _record_many(db_conn, username="alice", ip="1.1.1.1", outcome="failure", n=2)
    # Bump those rows' ts into the past via SQL.
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE api_login_attempts SET ts = now() - interval '10 seconds'"
        )
    db_conn.commit()
    auth_mod._check_login_rate_limits(db_conn, "alice", "1.1.1.1", cfg=cfg)


def test_check_null_ip_does_not_contribute_to_ip_cap(db_conn: psycopg.Connection) -> None:
    cfg = AuthConfig(login_per_ip_max=2, login_per_user_max=10)
    for _ in range(5):
        auth_mod._record_login_attempt(db_conn, "alice", None, "failure")
    db_conn.commit()
    # No IP context → no IP cap evaluated for this call site.
    auth_mod._check_login_rate_limits(db_conn, "alice", None, cfg=cfg)


def test_check_order_global_first(db_conn: psycopg.Connection) -> None:
    """Global cap is checked first so RateLimited.cap == 'global' even when
    per-IP and per-user are also over."""
    cfg = AuthConfig(
        login_per_user_max=1,
        login_per_ip_max=1,
        login_global_max=1,
    )
    auth_mod._record_login_attempt(db_conn, "alice", "1.1.1.1", "failure")
    db_conn.commit()
    with pytest.raises(RateLimited) as ei:
        auth_mod._check_login_rate_limits(db_conn, "alice", "1.1.1.1", cfg=cfg)
    assert ei.value.cap == "global"


def test_sweep_deletes_expired_rows(db_conn: psycopg.Connection) -> None:
    # Two recent rows, three old rows.
    for u in ("alice", "bob"):
        auth_mod._record_login_attempt(db_conn, u, "1.1.1.1", "failure")
    for u in ("carol", "dave", "eve"):
        auth_mod._record_login_attempt(db_conn, u, "2.2.2.2", "failure")
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE api_login_attempts SET ts = now() - interval '1 day' "
            "WHERE username IN ('carol','dave','eve')"
        )
    db_conn.commit()
    deleted = auth_mod._sweep_login_attempts(db_conn, retention_s=60)
    db_conn.commit()
    assert deleted == 3
    assert _count(db_conn, "SELECT count(*) FROM api_login_attempts") == 2


def test_sweep_no_op_when_lock_contended(db_conn: psycopg.Connection, db_dsn: str) -> None:
    """Second worker can't pile up DELETEs while another holds the lock."""
    auth_mod._record_login_attempt(db_conn, "alice", "1.1.1.1", "failure")
    with db_conn.cursor() as cur:
        cur.execute("UPDATE api_login_attempts SET ts = now() - interval '1 day'")
    db_conn.commit()

    other = psycopg.connect(db_dsn, autocommit=False)
    try:
        # Acquire the sweep advisory lock on `other` for the whole test.
        with other.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (auth_mod._SWEEP_ADVISORY_LOCK_KEY,))
        # `db_conn` tries to sweep — must short-circuit.
        deleted = auth_mod._sweep_login_attempts(db_conn, retention_s=60)
        assert deleted == 0  # lock not acquired → did not run
    finally:
        with other.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (auth_mod._SWEEP_ADVISORY_LOCK_KEY,))
        other.close()
