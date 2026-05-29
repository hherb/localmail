"""Tests for the IDLE + poll session functions used by the daemon."""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from psycopg_pool import ConnectionPool

from localmail import idle as idle_mod
from localmail import poller as poll_mod
from localmail.config import AccountConfig
from localmail.idle import _idle_step, _one_inbox_session
from localmail.poller import _one_poll_pass
from localmail.sync import upsert_account, upsert_mailbox
from localmail.worker import WorkerContext

from . import _eml
from ._fake_imap import FakeIMAPClient


def make_account() -> AccountConfig:
    return AccountConfig(
        name="acct",
        email="me@example.com",
        imap_host="imap.example.com",
        imap_port=993,
        auth_method="password",
    )


def make_ctx(
    pool: ConnectionPool,
    tmp_path: Path,
    stop: threading.Event,
    *,
    account: AccountConfig | None = None,
) -> WorkerContext:
    account = account or make_account()
    with pool.connection() as conn:
        account_id = upsert_account(conn, account)
        conn.commit()
    return WorkerContext(
        account=account,
        account_id=account_id,
        pool=pool,
        attachments_root=tmp_path,
        idle_renew_seconds=60,
        poll_seconds=1,
        gmail_client_secrets=None,
        stop=stop,
        ssl=False,
    )


@pytest.fixture
def pool(db_dsn):
    p = ConnectionPool(conninfo=db_dsn, min_size=1, max_size=4, open=True)
    with p.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "TRUNCATE accounts, mailboxes, messages, message_labels, "
                "attachment_blobs, failed_messages RESTART IDENTITY CASCADE"
            )
        conn.commit()
    yield p
    p.close()


@contextmanager
def _fake_open_connection(imap, account, **kw):  # noqa: ARG001
    yield imap


# --- _idle_step --------------------------------------------------------------


def test_idle_step_returns_same_deadline_when_nothing_happens(pool, tmp_path: Path):
    imap = FakeIMAPClient()
    imap.add_folder("INBOX")
    imap.idle()

    with pool.connection() as conn:
        account_id = upsert_account(conn, make_account())
        upsert_mailbox(conn, account_id=account_id, name="INBOX", delimiter=None, flags=[])
        conn.commit()

    ctx = make_ctx(pool, tmp_path, threading.Event())
    renew_at = time.monotonic() + 60.0
    new_renew = _idle_step(ctx, imap, account_id, renew_at)
    assert new_renew == renew_at  # nothing happened, deadline unchanged
    assert imap.idle_done_call_count == 0


def test_idle_step_syncs_and_re_idles_on_new_mail(pool, tmp_path: Path):
    imap = FakeIMAPClient()
    imap.add_folder("INBOX")
    imap.append("INBOX", _eml.plain())
    imap.select_folder("INBOX")
    imap.idle()
    imap.simulate_new_mail(1)

    with pool.connection() as conn:
        account_id = upsert_account(conn, make_account())
        upsert_mailbox(conn, account_id=account_id, name="INBOX", delimiter=None, flags=[])
        conn.commit()

    ctx = make_ctx(pool, tmp_path, threading.Event())
    renew_at = time.monotonic() + 60.0

    new_renew = _idle_step(ctx, imap, account_id, renew_at)

    assert imap.idle_done_call_count == 1
    assert imap.idle_call_count == 2  # one initial + one re-issue after sync
    assert new_renew > renew_at - 1  # deadline reset

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM messages")
        assert cur.fetchone()[0] == 1


def test_idle_step_renews_idle_when_deadline_passed(pool, tmp_path: Path):
    imap = FakeIMAPClient()
    imap.add_folder("INBOX")
    imap.select_folder("INBOX")
    imap.idle()

    with pool.connection() as conn:
        account_id = upsert_account(conn, make_account())
        upsert_mailbox(conn, account_id=account_id, name="INBOX", delimiter=None, flags=[])
        conn.commit()

    ctx = make_ctx(pool, tmp_path, threading.Event())
    # Deadline already past.
    new_renew = _idle_step(ctx, imap, account_id, renew_at=time.monotonic() - 1)

    assert imap.idle_done_call_count == 1
    assert imap.idle_call_count == 2  # initial + renewed
    assert new_renew > time.monotonic()


# --- _one_inbox_session (full lifecycle) -------------------------------------


def test_one_inbox_session_syncs_backlog_then_idles(
    pool, tmp_path: Path, monkeypatch
):
    imap = FakeIMAPClient()
    imap.add_folder("INBOX")
    imap.append("INBOX", _eml.plain())  # backlog from when daemon was down

    @contextmanager
    def fake_open(account, **kw):  # noqa: ARG001
        yield imap

    monkeypatch.setattr(idle_mod, "open_connection", fake_open)

    stop = threading.Event()
    ctx = make_ctx(pool, tmp_path, stop)
    # Make the IDLE loop exit on the first iteration.
    stop.set()

    _one_inbox_session(ctx)

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM messages")
        assert cur.fetchone()[0] == 1  # backlog was synced before entering IDLE


# --- _one_poll_pass ----------------------------------------------------------


def test_one_poll_pass_syncs_every_non_inbox_folder(pool, tmp_path: Path, monkeypatch):
    imap = FakeIMAPClient()
    imap.add_folder("INBOX")
    imap.add_folder("Archive")
    imap.add_folder("Receipts")
    imap.append("INBOX", _eml.plain())  # should be SKIPPED by poll loop
    imap.append("Archive", _eml.multipart_alt())
    imap.append("Receipts", _eml.with_attachment())

    @contextmanager
    def fake_open(account, **kw):  # noqa: ARG001
        yield imap

    monkeypatch.setattr(poll_mod, "open_connection", fake_open)

    stop = threading.Event()
    ctx = make_ctx(pool, tmp_path, stop)
    results = _one_poll_pass(ctx)

    # INBOX is owned by the IDLE thread; poll must skip it.
    assert "INBOX" not in results
    assert results == {"Archive": 1, "Receipts": 1}

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM messages")
        assert cur.fetchone()[0] == 2


def test_one_poll_pass_respects_folder_deny_flags(pool, tmp_path: Path, monkeypatch):
    imap = FakeIMAPClient()
    imap.add_folder("INBOX")
    f = imap.add_folder("[Gmail]/Bin")
    f.flags = ("\\HasNoChildren", "\\Trash")
    imap.append("[Gmail]/Bin", _eml.plain())

    @contextmanager
    def fake_open(account, **kw):  # noqa: ARG001
        yield imap

    monkeypatch.setattr(poll_mod, "open_connection", fake_open)

    account = AccountConfig(
        name="acct", email="me@example.com", imap_host="imap.example.com",
        auth_method="password", folder_deny_flags=["\\Trash"],
    )
    ctx = make_ctx(pool, tmp_path, threading.Event(), account=account)
    results = _one_poll_pass(ctx)
    assert results == {}  # Bin was denied, INBOX is owned by IDLE


def test_one_poll_pass_stops_early_when_stop_event_set(pool, tmp_path: Path, monkeypatch):
    imap = FakeIMAPClient()
    imap.add_folder("INBOX")
    imap.add_folder("a")
    imap.add_folder("b")
    imap.append("a", _eml.plain())
    imap.append("b", _eml.multipart_alt())

    @contextmanager
    def fake_open(account, **kw):  # noqa: ARG001
        yield imap

    monkeypatch.setattr(poll_mod, "open_connection", fake_open)

    stop = threading.Event()
    stop.set()  # already stopped before any folder is processed
    ctx = make_ctx(pool, tmp_path, stop)

    results = _one_poll_pass(ctx)
    assert results == {}
