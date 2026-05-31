"""Service layer for daemon status (2B.2).

Pure read accessor over a psycopg connection — no FastAPI, no IO beyond the
conn. Daemon status is operator-global (no per-user ACL); the HTTP route that
exposes it (2B.4) is admin-gated. Staleness is derived in SQL from
now() - last_heartbeat_at so it can't drift from the writer's clock.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import psycopg
from psycopg.rows import class_row


@dataclass(frozen=True)
class HeartbeatRow:
    worker_kind: str
    account_id: int | None
    state: str
    current_folder: str | None
    last_error_msg: str | None
    started_at: datetime
    last_heartbeat_at: datetime
    stale: bool


@dataclass(frozen=True)
class DaemonStatus:
    heartbeats: list[HeartbeatRow]


def get_daemon_status(
    conn: psycopg.Connection, *, stale_seconds: int
) -> DaemonStatus:
    """Return every heartbeat row with a derived `stale` flag, account rows
    first (then NULL-account process rows), ordered by worker_kind within."""
    with conn.cursor(row_factory=class_row(HeartbeatRow)) as cur:
        cur.execute(
            """
            SELECT worker_kind, account_id, state, current_folder,
                   last_error_msg, started_at, last_heartbeat_at,
                   (now() - last_heartbeat_at) > make_interval(secs => %s) AS stale
              FROM daemon_heartbeats
             ORDER BY account_id NULLS LAST, worker_kind
            """,
            (stale_seconds,),
        )
        return DaemonStatus(heartbeats=cur.fetchall())


CommandName = Literal["reload-now", "restart-account", "drain-stop"]
# Only the terminal states are a valid mark target — a claimed, in-flight row
# must never be set back to 'queued' (it would be re-claimed under a held lock).
# The full 'queued'/'done'/'failed' domain lives in the migration's CHECK.
TerminalCommandState = Literal["done", "failed"]


@dataclass(frozen=True)
class DaemonCommand:
    id: int
    command: str
    account_id: int | None
    requested_by: int | None
    requested_at: datetime


def enqueue_command(
    conn: psycopg.Connection,
    *,
    command: CommandName,
    account_id: int | None = None,
    requested_by: int | None,
) -> int:
    """Queue one imperative daemon command and NOTIFY the listener. Returns the
    new row id. Does NOT commit (caller owns the tx). The NOTIFY is transactional
    — it reaches a LISTENing daemon only when the caller commits.

    `restart-account` requires `account_id`; the other commands forbid it. The
    DB CHECK is the authority (a bad pairing raises CheckViolation on flush)."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO daemon_commands (command, account_id, requested_by) "
            "VALUES (%s, %s, %s) RETURNING id",
            (command, account_id, requested_by),
        )
        row = cur.fetchone()
        assert row is not None
        new_id = int(row[0])
    # Channel name is a SQL identifier literal, not a parameter — hardcoded.
    conn.execute("NOTIFY daemon_commands")
    return new_id


def claim_commands(conn: psycopg.Connection) -> list[DaemonCommand]:
    """Claim every queued command oldest-first, locking the rows so a concurrent
    consumer skips them (FOR UPDATE SKIP LOCKED). Sets picked_at = now() on each.
    Does NOT commit — the caller acts on each command, marks it, then commits so
    the lock is held across the work (single-instance daemon; defensive lock)."""
    with conn.cursor(row_factory=class_row(DaemonCommand)) as cur:
        cur.execute(
            "SELECT id, command, account_id, requested_by, requested_at "
            "FROM daemon_commands WHERE state = 'queued' "
            "ORDER BY requested_at, id FOR UPDATE SKIP LOCKED"
        )
        claimed = cur.fetchall()
    if claimed:
        conn.execute(
            "UPDATE daemon_commands SET picked_at = now() WHERE id = ANY(%s)",
            ([c.id for c in claimed],),
        )
    return claimed


def mark_command(
    conn: psycopg.Connection,
    command_id: int,
    *,
    state: TerminalCommandState,
    result_msg: str | None = None,
) -> None:
    """Mark a claimed command terminal (done/failed) with a result message and
    done_at = now(). Does NOT commit (caller owns the tx)."""
    conn.execute(
        "UPDATE daemon_commands SET state = %s, result_msg = %s, done_at = now() "
        "WHERE id = %s",
        (state, result_msg, command_id),
    )
