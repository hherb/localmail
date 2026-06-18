# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Daemon consumes daemon_commands at the top of reconcile (2B.3).

These are real-DB tests: commands FK accounts(id), so accounts must exist in the
DB and the daemon reads them via the real list_syncable_accounts. IDLE/poll loops
are replaced by quiet stubs (the quiet_threads fixture)."""
from __future__ import annotations

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


def _cfg(db_dsn):
    cfg = LocalmailConfig.model_validate({"database": {"dsn": db_dsn}})
    cfg.search.run_embed_worker = False
    cfg.search.run_extract_worker = False
    cfg.daemon.command_listen_enabled = False  # no real listener in these tests
    return cfg


def _account(conn: psycopg.Connection, name: str) -> int:
    cfg = AccountConfig(
        name=name, email=f"{name}@example.com",
        imap_host="imap.example.com", imap_port=993, auth_method="password",
    )
    return create_account(conn, **account_create_kwargs(cfg)).id


@pytest.fixture
def quiet_threads(monkeypatch):
    monkeypatch.setattr(daemon_mod, "run_inbox_idle_loop", lambda ctx: ctx.stop.wait())
    monkeypatch.setattr(daemon_mod, "run_poll_loop", lambda ctx: ctx.stop.wait())


def _make_daemon(db_dsn):
    return Daemon(cfg=_cfg(db_dsn), dsn=db_dsn,
                  embedding_backend_factory=lambda c: _FakeBackend())


def _command_states(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT command, state FROM daemon_commands ORDER BY id")
        return cur.fetchall()


def test_reload_now_command_marked_done(db_conn, db_dsn, quiet_threads):
    _account(db_conn, "a1"); db_conn.commit()
    d = _make_daemon(db_dsn)
    try:
        d.start_workers()
        enqueue_command(db_conn, command="reload-now", requested_by=None)
        db_conn.commit()
        d.reconcile()
        assert _command_states(db_conn) == [("reload-now", "done")]
    finally:
        d.stop(); d.join(timeout=2); d.pool.close()


def test_restart_account_tears_down_and_respawns(db_conn, db_dsn, quiet_threads):
    aid = _account(db_conn, "a1"); db_conn.commit()
    d = _make_daemon(db_dsn)
    try:
        d.start_workers()
        old = d._account_threads[aid]
        enqueue_command(db_conn, command="restart-account", account_id=aid,
                        requested_by=None)
        db_conn.commit()
        d.reconcile()  # drain tears aid down; the same-tick diff respawns it
        assert aid in d._account_threads
        assert d._account_threads[aid] is not old
        assert old.stop_event.is_set()
        assert _command_states(db_conn) == [("restart-account", "done")]
    finally:
        d.stop(); d.join(timeout=2); d.pool.close()


def test_restart_account_leaves_other_accounts_untouched(db_conn, db_dsn, quiet_threads):
    a1 = _account(db_conn, "a1"); a2 = _account(db_conn, "a2"); db_conn.commit()
    d = _make_daemon(db_dsn)
    try:
        d.start_workers()
        other = d._account_threads[a2]
        enqueue_command(db_conn, command="restart-account", account_id=a1,
                        requested_by=None)
        db_conn.commit()
        d.reconcile()
        assert d._account_threads[a2] is other  # untouched
        assert not other.stop_event.is_set()
    finally:
        d.stop(); d.join(timeout=2); d.pool.close()


def test_drain_stop_sets_master_stop_event(db_conn, db_dsn, quiet_threads):
    _account(db_conn, "a1"); db_conn.commit()
    d = _make_daemon(db_dsn)
    try:
        d.start_workers()
        enqueue_command(db_conn, command="drain-stop", requested_by=None)
        db_conn.commit()
        d.reconcile()
        assert d._stop_event.is_set()
        assert _command_states(db_conn) == [("drain-stop", "done")]
    finally:
        d.stop(); d.join(timeout=2); d.pool.close()


def test_drain_command_failure_marks_failed_and_survives(db_conn, db_dsn, quiet_threads,
                                                          monkeypatch):
    _account(db_conn, "a1"); db_conn.commit()
    d = _make_daemon(db_dsn)
    try:
        d.start_workers()
        enqueue_command(db_conn, command="reload-now", requested_by=None)
        db_conn.commit()

        def boom(cmd):
            raise RuntimeError("apply failed")

        monkeypatch.setattr(d, "_apply_command", boom)
        d.reconcile()  # must not raise
        assert _command_states(db_conn) == [("reload-now", "failed")]
    finally:
        d.stop(); d.join(timeout=2); d.pool.close()
