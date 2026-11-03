# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The importer and `retry-failed` do not blame a message for contention (#437).

Sync (#414) and the embed worker / lang detection (#425) re-raise
`db.DB_CONTENTION_ERRORS`; these two per-message SAVEPOINT handlers used to
record them in `failed_messages` as if the message were malformed. Their
connections carry no timeout, so a lock wait just blocks: what raises is a
deadlock, or a lock/statement timeout an operator configured. Against the
daemon's sync threads an import shares only the global `attachment_blobs` rows
(dedup is per account, and the daemon never syncs archive accounts), but the
embed worker and lang detection lock archive `messages` rows `FOR UPDATE`, which
a new label row's foreign-key check waits on; `retry-failed` on a live account's
row can also meet it on the same Message-Id.

The error is raised server-side by `_raising_trigger` (shared with #425's
tests), so the `ROLLBACK TO SAVEPOINT` ahead of the re-raise runs for real. A
`DataException` through the same trigger is the positive control: it must still
be recorded, or a re-raise of everything would pass.
"""

from __future__ import annotations

import mailbox as _mailbox
from pathlib import Path

from click.testing import CliRunner

import psycopg
import pytest

from localmail.cli import main
from localmail.importer import runner
from localmail.sync import record_failed_message, retry_failed_messages, upsert_mailbox
from tests.conftest import TEST_DSN

from .test_worker_db_contention import _CASES, _count, _raising_trigger

# One try, no wait: these tests do not fail the terminal write (#442 does).
_TW = dict(terminal_write_attempts=1, terminal_write_backoff_s=0.0)

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
    caplog: pytest.LogCaptureFixture,
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
            checkpoint_every=50, checkpoint_seconds=3600.0, **_TW,
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
        assert error_msg.startswith(
            f"database contention; re-run the import (idempotent): {raised.__name__}: ")
        assert "DB contention at uid=1 mailbox=Inbox" in caplog.text


@pytest.mark.parametrize(("sqlstate", "raised"), _CASES)
def test_retry_failed_records_only_a_genuine_failure(
    db_conn: psycopg.Connection, tmp_path: Path, sqlstate: str, raised,
    caplog: pytest.LogCaptureFixture,
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
            assert "DB contention on failed_messages" in caplog.text
            assert f"uid=1 mailbox_id={mb.id} after 0 recovered" in caplog.text

    db_conn.rollback()
    after = _count(db_conn, "SELECT retry_count FROM failed_messages")
    assert after == (before + 1 if raised is None else before)


def _raw(n: int) -> bytes:
    return (
        f"From: a@example.com\r\nSubject: s{n}\r\n"
        f"Message-Id: <m{n}-437@example.com>\r\n\r\nbody {n}\r\n"
    ).encode()


def _seed_failed(conn: psycopg.Connection, raws: list[bytes]) -> None:
    aid = _archive_account(conn)
    mb = upsert_mailbox(conn, account_id=aid, name="Inbox", delimiter=None, flags=[])
    for uid, raw in enumerate(raws, start=1):
        record_failed_message(
            conn, account_id=aid, mailbox_id=mb.id, uid=uid, raw=raw,
            exc=ValueError("original failure"),
        )
    conn.commit()


def test_retry_keeps_rows_recovered_before_the_contended_one(
    db_conn: psycopg.Connection, tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """Each recovered row is committed before the next is tried, so a stop on
    row 2 keeps row 1 recovered and never attempts row 3."""
    _seed_failed(db_conn, [_raw(1), _raw(2), _raw(3)])
    with _raising_trigger(
        db_conn, table="messages", event="INSERT", sqlstate="57014",
        when="NEW.subject = 's2'",
    ):
        with pytest.raises(psycopg.errors.QueryCanceled):
            retry_failed_messages(db_conn, attachments_root=tmp_path / "blobs")
    assert "after 1 recovered" in caplog.text
    db_conn.rollback()
    subjects = [r[0] for r in db_conn.execute("SELECT subject FROM messages ORDER BY id")]
    assert subjects == ["s1"]
    counts = [r[0] for r in db_conn.execute(
        "SELECT retry_count FROM failed_messages ORDER BY uid")]
    assert counts == [0, 0]


def test_cli_retry_failed_reports_contention_without_a_traceback(
    db_conn: psycopg.Connection, tmp_path: Path, cli_config: Path,
) -> None:
    _seed_failed(db_conn, [_RAW])
    with _raising_trigger(db_conn, table="messages", event="INSERT", sqlstate="57014"):
        result = CliRunner().invoke(main, ["retry-failed"])
    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit)
    # One line: psycopg's str(e) would add the server's CONTEXT block.
    line = next(ln for ln in result.stderr.splitlines() if "stopped on" in ln)
    assert "stopped on database contention (QueryCanceled" in line
    assert "re-run retry-failed" in line


def test_cli_import_exits_nonzero_when_the_job_fails(
    db_conn: psycopg.Connection, tmp_path: Path, cli_config: Path,
) -> None:
    _archive_account(db_conn)
    with _raising_trigger(db_conn, table="messages", event="INSERT", sqlstate="57014"):
        result = CliRunner().invoke(
            main, ["import", _mbox(tmp_path), "--account", "arch", "--kind", "mbox"])
    assert result.exit_code == 1, result.output
    assert "status=failed" in result.stdout
    assert "error: database contention; re-run the import (idempotent): QueryCanceled: " \
        in result.stderr
