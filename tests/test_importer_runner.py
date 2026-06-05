"""End-to-end importer-core tests (real DB)."""
from __future__ import annotations

import mailbox as _mailbox
from datetime import datetime, timezone

import psycopg

from localmail.importer import runner
from tests import _eml
from tests.conftest import TEST_DSN


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
        _conn_factory, jid, attachments_root=tmp_path / "blobs", checkpoint_every=50)

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
    runner.run_import(_conn_factory, jid1, attachments_root=tmp_path / "b", checkpoint_every=50)
    with db_conn.cursor() as cur:
        cur.execute("UPDATE import_jobs SET status='completed' WHERE id=%s", (jid1,))
    db_conn.commit()
    jid2 = _job(db_conn, aid, path)
    runner.run_import(_conn_factory, jid2, attachments_root=tmp_path / "b", checkpoint_every=50)
    job = _read_job(db_conn, jid2)
    assert job["inserted"] == 0
    assert job["skipped_dup"] == 1


def test_run_import_two_messages_both_insert(db_conn, tmp_path):
    aid = _archive(db_conn)
    path = _make_mbox(tmp_path, _eml.plain(), _eml.multipart_alt())
    jid = _job(db_conn, aid, path)
    runner.run_import(_conn_factory, jid, attachments_root=tmp_path / "b", checkpoint_every=1)
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
    runner.run_import(_conn_factory, jid, attachments_root=tmp_path / "b", checkpoint_every=1)
    assert _read_job(db_conn, jid)["status"] == "cancelled"


def test_run_import_fatal_error_marks_failed(db_conn, tmp_path):
    aid = _archive(db_conn)
    jid = _job(db_conn, aid, tmp_path / "does-not-exist.mbox")
    runner.run_import(_conn_factory, jid, attachments_root=tmp_path / "b", checkpoint_every=50)
    job = _read_job(db_conn, jid)
    assert job["status"] == "failed"
    assert job["error_msg"]


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
    runner.run_import(_conn_factory, jid, attachments_root=tmp_path / "b", checkpoint_every=1)

    job = _read_job(db_conn, jid)
    assert job["status"] == "completed"
    assert job["inserted"] == 2          # the two good messages survived
    assert job["failed"] == 1            # the poison was isolated, not fatal
    # The poison message was recorded for later retry.
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM failed_messages WHERE account_id=%s", (aid,))
        assert cur.fetchone()[0] == 1
