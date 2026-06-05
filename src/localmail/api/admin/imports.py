"""Service layer for admin-UI archive imports (Sub-plan 2A.5).

Transport-free: pure functions over a psycopg connection, no FastAPI imports.
Admin-gated at the router; not per-user ACL-scoped (consistent with the
accounts/users admin services). Composes api/admin/accounts for archive
validation and importer.runner for execution.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import psycopg
from psycopg.rows import class_row

from localmail.api.admin import accounts as _accounts
from localmail.api.errors import NotFound
from localmail.importer import runner as _runner
from localmail.importer.job_state import ACTIVE_STATUSES


class ImportFieldError(ValueError):
    """Validation rejected a create (bad source kind / non-archive account)."""


class ImportBusyError(ValueError):
    """Another import is already pending/running (single-active busy-guard)."""


_VALID_KINDS = ("mbox", "maildir")


@dataclass(frozen=True)
class ImportJob:
    id: int
    account_id: int
    source_kind: str
    source_path: str
    status: str
    total_messages: int | None
    processed: int
    inserted: int
    skipped_dup: int
    failed: int
    error_msg: str | None
    cancel_requested: bool
    last_progress_at: datetime | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


# Selected column NAMES must match the ImportJob dataclass field names — both
# reads use psycopg `class_row`, which maps result columns to constructor
# kwargs by name, so column ORDER is irrelevant and a rename fails loudly at
# fetch time rather than silently shifting positions.
_SELECT = """
    SELECT id, account_id, source_kind, source_path, status, total_messages,
           processed, inserted, skipped_dup, failed, error_msg, cancel_requested,
           last_progress_at, created_at, started_at, finished_at
      FROM import_jobs
"""


def list_jobs(conn: psycopg.Connection) -> list[ImportJob]:
    """Every import job, newest first."""
    with conn.cursor(row_factory=class_row(ImportJob)) as cur:
        cur.execute(_SELECT + " ORDER BY id DESC")
        return cur.fetchall()


def get_job(conn: psycopg.Connection, job_id: int) -> ImportJob:
    """One job by id. Raises NotFound."""
    with conn.cursor(row_factory=class_row(ImportJob)) as cur:
        cur.execute(_SELECT + " WHERE id = %s", (job_id,))
        row = cur.fetchone()
    if row is None:
        raise NotFound(f"import job {job_id} not found")
    return row


def create_job(
    conn: psycopg.Connection, *, account_id: int, source_kind: str, source_path: str,
) -> int:
    """Insert a pending import job and return its id.

    Validates the target is an existing archive account and the source kind is
    known. The single-active busy-guard is enforced both by a pre-check and by
    the DB unique index (a concurrent racer surfaces as ImportBusyError).
    On ImportBusyError from the racing-insert path the transaction is left
    aborted (psycopg3 semantics); the caller commits on success or rolls back
    on error. The pool-connection context in the routers does this rollback
    automatically.
    """
    if source_kind not in _VALID_KINDS:
        raise ImportFieldError(f"source_kind must be one of {_VALID_KINDS}")
    account = _accounts.get_account(conn, account_id)  # raises NotFound
    if account.auth_method != "archive":
        raise ImportFieldError("imports target an archive account")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM import_jobs WHERE status = ANY(%s)",
            (list(ACTIVE_STATUSES),),
        )
        row = cur.fetchone()
        assert row is not None
        if int(row[0]) > 0:
            raise ImportBusyError("an import is already running")
        try:
            cur.execute(
                "INSERT INTO import_jobs (account_id, source_kind, source_path, status) "
                "VALUES (%s, %s, %s, 'pending') RETURNING id",
                (account_id, source_kind, source_path),
            )
        except psycopg.errors.UniqueViolation as e:
            raise ImportBusyError("an import is already running") from e
        new = cur.fetchone()
        assert new is not None
        return int(new[0])


def cancel_job(conn: psycopg.Connection, job_id: int) -> None:
    """Request cooperative cancellation of an active job. Raises NotFound."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE import_jobs SET cancel_requested = TRUE WHERE id = %s",
            (job_id,),
        )
        if cur.rowcount == 0:
            raise NotFound(f"import job {job_id} not found")


def reconcile_orphaned_jobs(conn: psycopg.Connection) -> int:
    """Mark every still-active job failed (called at serve startup).

    An in-serve worker cannot survive a process restart, so any pending/running
    row at startup is orphaned. Returns the number of jobs reconciled. Caller
    commits.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE import_jobs "
            "   SET status = 'failed', "
            "       error_msg = 'interrupted: serve process restarted', "
            "       finished_at = now() "
            " WHERE status = ANY(%s)",
            (list(ACTIVE_STATUSES),),
        )
        return cur.rowcount


def start_job(
    conn_factory: _runner.ConnFactory, job_id: int, *,
    attachments_root: Path, checkpoint_every: int,
) -> threading.Thread:
    """Spawn a daemon thread running the import. Returns the thread (joinable)."""
    t = threading.Thread(
        target=_runner.run_import,
        args=(conn_factory, job_id),
        kwargs={"attachments_root": attachments_root, "checkpoint_every": checkpoint_every},
        name=f"import-job-{job_id}",
        daemon=True,
    )
    t.start()
    return t
