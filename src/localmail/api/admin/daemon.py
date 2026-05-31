"""Service layer for daemon status (2B.2).

Pure read accessor over a psycopg connection — no FastAPI, no IO beyond the
conn. Daemon status is operator-global (no per-user ACL); the HTTP route that
exposes it (2B.4) is admin-gated. Staleness is derived in SQL from
now() - last_heartbeat_at so it can't drift from the writer's clock.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

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
