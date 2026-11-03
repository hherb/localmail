# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""End-to-end importer-core tests (real DB)."""
from __future__ import annotations

import mailbox as _mailbox
from datetime import datetime, timezone

import psycopg

from localmail.importer import runner
from tests import _eml
from tests.conftest import TEST_DSN

# One try, no wait: these tests do not fail the terminal write (#442 does).
_TW = dict(terminal_write_attempts=1, terminal_write_backoff_s=0.0)


def _conn_factory():
    return psycopg.connect(TEST_DSN, autocommit=False)


def _archive(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, auth_method, imap_host, "
            "imap_port, config) "
            "VALUES ('arch', 'a@b.test', 'archive', NULL, NULL, '{}') RETURNING id"
        )
        row = cur.fetchone()
    conn.commit()
    assert row is not None
    return int(row[0])


def _job(conn, account_id, path, kind="mbox") -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO import_jobs (account_id, source_kind, source_path, status) "
            "VALUES (%s, %s, %s, 'pending') RETURNING id",
            (account_id, kind, str(path)),
        )
        row = cur.fetchone()
    conn.commit()
    assert row is not None
    return int(row[0])


def _make_mbox(tmp_path, *messages) -> str:
    p = tmp_path / "a.mbox"
    box = _mailbox.mbox(str(p))
    box.lock()
    for raw in messages:
        m = _mailbox.mboxMessage(raw)
        m.set_from("alice@example.com Wed Jan  1 12:00:00 2025")
        box.add(m)
    box.flush()
    box.unlock()
    return str(p)


def _read_job(conn, jid) -> dict:
    conn.rollback()  # discard any snapshot; re-read fresh
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, inserted, skipped_dup, failed, error_msg "
            "FROM import_jobs WHERE id=%s", (jid,))
        s, ins, skip, fail, err = cur.fetchone()
    return {"status": s, "inserted": ins, "skipped_dup": skip, "failed": fail, "error_msg": err}


def test_run_import_inserts_messages_with_received_date(db_conn, tmp_path):
    aid = _archive(db_conn)
    path = _make_mbox(tmp_path, _eml.plain())
    jid = _job(db_conn, aid, path)

    runner.run_import(
        _conn_factory, jid, attachments_root=tmp_path / "blobs", checkpoint_every=50, checkpoint_seconds=3600, **_TW)

    job = _read_job(db_conn, jid)
    assert job["status"] == "completed"
    assert job["inserted"] == 1
    with db_conn.cursor() as cur:
        cur.execute("SELECT internal_date FROM messages WHERE account_id=%s", (aid,))
        (internal_date,) = cur.fetchone()
    assert internal_date == datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_run_import_reimport_is_idempotent(db_conn, tmp_path):
    aid = _archive(db_conn)
    path = _make_mbox(tmp_path, _eml.plain())
    jid1 = _job(db_conn, aid, path)
    runner.run_import(_conn_factory, jid1, attachments_root=tmp_path / "b", checkpoint_every=50, checkpoint_seconds=3600, **_TW)
    with db_conn.cursor() as cur:
        cur.execute("UPDATE import_jobs SET status='completed' WHERE id=%s", (jid1,))
    db_conn.commit()
    jid2 = _job(db_conn, aid, path)
    runner.run_import(_conn_factory, jid2, attachments_root=tmp_path / "b", checkpoint_every=50, checkpoint_seconds=3600, **_TW)
    job = _read_job(db_conn, jid2)
    assert job["inserted"] == 0
    assert job["skipped_dup"] == 1


def test_run_import_two_messages_both_insert(db_conn, tmp_path):
    aid = _archive(db_conn)
    path = _make_mbox(tmp_path, _eml.plain(), _eml.multipart_alt())
    jid = _job(db_conn, aid, path)
    runner.run_import(_conn_factory, jid, attachments_root=tmp_path / "b", checkpoint_every=1, checkpoint_seconds=3600, **_TW)
    job = _read_job(db_conn, jid)
    assert job["inserted"] == 2
    assert job["failed"] == 0


def test_run_import_cancel_stops(db_conn, tmp_path):
    aid = _archive(db_conn)
    path = _make_mbox(tmp_path, _eml.plain(), _eml.multipart_alt(), _eml.utf8_subject())
    jid = _job(db_conn, aid, path)
    with db_conn.cursor() as cur:
        cur.execute("UPDATE import_jobs SET cancel_requested=TRUE WHERE id=%s", (jid,))
    db_conn.commit()
    runner.run_import(_conn_factory, jid, attachments_root=tmp_path / "b", checkpoint_every=1, checkpoint_seconds=3600, **_TW)
    assert _read_job(db_conn, jid)["status"] == "cancelled"


