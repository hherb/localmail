# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Heartbeat wiring: reconcile writes a heartbeat; startup clears stale rows."""
from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg_pool import ConnectionPool

import localmail.daemon as daemon_mod
from localmail import idle as idle_mod
from localmail import poller as poll_mod
from localmail.account_seed import account_create_kwargs
from localmail.api.admin.accounts import (
    create_account,
    get_account_by_name,
    list_syncable_accounts,
)
from localmail.config import AccountConfig, LocalmailConfig
from localmail.daemon import Daemon
from localmail.heartbeat import record_heartbeat
from localmail.idle import _one_inbox_session
from localmail.poller import _one_poll_pass
from localmail.worker import WorkerContext

from ._fake_imap import FakeIMAPClient


def _cfg(db_dsn: str) -> LocalmailConfig:
    cfg = LocalmailConfig.model_validate({"database": {"dsn": db_dsn}})
    cfg.search.run_embed_worker = False
    cfg.search.run_extract_worker = False
    return cfg


def _heartbeat_kinds(dsn: str) -> set[str]:
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT worker_kind FROM daemon_heartbeats")
            return {r[0] for r in cur.fetchall()}


def _truncate(dsn: str) -> None:
    # db_dsn only applies migrations (session-scoped); it never truncates, so
    # leftover `accounts` rows from earlier tests would make start_workers /
    # reconcile spawn IMAP threads. Clear both tables for a deterministic run.
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "TRUNCATE accounts, daemon_heartbeats RESTART IDENTITY CASCADE"
        )
        conn.commit()


def test_reconcile_records_reconcile_heartbeat(db_dsn: str) -> None:
    _truncate(db_dsn)
    d = Daemon(_cfg(db_dsn), ssl=False, stop_event=threading.Event())
    try:
        d.reconcile()
        assert "reconcile" in _heartbeat_kinds(db_dsn)
    finally:
        d.stop()
        d.pool.close()


# --- idle/poll loop heartbeat wiring (spy on safe_heartbeat) -----------------


class _HBSpy:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def __call__(self, pool, *, worker_kind, account_id, state,
                 current_folder=None, last_error_msg=None) -> None:
        self.calls.append((worker_kind, state, current_folder))


def _wiring_account() -> AccountConfig:
    return AccountConfig(
        name="acct",
        email="me@example.com",
        imap_host="imap.example.com",
        imap_port=993,
        auth_method="password",
    )


def _ensure_account(conn, account: AccountConfig) -> int:
    existing = get_account_by_name(conn, account.name)
    if existing is not None:
        return existing.id
    return create_account(conn, **account_create_kwargs(account)).id


def _wiring_pool(db_dsn: str) -> ConnectionPool:
    p = ConnectionPool(conninfo=db_dsn, min_size=1, max_size=4, open=True)
    with p.connection() as conn:
        conn.execute(
            "TRUNCATE accounts, mailboxes, messages, message_labels, "
            "attachment_blobs, failed_messages, daemon_heartbeats "
            "RESTART IDENTITY CASCADE"
        )
        conn.commit()
    return p


def _wiring_ctx(pool: ConnectionPool, tmp_path: Path,
                stop: threading.Event) -> WorkerContext:
    account = _wiring_account()
    with pool.connection() as conn:
        account_id = _ensure_account(conn, account)
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


def test_idle_session_records_connecting_then_idle(
    db_dsn: str, tmp_path: Path, monkeypatch
) -> None:
    pool = _wiring_pool(db_dsn)
    try:
        imap = FakeIMAPClient()
        imap.add_folder("INBOX")

        @contextmanager
        def fake_open(account, **kw):  # noqa: ARG001
            yield imap

        monkeypatch.setattr(idle_mod, "open_connection", fake_open)
        spy = _HBSpy()
        monkeypatch.setattr(idle_mod, "safe_heartbeat", spy)

        stop = threading.Event()
        ctx = _wiring_ctx(pool, tmp_path, stop)
        stop.set()  # exit the inner idle loop immediately after connect+idle

        _one_inbox_session(ctx)

        assert ("idle", "connecting", None) in spy.calls
        assert ("idle", "idle", None) in spy.calls
    finally:
        pool.close()


def test_poll_pass_records_polling_and_syncing(
    db_dsn: str, tmp_path: Path, monkeypatch
) -> None:
    pool = _wiring_pool(db_dsn)
    try:
        imap = FakeIMAPClient.with_folders(["INBOX", "Archive"])

        @contextmanager
        def fake_open(account, **kw):  # noqa: ARG001
            yield imap

        monkeypatch.setattr(poll_mod, "open_connection", fake_open)
        spy = _HBSpy()
        monkeypatch.setattr(poll_mod, "safe_heartbeat", spy)

        ctx = _wiring_ctx(pool, tmp_path, threading.Event())
        _one_poll_pass(ctx)

        assert any(c[0] == "poll" and c[1] == "polling" for c in spy.calls)
        assert ("poll", "syncing", "Archive") in spy.calls
    finally:
        pool.close()


