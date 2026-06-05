"""Service-layer tests for admin imports (real DB)."""
from __future__ import annotations

import mailbox as _mailbox

import psycopg
import pytest

from localmail.api.admin import imports as svc
from localmail.api.errors import NotFound
from tests import _eml
from tests.conftest import TEST_DSN


def _account(conn, name, auth="archive"):
    host = "NULL" if auth == "archive" else "'h'"
    port = "NULL" if auth == "archive" else "993"
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO accounts (name, email_address, auth_method, imap_host, "
            f"imap_port, config) VALUES (%s, 'a@b.test', %s, {host}, {port}, '{{}}') "
            f"RETURNING id",
            (name, auth),
        )
        row = cur.fetchone()
    conn.commit()
    assert row is not None
    return int(row[0])


def test_create_and_list_job(db_conn):
    aid = _account(db_conn, "arch")
    jid = svc.create_job(
        db_conn, account_id=aid, source_kind="mbox", source_path="/srv/a.mbox")
    db_conn.commit()
    jobs = svc.list_jobs(db_conn)
    assert [j.id for j in jobs] == [jid]
    assert jobs[0].status == "pending"
    assert jobs[0].account_id == aid


def test_create_rejects_non_archive_account(db_conn):
    aid = _account(db_conn, "live", auth="password")
    with pytest.raises(svc.ImportFieldError):
        svc.create_job(
            db_conn, account_id=aid, source_kind="mbox", source_path="/srv/a.mbox")


def test_create_rejects_unknown_account(db_conn):
    with pytest.raises(NotFound):
        svc.create_job(
            db_conn, account_id=9999, source_kind="mbox", source_path="/x")


def test_create_rejects_bad_source_kind(db_conn):
    aid = _account(db_conn, "arch")
    with pytest.raises(svc.ImportFieldError):
        svc.create_job(
            db_conn, account_id=aid, source_kind="zip", source_path="/x")


def test_busy_guard_second_create_raises(db_conn):
    aid = _account(db_conn, "arch")
    svc.create_job(db_conn, account_id=aid, source_kind="mbox", source_path="/a")
    db_conn.commit()
    with pytest.raises(svc.ImportBusyError):
        svc.create_job(db_conn, account_id=aid, source_kind="mbox", source_path="/b")


def test_get_job_not_found(db_conn):
    with pytest.raises(NotFound):
        svc.get_job(db_conn, 12345)


def test_cancel_sets_flag(db_conn):
    aid = _account(db_conn, "arch")
    jid = svc.create_job(db_conn, account_id=aid, source_kind="mbox", source_path="/a")
    db_conn.commit()
    svc.cancel_job(db_conn, jid)
    db_conn.commit()
    assert svc.get_job(db_conn, jid).cancel_requested is True


def test_cancel_unknown_raises(db_conn):
    with pytest.raises(NotFound):
        svc.cancel_job(db_conn, 4242)


def test_reconcile_orphaned_marks_active_failed(db_conn):
    aid = _account(db_conn, "arch")
    jid = svc.create_job(db_conn, account_id=aid, source_kind="mbox", source_path="/a")
    with db_conn.cursor() as cur:
        cur.execute("UPDATE import_jobs SET status='running' WHERE id=%s", (jid,))
    db_conn.commit()
    n = svc.reconcile_orphaned_jobs(db_conn)
    db_conn.commit()
    assert n == 1
    job = svc.get_job(db_conn, jid)
    assert job.status == "failed"
    assert "interrupted" in (job.error_msg or "")


def test_start_job_runs_to_completion(db_conn, tmp_path):
    aid = _account(db_conn, "arch")
    p = tmp_path / "a.mbox"
    box = _mailbox.mbox(str(p))
    box.lock()
    box.add(_mailbox.mboxMessage(_eml.plain()))
    box.flush()
    box.unlock()
    jid = svc.create_job(
        db_conn, account_id=aid, source_kind="mbox", source_path=str(p))
    db_conn.commit()

    t = svc.start_job(
        lambda: psycopg.connect(TEST_DSN, autocommit=False), jid,
        attachments_root=tmp_path / "blobs", checkpoint_every=50, checkpoint_seconds=3600)
    t.join(timeout=30)
    assert not t.is_alive()
    assert svc.get_job(db_conn, jid).status == "completed"