def test_run_import_fatal_error_marks_failed(db_conn, tmp_path):
    aid = _archive(db_conn)
    jid = _job(db_conn, aid, tmp_path / "does-not-exist.mbox")
    runner.run_import(_conn_factory, jid, attachments_root=tmp_path / "b", checkpoint_every=50, checkpoint_seconds=3600, **_TW)
    job = _read_job(db_conn, jid)
    assert job["status"] == "failed"
    assert job["error_msg"]


def test_run_import_flushes_after_first_message(db_conn, tmp_path, monkeypatch):
    # A small import (< checkpoint_every) must flush at least once before the
    # terminal write, so the panel shows progress instead of 0/0/0/0 (issue #163).
    aid = _archive(db_conn)
    path = _make_mbox(tmp_path, _eml.plain())  # one message, well under checkpoint_every
    jid = _job(db_conn, aid, path)

    flushes = {"n": 0}
    real_flush = runner._flush

    def counting_flush(*a, **k):
        flushes["n"] += 1
        return real_flush(*a, **k)

    monkeypatch.setattr(runner, "_flush", counting_flush)
    runner.run_import(
        _conn_factory, jid, attachments_root=tmp_path / "b",
        checkpoint_every=50, checkpoint_seconds=3600, **_TW)

    assert flushes["n"] >= 1  # the first-message flush fired despite count < 50
    assert _read_job(db_conn, jid)["status"] == "completed"


def test_run_import_small_import_is_cancellable(db_conn, tmp_path):
    # Cancel must be honoured even for an import smaller than checkpoint_every:
    # the first-message poll picks up the request (issue #163).
    aid = _archive(db_conn)
    path = _make_mbox(tmp_path, _eml.plain())  # one message, checkpoint_every=50
    jid = _job(db_conn, aid, path)
    with db_conn.cursor() as cur:
        cur.execute("UPDATE import_jobs SET cancel_requested=TRUE WHERE id=%s", (jid,))
    db_conn.commit()

    runner.run_import(
        _conn_factory, jid, attachments_root=tmp_path / "b",
        checkpoint_every=50, checkpoint_seconds=3600, **_TW)

    assert _read_job(db_conn, jid)["status"] == "cancelled"


def test_run_import_time_cadence_flushes_between_count_boundaries(db_conn, tmp_path, monkeypatch):
    # Count cadence never fires (checkpoint_every huge), but an injected clock
    # makes each message exceed checkpoint_seconds, so every message flushes.
    aid = _archive(db_conn)
    path = _make_mbox(tmp_path, _eml.plain(), _eml.multipart_alt(), _eml.utf8_subject())
    jid = _job(db_conn, aid, path)

    ticks = iter(range(0, 100, 5))  # 0, 5, 10, ... — +5s per clock() call

    def fake_clock() -> float:
        return float(next(ticks))

    flushes = {"n": 0}
    real_flush = runner._flush

    def counting_flush(*a, **k):
        flushes["n"] += 1
        return real_flush(*a, **k)

    monkeypatch.setattr(runner, "_flush", counting_flush)
    runner.run_import(
        _conn_factory, jid, attachments_root=tmp_path / "b",
        checkpoint_every=10_000, checkpoint_seconds=2, clock=fake_clock, **_TW)

    # First message (first-message rule) + two more by the 5s>2s time cadence.
    assert flushes["n"] == 3
    assert _read_job(db_conn, jid)["status"] == "completed"


def test_run_import_poison_message_isolated(db_conn, tmp_path, monkeypatch):
    aid = _archive(db_conn)
    path = _make_mbox(tmp_path, _eml.plain(), _eml.multipart_alt(), _eml.utf8_subject())
    jid = _job(db_conn, aid, path)

    real = runner.process_one_message
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:  # poison the 2nd message only
            raise ValueError("synthetic poison")
        return real(*args, **kwargs)

    monkeypatch.setattr(runner, "process_one_message", flaky)
    runner.run_import(_conn_factory, jid, attachments_root=tmp_path / "b", checkpoint_every=1, checkpoint_seconds=3600, **_TW)

    job = _read_job(db_conn, jid)
    assert job["status"] == "completed"
    assert job["inserted"] == 2          # the two good messages survived
    assert job["failed"] == 1            # the poison was isolated, not fatal
    # The poison message was recorded for later retry.
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM failed_messages WHERE account_id=%s", (aid,))
        assert cur.fetchone()[0] == 1


