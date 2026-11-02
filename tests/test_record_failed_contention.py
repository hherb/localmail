# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Recording a poison pill does not swallow contention on its own write (#441).

`sync.record_failed_message` guards its `failed_messages` upsert with a nested
savepoint so a logging failure cannot kill the caller's transaction. When that
upsert itself hit a `db.DB_CONTENTION_ERRORS` class, the catch swallowed it:
the sync daemon then advanced `uidnext` past a message recorded nowhere (lost
until a manual rewind), an import counted it `failed` with no row, and
`retry-failed` counted it `still_failing` with its row untouched. Each caller
already handles contention correctly when it arrives from the message itself;
the fix routes this one to the same handling.

The message is made a poison pill by patching `upsert_message`; the contention
is raised server-side on the `failed_messages` INSERT by `_raising_trigger`
(#425), so the `ROLLBACK TO SAVEPOINT fail_log` runs for real. A
`DataException` there is the positive control: a non-contention failure to log
is still swallowed, as before.
"""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from localmail import sync as sync_mod
from localmail.importer import runner
from localmail.sync import record_failed_message, retry_failed_messages, upsert_mailbox

from . import _eml
from ._fake_imap import FakeIMAPClient
from .test_import_retry_db_contention import (
    _RAW,
    _archive_account,
    _conn_factory,
    _mbox,
)
from .test_sync import _sync, make_account
from .test_worker_db_contention import _CASES, _count, _raising_trigger


@pytest.fixture
def poison(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*a, **kw):
        raise ValueError("not a real message")

    monkeypatch.setattr(sync_mod, "upsert_message", broken)


@pytest.mark.parametrize(("sqlstate", "raised"), _CASES)
def test_record_failed_message_reraises_only_contention(
    db_conn: psycopg.Connection, sqlstate: str, raised,
) -> None:
    aid = _archive_account(db_conn)
    mb = upsert_mailbox(db_conn, account_id=aid, name="Inbox", delimiter=None, flags=[])
    db_conn.commit()
    with _raising_trigger(db_conn, table="failed_messages", event="INSERT", sqlstate=sqlstate):
        if raised is None:
            record_failed_message(
                db_conn, account_id=aid, mailbox_id=mb.id, uid=1, raw=_RAW,
                exc=ValueError("x"))
            # The caller's transaction survives the swallowed failure.
            assert _count(db_conn, "SELECT count(*) FROM mailboxes") == 1
        else:
            with pytest.raises(raised):
                record_failed_message(
                    db_conn, account_id=aid, mailbox_id=mb.id, uid=1, raw=_RAW,
                    exc=ValueError("x"))
            # Rolled back to its own savepoint, so the transaction is usable.
            assert _count(db_conn, "SELECT count(*) FROM mailboxes") == 1


@pytest.mark.parametrize(("sqlstate", "raised"), _CASES)
def test_sync_does_not_advance_past_an_unrecorded_poison_pill(
    db_conn: psycopg.Connection, tmp_path: Path, poison, sqlstate: str, raised,
) -> None:
    imap = FakeIMAPClient()
    imap.add_folder("INBOX")
    imap.append("INBOX", _eml.plain())
    with _raising_trigger(db_conn, table="failed_messages", event="INSERT", sqlstate=sqlstate):
        if raised is None:
            _sync(db_conn, imap, account=make_account(), attachments_root=tmp_path)
        else:
            with pytest.raises(raised):
                _sync(db_conn, imap, account=make_account(), attachments_root=tmp_path)
    db_conn.rollback()
    advanced = _count(db_conn, "SELECT count(*) FROM mailboxes WHERE uidnext > 1")
    # Swallowed (control): skipped as before. Contended: the next pass re-fetches it.
    assert advanced == (1 if raised is None else 0)


@pytest.mark.parametrize(("sqlstate", "raised"), _CASES)
def test_import_fails_the_job_when_the_poison_pill_cannot_be_recorded(
    db_conn: psycopg.Connection, tmp_path: Path, poison, sqlstate: str, raised,
) -> None:
    aid = _archive_account(db_conn)
    row = db_conn.execute(
        "INSERT INTO import_jobs (account_id, source_kind, source_path, status) "
        "VALUES (%s, 'mbox', %s, 'pending') RETURNING id",
        (aid, _mbox(tmp_path)),
    ).fetchone()
    db_conn.commit()
    assert row is not None
    job_id = int(row[0])

    with _raising_trigger(db_conn, table="failed_messages", event="INSERT", sqlstate=sqlstate):
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
    if raised is None:
        assert (status, failed) == ("completed", 1)
    else:
        assert (status, failed) == ("failed", 0)
        assert error_msg.startswith(
            f"database contention; re-run the import (idempotent): {raised.__name__}: ")


@pytest.mark.parametrize(("sqlstate", "raised"), _CASES)
def test_retry_failed_stops_when_the_rerecord_is_contended(
    db_conn: psycopg.Connection, tmp_path: Path, poison, sqlstate: str, raised,
) -> None:
    aid = _archive_account(db_conn)
    mb = upsert_mailbox(db_conn, account_id=aid, name="Inbox", delimiter=None, flags=[])
    record_failed_message(
        db_conn, account_id=aid, mailbox_id=mb.id, uid=1, raw=_RAW,
        exc=ValueError("original failure"))
    db_conn.commit()

    with _raising_trigger(db_conn, table="failed_messages", event="INSERT", sqlstate=sqlstate):
        if raised is None:
            assert retry_failed_messages(db_conn, attachments_root=tmp_path / "blobs") == (0, 1)
        else:
            with pytest.raises(raised):
                retry_failed_messages(db_conn, attachments_root=tmp_path / "blobs")
    db_conn.rollback()
    assert _count(db_conn, "SELECT retry_count FROM failed_messages") == 0
