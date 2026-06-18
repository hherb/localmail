# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""DB-backed per-IP sliding-window rate limit for open Dynamic Client
Registration, mirroring api_login_attempts (multi-worker-safe).
"""
from __future__ import annotations

import psycopg

# Stable advisory-lock key for the registration sweep (distinct from the login
# sweep's key). Arbitrary fixed int64.
_SWEEP_LOCK_KEY = 0x6F_61_75_74_68_72_65_67  # "oauthreg" in ASCII


def reset(conn: psycopg.Connection) -> None:
    """Test-only: truncate the audit table. Caller commits."""
    with conn.cursor() as cur:
        cur.execute("TRUNCATE oauth_registration_attempts RESTART IDENTITY")


def record(conn: psycopg.Connection, ip: str | None) -> None:
    """Append one registration attempt. Caller commits."""
    with conn.cursor() as cur:
        cur.execute("INSERT INTO oauth_registration_attempts (ip) VALUES (%s)", (ip,))


def count_recent(conn: psycopg.Connection, ip: str | None, *, window_s: int) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM oauth_registration_attempts "
            "WHERE ip = %s AND ts > now() - make_interval(secs => %s)",
            (ip, window_s),
        )
        row = cur.fetchone()
    assert row is not None
    return int(row[0])


def over_limit(
    conn: psycopg.Connection, ip: str | None, *, window_s: int, max_n: int
) -> bool:
    return count_recent(conn, ip, window_s=window_s) >= max_n


def sweep(conn: psycopg.Connection, *, retention_s: int) -> int:
    """Best-effort DELETE of expired rows, advisory-lock-gated. Caller commits."""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (_SWEEP_LOCK_KEY,))
        row = cur.fetchone()
        assert row is not None
        if not row[0]:
            return 0
        try:
            cur.execute(
                "DELETE FROM oauth_registration_attempts "
                "WHERE ts < now() - make_interval(secs => %s)",
                (retention_s,),
            )
            return cur.rowcount
        finally:
            cur.execute("SELECT pg_advisory_unlock(%s)", (_SWEEP_LOCK_KEY,))
