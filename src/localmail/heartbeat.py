"""Daemon liveness heartbeats (2B.2, Plane A).

Every daemon worker thread (per-account IDLE + poll) and process-level worker
(embed, extract, reconcile) upserts a single `daemon_heartbeats` row at the top
of each loop iteration and on state transitions. The admin daemon-status reader
(api/admin/daemon.py) derives liveness purely from now() - last_heartbeat_at.

`record_heartbeat` / `clear_all_heartbeats` are pure-ish: they take a conn and
do NOT commit (the caller owns the transaction). `safe_heartbeat` borrows a pool
connection, records, commits, and swallows every exception — a heartbeat write
must never crash the sync/poll loop that calls it.
"""
from __future__ import annotations

import logging
from typing import Literal

import psycopg
from psycopg_pool import ConnectionPool

log = logging.getLogger(__name__)

WorkerKind = Literal["idle", "poll", "embed", "extract", "reconcile"]
WorkerState = Literal[
    "starting", "connecting", "idle", "polling",
    "syncing", "error", "reconnecting", "stopped",
]

_UPSERT_ACCOUNT = """
    INSERT INTO daemon_heartbeats
        (worker_kind, account_id, state, current_folder,
         last_error_msg, started_at, last_heartbeat_at)
    VALUES (%s, %s, %s, %s, %s, now(), now())
    ON CONFLICT (worker_kind, account_id) WHERE account_id IS NOT NULL
    DO UPDATE SET state = EXCLUDED.state,
                  current_folder = EXCLUDED.current_folder,
                  last_error_msg = EXCLUDED.last_error_msg,
                  last_heartbeat_at = now()
"""

_UPSERT_PROCESS = """
    INSERT INTO daemon_heartbeats
        (worker_kind, account_id, state, current_folder,
         last_error_msg, started_at, last_heartbeat_at)
    VALUES (%s, NULL, %s, %s, %s, now(), now())
    ON CONFLICT (worker_kind) WHERE account_id IS NULL
    DO UPDATE SET state = EXCLUDED.state,
                  current_folder = EXCLUDED.current_folder,
                  last_error_msg = EXCLUDED.last_error_msg,
                  last_heartbeat_at = now()
"""


def record_heartbeat(
    conn: psycopg.Connection,
    *,
    worker_kind: WorkerKind,
    account_id: int | None,
    state: WorkerState,
    current_folder: str | None = None,
    last_error_msg: str | None = None,
) -> None:
    """Upsert this worker's heartbeat row. Does NOT commit (caller owns the tx).

    Account-scoped workers (idle/poll) pass an account_id and hit the
    `daemon_heartbeats_acct_idx` partial index; process-level workers
    (embed/extract/reconcile) pass None and hit `daemon_heartbeats_proc_idx`.
    `started_at` is set only on insert (the DO UPDATE never touches it).
    """
    if account_id is None:
        conn.execute(_UPSERT_PROCESS,
                     (worker_kind, state, current_folder, last_error_msg))
    else:
        conn.execute(_UPSERT_ACCOUNT,
                     (worker_kind, account_id, state, current_folder, last_error_msg))


def clear_all_heartbeats(conn: psycopg.Connection) -> None:
    """Delete every heartbeat row (single-instance startup reset). No commit."""
    conn.execute("DELETE FROM daemon_heartbeats")


def safe_heartbeat(
    pool: ConnectionPool,
    *,
    worker_kind: WorkerKind,
    account_id: int | None,
    state: WorkerState,
    current_folder: str | None = None,
    last_error_msg: str | None = None,
) -> None:
    """Borrow a pool connection, record a heartbeat, commit. Never raises.

    All exceptions are logged at WARNING and swallowed so a transient DB blip
    or a heartbeat-write bug can never crash the long-lived loop that calls it.
    """
    try:
        with pool.connection() as conn:
            record_heartbeat(
                conn, worker_kind=worker_kind, account_id=account_id,
                state=state, current_folder=current_folder,
                last_error_msg=last_error_msg,
            )
    except Exception:  # noqa: BLE001
        log.warning("heartbeat write failed (kind=%s account_id=%s)",
                    worker_kind, account_id, exc_info=True)