def _numbered(n: int) -> bytes:
    return (
        f"From: a@example.com\r\nSubject: m{n}\r\n"
        f"Message-Id: <m{n}-439@example.com>\r\n\r\nbody {n}\r\n"
    ).encode()


def test_a_failed_job_reports_only_what_was_committed(db_conn, tmp_path, monkeypatch):
    """#439: on a mid-run failure the job's counters describe the committed
    work, not work the closing connection rolled back.

    Message 1 is inserted and committed by the first-message checkpoint;
    message 2 is a poison pill, recorded but not yet committed; message 3
    fails the job. The rollback takes message 2's failed_messages row with
    it, so the job must report processed=1, inserted=1, failed=0.
    """
    aid = _archive(db_conn)
    path = _make_mbox(tmp_path, _numbered(1), _numbered(2), _numbered(3))
    jid = _job(db_conn, aid, path)
    real = runner.process_one_message

    def scripted(conn, *, raw, **kw):
        if b"Subject: m2" in raw:
            raise ValueError("poison")
        if b"Subject: m3" in raw:
            raise psycopg.errors.DeadlockDetected("contended")
        return real(conn, raw=raw, **kw)

    monkeypatch.setattr(runner, "process_one_message", scripted)
    runner.run_import(
        _conn_factory, jid, attachments_root=tmp_path / "blobs",
        checkpoint_every=50, checkpoint_seconds=3600.0, **_TW)

    db_conn.rollback()
    row = db_conn.execute(
        "SELECT status, processed, inserted, skipped_dup, failed FROM import_jobs "
        "WHERE id=%s", (jid,)).fetchone()
    assert row == ("failed", 1, 1, 0, 0)
    counts = db_conn.execute(
        "SELECT (SELECT count(*) FROM messages), (SELECT count(*) FROM failed_messages)"
    ).fetchone()
    assert counts == (1, 0)


def test_a_new_mailbox_commit_counts_as_committed_work(db_conn, tmp_path, monkeypatch):
    """#439: first sighting of a mailbox commits too, which also commits the
    messages processed before it. Root `md` holds m1 and m2, one flushed by
    the first-message checkpoint and the other not; entering `Archive`
    commits it; m3 there fails the job. Both committed messages must be
    reported."""
    md = _mailbox.Maildir(str(tmp_path / "md"))
    md.add(_mailbox.MaildirMessage(_numbered(1)))
    md.add(_mailbox.MaildirMessage(_numbered(2)))
    md.add_folder("Archive").add(_mailbox.MaildirMessage(_numbered(3)))
    aid = _archive(db_conn)
    jid = _job(db_conn, aid, tmp_path / "md", kind="maildir")
    real = runner.process_one_message

    def scripted(conn, *, raw, **kw):
        if b"Subject: m3" in raw:
            raise psycopg.errors.DeadlockDetected("contended")
        return real(conn, raw=raw, **kw)

    monkeypatch.setattr(runner, "process_one_message", scripted)
    runner.run_import(
        _conn_factory, jid, attachments_root=tmp_path / "blobs",
        checkpoint_every=50, checkpoint_seconds=3600.0, **_TW)

    db_conn.rollback()
    row = db_conn.execute(
        "SELECT status, processed, inserted FROM import_jobs WHERE id=%s", (jid,),
    ).fetchone()
    assert row == ("failed", 2, 2)
    assert db_conn.execute("SELECT count(*) FROM messages").fetchone() == (2,)


def test_a_failure_after_the_final_commit_reports_every_message(
    db_conn, tmp_path,
):
    """#439: once the loop's final commit has run every message is committed,
    so a failure after it must not report the older checkpoint's counts. Since
    #442 the one such failure that reaches the handler is the worker
    connection's close(); the "completed" write is retried on its own."""
    aid = _archive(db_conn)
    path = _make_mbox(tmp_path, _numbered(1), _numbered(2))
    jid = _job(db_conn, aid, path)
    factory_calls = [0]

    def factory():
        conn = _conn_factory()
        factory_calls[0] += 1
        if factory_calls[0] == 1:  # the worker connection
            real_close = conn.close

            def failing_close():
                conn.close = real_close  # the handler's retry closes it for real
                raise psycopg.OperationalError("close failed")
            conn.close = failing_close
        return conn

    runner.run_import(
        factory, jid, attachments_root=tmp_path / "blobs",
        checkpoint_every=50, checkpoint_seconds=3600.0, **_TW)

    db_conn.rollback()
    row = db_conn.execute(
        "SELECT status, processed, inserted FROM import_jobs WHERE id=%s", (jid,),
    ).fetchone()
    assert row == ("failed", 2, 2)
