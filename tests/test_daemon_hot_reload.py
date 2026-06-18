# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Daemon account hot-reload: reconcile spawns/tears-down/respawns (2B.1)."""

from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest

import localmail.daemon as daemon_mod
from localmail.config import LocalmailConfig
from localmail.daemon import Daemon


class _FakeBackend:
    name = "fake"
    model = "fake"
    dimension = 768

    def embed_documents(self, texts):
        return [[0.5] * 768 for _ in texts]

    def embed_query(self, _text):
        return [0.5] * 768

    def health_check(self) -> None:
        pass


def _cfg(db_dsn):
    cfg = LocalmailConfig.model_validate({"database": {"dsn": db_dsn}})
    cfg.search.run_embed_worker = False
    cfg.search.run_extract_worker = False
    return cfg


def _row(account_id: int, day: int, name: str | None = None):
    """A minimal stand-in carrying the fields _spawn_account reads."""
    from localmail.api.admin.accounts import Account

    return Account(
        id=account_id,
        name=name or f"acct{account_id}",
        email_address=f"a{account_id}@example.com",
        auth_method="password",
        oauth_provider=None,
        imap_host="imap.example.com",
        imap_port=993,
        folder_allow=None,
        folder_deny=None,
        folder_deny_flags=None,
        sync_enabled=True,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, day, tzinfo=timezone.utc),
    )


@pytest.fixture
def quiet_threads(monkeypatch):
    """Replace the IDLE/poll loops with functions that block on ctx.stop so no
    real IMAP/DB IO happens; threads exit promptly when their event is set."""

    def fake_idle(ctx):
        ctx.stop.wait()

    def fake_poll(ctx):
        ctx.stop.wait()

    monkeypatch.setattr(daemon_mod, "run_inbox_idle_loop", fake_idle)
    monkeypatch.setattr(daemon_mod, "run_poll_loop", fake_poll)


def _make_daemon(db_dsn, monkeypatch, desired):
    """Construct a Daemon whose list_syncable_accounts returns `desired()`."""
    monkeypatch.setattr(daemon_mod, "list_syncable_accounts", lambda conn: desired())
    d = Daemon(
        cfg=_cfg(db_dsn),
        dsn=db_dsn,
        embedding_backend_factory=lambda c: _FakeBackend(),
    )
    return d


def test_reconcile_spawns_new_account(db_dsn, monkeypatch, quiet_threads):
    state = {"rows": []}
    d = _make_daemon(db_dsn, monkeypatch, lambda: state["rows"])
    try:
        d.start_workers()
        assert d._account_threads == {}
        state["rows"] = [_row(1, 1)]
        d.reconcile()
        assert set(d._account_threads) == {1}
    finally:
        d.stop()
        d.join(timeout=2)
        d.pool.close()


def test_reconcile_tears_down_vanished_account(db_dsn, monkeypatch, quiet_threads):
    state = {"rows": [_row(1, 1), _row(2, 1)]}
    d = _make_daemon(db_dsn, monkeypatch, lambda: state["rows"])
    try:
        d.start_workers()
        assert set(d._account_threads) == {1, 2}
        bundle2 = d._account_threads[2]
        state["rows"] = [_row(1, 1)]
        d.reconcile()
        assert set(d._account_threads) == {1}
        assert bundle2.stop_event.is_set()  # the removed account was told to stop
        assert not bundle2.idle_thread.is_alive()
    finally:
        d.stop()
        d.join(timeout=2)
        d.pool.close()


def test_reconcile_respawns_on_updated_at_change(db_dsn, monkeypatch, quiet_threads):
    state = {"rows": [_row(1, 1)]}
    d = _make_daemon(db_dsn, monkeypatch, lambda: state["rows"])
    try:
        d.start_workers()
        old = d._account_threads[1]
        state["rows"] = [_row(1, 2)]  # same id, newer updated_at
        d.reconcile()
        assert set(d._account_threads) == {1}
        assert d._account_threads[1] is not old
        assert old.stop_event.is_set()
    finally:
        d.stop()
        d.join(timeout=2)
        d.pool.close()


def test_reconcile_survives_db_read_error(db_dsn, monkeypatch, quiet_threads):
    state = {"rows": [_row(1, 1)]}
    d = _make_daemon(db_dsn, monkeypatch, lambda: state["rows"])
    try:
        d.start_workers()
        assert set(d._account_threads) == {1}

        def boom(conn):
            raise RuntimeError("db down")

        monkeypatch.setattr(daemon_mod, "list_syncable_accounts", boom)
        d.reconcile()  # must not raise
        assert set(d._account_threads) == {1}  # existing thread kept
    finally:
        d.stop()
        d.join(timeout=2)
        d.pool.close()


def test_reconcile_resizes_pool_when_count_changes(db_dsn, monkeypatch, quiet_threads):
    state = {"rows": []}
    d = _make_daemon(db_dsn, monkeypatch, lambda: state["rows"])
    calls = []
    monkeypatch.setattr(d.pool, "resize", lambda **kw: calls.append(kw))
    try:
        d.start_workers()
        state["rows"] = [_row(i, 1) for i in range(1, 6)]  # 5 accounts
        d.reconcile()
        assert calls, "expected pool.resize to be called when count grew"
        calls.clear()
        d.reconcile()  # no-op reconcile must not resize again
        assert calls == []
    finally:
        d.stop()
        d.join(timeout=2)
        d.pool.close()


def test_run_forever_reconciles_then_stops(db_dsn, monkeypatch, quiet_threads):
    """run_forever picks up an account added after start, then stops cleanly."""
    state = {"rows": []}
    d = _make_daemon(db_dsn, monkeypatch, lambda: state["rows"])
    d.cfg.daemon.reload_seconds = 0.05  # tight loop so the test is fast
    seen = threading.Event()
    orig_reconcile = d.reconcile

    def watched_reconcile():
        orig_reconcile()
        if d._account_threads:
            seen.set()

    monkeypatch.setattr(d, "reconcile", watched_reconcile)

    t = threading.Thread(target=d.run_forever, daemon=True)
    t.start()
    try:
        state["rows"] = [_row(1, 1)]
        assert seen.wait(timeout=3), "account was not picked up by run_forever"
        assert set(d._account_threads) == {1}
    finally:
        d.stop()
        t.join(timeout=3)
    assert not t.is_alive()
    assert d._account_threads == {}  # torn down on shutdown
