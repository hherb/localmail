# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""LISTEN/NOTIFY wake: the listener sets the reconcile wake on NOTIFY, and
run_forever reconciles early instead of waiting out reload_seconds (2B.3)."""
from __future__ import annotations

import threading
import time

import psycopg
import pytest

import localmail.daemon as daemon_mod
from localmail.account_seed import account_create_kwargs
from localmail.api.admin.accounts import create_account
from localmail.api.admin.daemon import enqueue_command
from localmail.config import AccountConfig, LocalmailConfig
from localmail.daemon import Daemon


class _FakeBackend:
    name = "fake"; model = "fake"; dimension = 768
    def embed_documents(self, texts): return [[0.5] * 768 for _ in texts]
    def embed_query(self, _t): return [0.5] * 768
    def health_check(self) -> None: pass


def _cfg(db_dsn, *, listen=True):
    cfg = LocalmailConfig.model_validate({"database": {"dsn": db_dsn}})
    cfg.search.run_embed_worker = False
    cfg.search.run_extract_worker = False
    cfg.daemon.command_listen_enabled = listen
    cfg.daemon.command_listen_poll_seconds = 0.2  # snappy stop in tests
    cfg.daemon.reload_seconds = 30  # large: only a NOTIFY can cause an early tick
    return cfg


def _account(conn, name="a1"):
    cfg = AccountConfig(name=name, email=f"{name}@example.com",
                        imap_host="imap.example.com", imap_port=993,
                        auth_method="password")
    return create_account(conn, **account_create_kwargs(cfg)).id


@pytest.fixture
def quiet_threads(monkeypatch):
    monkeypatch.setattr(daemon_mod, "run_inbox_idle_loop", lambda ctx: ctx.stop.wait())
    monkeypatch.setattr(daemon_mod, "run_poll_loop", lambda ctx: ctx.stop.wait())


def _await_listening(d, timeout=5):
    """Block until the listener has issued LISTEN (it publishes _listener_conn
    right after), so the subsequent NOTIFY can't be lost to a slow connect —
    Postgres only delivers NOTIFY to sessions already LISTENing. Deterministic
    readiness gate in place of a fixed sleep (avoids CI flakiness)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if d._listener_conn is not None:
            return
        time.sleep(0.02)
    raise AssertionError("listener did not start LISTENing in time")


def test_notify_sets_reconcile_wake(db_conn, db_dsn, quiet_threads):
    """The listener thread sets _reconcile_wake when a NOTIFY arrives."""
    d = Daemon(cfg=_cfg(db_dsn), dsn=db_dsn,
               embedding_backend_factory=lambda c: _FakeBackend())
    listener = threading.Thread(target=d._run_command_listener, daemon=True)
    listener.start()
    try:
        _await_listening(d)
        enqueue_command(db_conn, command="reload-now", requested_by=None)
        db_conn.commit()
        assert d._reconcile_wake.wait(timeout=5), "NOTIFY did not set the wake"
    finally:
        d.stop()
        listener.join(timeout=3)
        d.pool.close()


def test_run_forever_reconciles_early_on_notify(db_conn, db_dsn, quiet_threads):
    """With reload_seconds=30, only the NOTIFY path can make reconcile run fast."""
    _account(db_conn, "a1"); db_conn.commit()
    d = Daemon(cfg=_cfg(db_dsn), dsn=db_dsn,
               embedding_backend_factory=lambda c: _FakeBackend())
    reconciled = threading.Event()
    orig = d.reconcile

    def watched():
        orig()
        reconciled.set()

    d.reconcile = watched  # type: ignore[method-assign]
    t = threading.Thread(target=d.run_forever, daemon=True)
    t.start()
    try:
        _await_listening(d)  # listener LISTENing; loop is in its wake-wait
        reconciled.clear()
        enqueue_command(db_conn, command="reload-now", requested_by=None)
        db_conn.commit()
        assert reconciled.wait(timeout=5), "run_forever did not reconcile on NOTIFY"
    finally:
        d.stop()
        t.join(timeout=5)
    assert not t.is_alive()


def test_listener_disabled_still_consumes_on_poll(db_conn, db_dsn, quiet_threads):
    """With the listener off, a command is still consumed on the next poll tick."""
    _account(db_conn, "a1"); db_conn.commit()
    cfg = _cfg(db_dsn, listen=False)
    cfg.daemon.reload_seconds = 0.1  # poll fast since there's no NOTIFY wake
    d = Daemon(cfg=cfg, dsn=db_dsn,
               embedding_backend_factory=lambda c: _FakeBackend())
    t = threading.Thread(target=d.run_forever, daemon=True)
    t.start()
    try:
        enqueue_command(db_conn, command="reload-now", requested_by=None)
        db_conn.commit()
        deadline = time.monotonic() + 5
        row = None
        while time.monotonic() < deadline:
            with db_conn.cursor() as cur:
                cur.execute("SELECT state FROM daemon_commands")
                row = cur.fetchone()
            db_conn.rollback()  # release snapshot so we see the daemon's commit
            if row and row[0] == "done":
                break
            time.sleep(0.1)
        assert row is not None and row[0] == "done"
    finally:
        d.stop()
        t.join(timeout=5)
