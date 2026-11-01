# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The importer and `retry-failed` do not blame a message for contention (#437).

Sync (#414) and the embed worker / lang detection (#425) re-raise
`db.DB_CONTENTION_ERRORS`; these two per-message SAVEPOINT handlers used to
record them in `failed_messages` as if the message were malformed. The likely
trigger is an `ON CONFLICT DO NOTHING` waiting behind the daemon's uncommitted
batch for the same Message-Id or `attachment_blobs` row.

The error is raised server-side by `_raising_trigger` (shared with #425's
tests), so the `ROLLBACK TO SAVEPOINT` ahead of the re-raise runs for real. A
`DataException` through the same trigger is the positive control: it must still
be recorded, or a re-raise of everything would pass.
"""

from __future__ import annotations

import mailbox as _mailbox
from pathlib import Path

import psycopg
import pytest

from localmail.importer import runner
from localmail.sync import record_failed_message, retry_failed_messages, upsert_mailbox
from tests.conftest import TEST_DSN

from .test_worker_db_contention import _CASES, _count, _raising_trigger

_RAW = (
    b"From: alice@example.com\r\nTo: bob@example.com\r\nSubject: contended\r\n"
    b"Date: Wed, 01 Jan 2025 12:00:00 +0000\r\n"
    b"Message-Id: <contended-437@example.com>\r\n\r\nbody\r\n"
)


def _conn_factory() -> psycopg.Connection:
    return psycopg.connect(TEST_DSN, autocommit=False)


def _archive_account(conn: psycopg.Connection) -> int:
    row = conn.execute(
        "INSERT INTO accounts (name, email_address, auth_method, imap_host, "
        "imap_port, config) VALUES ('arch', 'a@b.test', 'archive', NULL, NULL, '{}') "
        "RETURNING id"
    ).fetchone()
    conn.commit()
    assert row is not None
    return int(row[0])


def _mbox(tmp_path: Path) -> str:
    path = tmp_path / "Inbox.mbox"
    box = _mailbox.mbox(str(path))
    box.lock()
    m = _mailbox.mboxMessage(_RAW)
    m.set_from("alice@example.com Wed Jan  1 12:00:00 2025")
    box.add(m)
    box.flush()
    box.unlock()
    return str(path)


@pytest.mark.parametrize(("sqlstate", "raised"), _CASES)
def test_import_records_only_a_genuine_failure(
    db_conn: psycopg.Connection, tmp_path: Path, sqlstate: str, raised,
) -> None:
    """A contended message fails the job (re-run it) instead of being counted
    `failed` and written to `failed_messages`."""
    aid = _archive_account(db_conn)
    row = db_conn.execute(
        "INSERT INTO import_jobs (account_id, source_kind, source_path, status) "
        "VALUES (%s, 'mbox', %s, 'pending') RETURNING id",
        (aid, _mbox(tmp_path)),
    ).fetchone()
    db_conn.commit()
    assert row is not None
    job_id = int(row[0])

    with _raising_trigger(db_conn, table="messages", event="INSERT", sqlstate=sqlstate):
        runner.run_import(
            _conn_factory, job_id, attachments_root=tmp_path / "blobs",
            checkpoint_every=50, checkpoint_seconds=3600.0,
        )

    db_conn.rollback()
    job = db_conn.execute(
        "SELECT status, failed, error_msg FROM import_jobs WHERE id = %s", (job_id,),
    ).fetchone()
    assert job is not None
    status, failed, error_msg = job
    recorded = _count(db_conn, "SELECT count(*) FROM failed_messages")
    if raised is None:
        assert (status, failed, recorded) == ("completed", 1, 1)
    else:
        assert (status, failed, recorded) == ("failed", 0, 0)
        assert error_msg.startswith(f"{raised.__name__}: ")


@pytest.mark.parametrize(("sqlstate", "raised"), _CASES)
def test_retry_failed_records_only_a_genuine_failure(
    db_conn: psycopg.Connection, tmp_path: Path, sqlstate: str, raised,
) -> None:
    """A contended retry raises and leaves the row's `retry_count` alone."""
    aid = _archive_account(db_conn)
    mb = upsert_mailbox(db_conn, account_id=aid, name="Inbox", delimiter=None, flags=[])
    record_failed_message(
        db_conn, account_id=aid, mailbox_id=mb.id, uid=1, raw=_RAW,
        exc=ValueError("original failure"),
    )
    db_conn.commit()
    before = _count(db_conn, "SELECT retry_count FROM failed_messages")

    with _raising_trigger(db_conn, table="messages", event="INSERT", sqlstate=sqlstate):
        if raised is None:
            outcome = retry_failed_messages(db_conn, attachments_root=tmp_path / "blobs")
            assert outcome == (0, 1)
        else:
            with pytest.raises(raised):
                retry_failed_messages(db_conn, attachments_root=tmp_path / "blobs")

    db_conn.rollback()
    after = _count(db_conn, "SELECT retry_count FROM failed_messages")
    assert after == (before + 1 if raised is None else before)