def test_poll_wait_between_passes_rebeats_idle_and_caps_sleep(
    db_dsn: str, tmp_path: Path, monkeypatch
) -> None:
    """A healthy poll thread must keep beating while it idles between passes,
    chunking the wait by HEARTBEAT_SECONDS rather than the (much larger)
    poll_seconds, so its daemon-status row never reads falsely stale."""
    pool = _wiring_pool(db_dsn)
    try:
        spy = _HBSpy()
        monkeypatch.setattr(poll_mod, "safe_heartbeat", spy)
        ctx = _wiring_ctx(pool, tmp_path, threading.Event())
        ctx.poll_seconds = 600  # >> HEARTBEAT_SECONDS

        waits: list[float] = []

        class _FakeStop:
            def __init__(self) -> None:
                self.n = 0

            def wait(self, timeout: float) -> bool:
                waits.append(timeout)
                self.n += 1
                return self.n >= 3  # signal stop after the third chunk

        ctx.stop = _FakeStop()  # type: ignore[assignment]

        assert poll_mod._wait_between_passes(ctx) is True
        assert spy.calls.count(("poll", "idle", None)) >= 3
        assert waits and all(w <= poll_mod.HEARTBEAT_SECONDS for w in waits)
    finally:
        pool.close()


def test_startup_clears_leftover_heartbeats(db_dsn: str) -> None:
    _truncate(db_dsn)
    with psycopg.connect(db_dsn) as conn:
        record_heartbeat(conn, worker_kind="embed", account_id=None, state="idle")
        conn.commit()
    d = Daemon(_cfg(db_dsn), ssl=False, stop_event=threading.Event())
    try:
        d.start_workers()  # spawns no account threads (no syncable accounts), clears HBs
        assert "embed" not in _heartbeat_kinds(db_dsn)
    finally:
        d.stop()
        d.pool.close()


def test_teardown_account_clears_its_heartbeats(
    db_dsn: str, monkeypatch
) -> None:
    """Tearing down an account (paused/removed via hot-reload) must drop its
    idle/poll heartbeat rows so it no longer reads as a (stale) live thread."""
    _truncate(db_dsn)
    monkeypatch.setattr(daemon_mod, "run_inbox_idle_loop",
                        lambda ctx: ctx.stop.wait())
    monkeypatch.setattr(daemon_mod, "run_poll_loop", lambda ctx: ctx.stop.wait())

    with psycopg.connect(db_dsn) as conn:
        aid = _ensure_account(conn, _wiring_account())
        conn.commit()

    d = Daemon(_cfg(db_dsn), ssl=False, stop_event=threading.Event())
    try:
        with psycopg.connect(db_dsn) as conn:
            row = next(r for r in list_syncable_accounts(conn) if r.id == aid)
        d._spawn_account(row)
        with d.pool.connection() as conn:
            record_heartbeat(conn, worker_kind="idle", account_id=aid,
                             state="idle")
            record_heartbeat(conn, worker_kind="poll", account_id=aid,
                             state="polling")
            conn.commit()
        assert _heartbeat_kinds(db_dsn) == {"idle", "poll"}

        d._teardown_account(aid)
        assert _heartbeat_kinds(db_dsn) == set()
    finally:
        d.stop()
        d.join(timeout=2)
        d.pool.close()


# --- process-level worker heartbeat wiring (embed + extract) -----------------

import time

import localmail.search.embed_worker as embed_mod
import localmail.search.extract_worker as extract_mod
from localmail.config import SearchConfig
from localmail.search.sweep_pacing import SweepOutcome


class _ProcHBSpy:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def __call__(self, pool, *, worker_kind, account_id, state,
                 current_folder=None, last_error_msg=None) -> None:
        assert account_id is None  # process-level workers are account-agnostic
        self.calls.append((worker_kind, state))


def test_embed_worker_records_embed_heartbeat(db_dsn: str, monkeypatch) -> None:
    pool = ConnectionPool(conninfo=db_dsn, min_size=1, max_size=2, open=True)
    try:
        spy = _ProcHBSpy()
        monkeypatch.setattr(embed_mod, "safe_heartbeat", spy)
        monkeypatch.setattr(
            embed_mod,
            "run_embed_worker_once",
            lambda *a, **k: SweepOutcome(embedded=0, lang_visited=0),
        )
        stop = threading.Event()

        class _Backend:
            name = "fake"
            model = "fake"
            dimension = 768

            def embed_documents(self, t):
                return [[0.0] * 768 for _ in t]

            def embed_query(self, t):
                return [0.0] * 768

            def health_check(self):
                pass

        cfg = SearchConfig(embed_worker_poll_interval_s=30)
        th = threading.Thread(
            target=embed_mod.run_embed_worker,
            args=(stop, pool, cfg, _Backend()),
            daemon=True,
        )
        th.start()
        time.sleep(0.2)
        stop.set()
        th.join(timeout=5)
        assert ("embed", "idle") in spy.calls
    finally:
        pool.close()


def test_extract_worker_records_extract_heartbeat(db_dsn: str, monkeypatch) -> None:
    pool = ConnectionPool(conninfo=db_dsn, min_size=1, max_size=2, open=True)
    try:
        spy = _ProcHBSpy()
        monkeypatch.setattr(extract_mod, "safe_heartbeat", spy)
        monkeypatch.setattr(extract_mod, "run_extract_worker_once", lambda *a, **k: 0)
        stop = threading.Event()
        cfg = SearchConfig(extract_worker_poll_interval_s=30)
        th = threading.Thread(
            target=extract_mod.run_extract_worker,
            kwargs={"pool": pool, "cfg": cfg, "stop_event": stop},
            daemon=True,
        )
        th.start()
        time.sleep(0.2)
        stop.set()
        th.join(timeout=5)
        assert ("extract", "idle") in spy.calls
    finally:
        pool.close()
