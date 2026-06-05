"""Importer core: stream an archive source through sync.process_one_message.

Owns the import_jobs row lifecycle: marks running, per-message SAVEPOINT
isolation (poison pills -> failed_messages, mirroring sync_mailbox), checkpoint
counter flushes + last_progress_at heartbeat, cooperative cancel, and a
guaranteed terminal status (completed / cancelled / failed+error_msg). Takes a
`conn_factory` (not a pool) so the long-lived worker holds its own connection.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

import psycopg

from localmail.importer.sources import ImportedMessage, iter_maildir, iter_mbox
from localmail.sync import process_one_message, record_failed_message, upsert_mailbox

log = logging.getLogger(__name__)

ConnFactory = Callable[[], psycopg.Connection]


@dataclass
class _Counters:
    processed: int = 0
    inserted: int = 0
    skipped_dup: int = 0
    failed: int = 0


@dataclass
class _Job:
    account_id: int
    source_kind: str
    source_path: str


def _load_job(conn: psycopg.Connection, job_id: int) -> _Job | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT account_id, source_kind, source_path FROM import_jobs WHERE id=%s",
            (job_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return _Job(account_id=int(row[0]), source_kind=row[1], source_path=row[2])


def _mark_running(conn: psycopg.Connection, job_id: int) -> bool:
    """Flip pending->running, stamp started_at + first heartbeat. False if not pending."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE import_jobs "
            "   SET status='running', started_at=now(), last_progress_at=now() "
            " WHERE id=%s AND status='pending'",
            (job_id,),
        )
        changed = cur.rowcount == 1
    conn.commit()
    return changed


def _flush(conn: psycopg.Connection, job_id: int, c: _Counters) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE import_jobs SET processed=%s, inserted=%s, skipped_dup=%s, "
            "failed=%s, last_progress_at=now() WHERE id=%s",
            (c.processed, c.inserted, c.skipped_dup, c.failed, job_id),
        )
    conn.commit()


def _cancel_requested(conn: psycopg.Connection, job_id: int) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT cancel_requested FROM import_jobs WHERE id=%s", (job_id,))
        row = cur.fetchone()
    return bool(row and row[0])


def _mark_terminal(
    conn_factory: ConnFactory, job_id: int, status: str, c: _Counters,
    *, error_msg: str | None = None,
) -> None:
    """Write a terminal status on a FRESH connection (the worker conn may be poisoned)."""
    with conn_factory() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE import_jobs SET status=%s, processed=%s, inserted=%s, "
                "skipped_dup=%s, failed=%s, error_msg=%s, finished_at=now() WHERE id=%s",
                (status, c.processed, c.inserted, c.skipped_dup, c.failed, error_msg, job_id),
            )
        conn.commit()


def _source_iter(job: _Job) -> Iterator[ImportedMessage]:
    path = Path(job.source_path)
    if job.source_kind == "mbox":
        return iter_mbox(path, mailbox_name=path.stem)
    return iter_maildir(path)


def run_import(
    conn_factory: ConnFactory, job_id: int, *,
    attachments_root: Path, checkpoint_every: int,
) -> None:
    """Execute one import job end-to-end. Always writes a terminal status."""
    c = _Counters()
    try:
        conn = conn_factory()
    except Exception as e:  # connection failure before any work
        log.exception("import job %s: could not open connection", job_id)
        _mark_terminal(conn_factory, job_id, "failed", c, error_msg=f"{type(e).__name__}: {e}")
        return
    try:
        job = _load_job(conn, job_id)
        if job is None or not _mark_running(conn, job_id):
            conn.close()
            return
        mailbox_ids: dict[str, int] = {}
        uid_counters: dict[str, int] = {}
        cancelled = False
        for msg in _source_iter(job):
            if msg.mailbox_name not in mailbox_ids:
                mb = upsert_mailbox(
                    conn, account_id=job.account_id, name=msg.mailbox_name,
                    delimiter=None, flags=[])
                conn.commit()
                mailbox_ids[msg.mailbox_name] = mb.id
                uid_counters[msg.mailbox_name] = 0
            uid_counters[msg.mailbox_name] += 1
            uid = uid_counters[msg.mailbox_name]
            mailbox_id = mailbox_ids[msg.mailbox_name]
            with conn.cursor() as cur:
                cur.execute("SAVEPOINT msg")
            try:
                _db_id, did_insert = process_one_message(
                    conn, account_id=job.account_id, mailbox_id=mailbox_id, uid=uid,
                    raw=msg.raw, flags=[], attachments_root=attachments_root,
                    internal_date=msg.received_date)
                with conn.cursor() as cur:
                    cur.execute("RELEASE SAVEPOINT msg")
                c.inserted += 1 if did_insert else 0
                c.skipped_dup += 0 if did_insert else 1
            except Exception as exc:  # poison pill -- isolate to this message
                log.warning(
                    "import job %s: skipping poison message uid=%s mailbox=%s: %s",
                    job_id, uid, msg.mailbox_name, exc)
                with conn.cursor() as cur:
                    cur.execute("ROLLBACK TO SAVEPOINT msg")
                    cur.execute("RELEASE SAVEPOINT msg")
                record_failed_message(
                    conn, account_id=job.account_id, mailbox_id=mailbox_id, uid=uid,
                    raw=msg.raw, exc=exc)
                c.failed += 1
            c.processed += 1
            if c.processed % checkpoint_every == 0:
                _flush(conn, job_id, c)
                if _cancel_requested(conn, job_id):
                    cancelled = True
                    break
        conn.commit()
        conn.close()
        _mark_terminal(
            conn_factory, job_id, "cancelled" if cancelled else "completed", c)
    except Exception as e:
        log.exception("import job %s failed", job_id)
        try:
            conn.close()
        except Exception:
            pass
        _mark_terminal(conn_factory, job_id, "failed", c, error_msg=f"{type(e).__name__}: {e}")
