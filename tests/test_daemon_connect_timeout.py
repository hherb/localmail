# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Bounded connect + statement for the daemon's fresh psycopg connects (#140, #142).

Three daemon paths open a *fresh* ``psycopg.connect(self._dsn)`` rather than
borrowing from the shared pool: ``_load_syncable_accounts`` (construction),
``reconcile`` (each tick), and ``_clear_heartbeats`` (startup reset). Without a
bounded ``connect_timeout`` a network black-hole (host up, packets dropped)
blocks the *connect* for the OS TCP default — minutes — stalling startup and the
reconcile/hot-reload loop (#140). ``connect_timeout`` bounds only the TCP connect
phase, so a black-hole that begins *after* the connect succeeds still hangs the
client; ``statement_timeout`` (server-side) bounds a slow / stuck query but does
nothing when the server never sees the query or the reply is dropped, while
``tcp_user_timeout`` forces the socket closed after that much unacknowledged data
— the actual post-connect black-hole bound (#142). Every fresh connect must pass
all three bounds, sourced from config (no magic literal).
"""

from __future__ import annotations

import threading

import psycopg

import localmail.daemon as daemon_mod
from localmail.config import LocalmailConfig
from localmail.daemon import Daemon


def _cfg(
    db_dsn: str,
    *,
    connect_timeout: int,
    statement_timeout: int = 30,
    tcp_user_timeout: int = 30000,
) -> LocalmailConfig:
    cfg = LocalmailConfig.model_validate(
        {
            "database": {"dsn": db_dsn},
            "daemon": {
                "db_connect_timeout_s": connect_timeout,
                "db_statement_timeout_s": statement_timeout,
                "db_tcp_user_timeout_ms": tcp_user_timeout,
            },
        }
    )
    cfg.search.run_embed_worker = False
    cfg.search.run_extract_worker = False
    return cfg


def _truncate(dsn: str) -> None:
    with psycopg.connect(dsn) as conn:
        conn.execute("TRUNCATE accounts, daemon_heartbeats RESTART IDENTITY CASCADE")
        conn.commit()


def test_fresh_connects_pass_connect_timeout_from_config(
    db_dsn: str, monkeypatch
) -> None:
    _truncate(db_dsn)

    real_connect = psycopg.connect
    captured: list[object] = []

    def spy(*args, **kwargs):
        captured.append(kwargs.get("connect_timeout"))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(daemon_mod.psycopg, "connect", spy)

    d = Daemon(_cfg(db_dsn, connect_timeout=7), ssl=False,
               stop_event=threading.Event())
    try:
        captured.clear()  # ignore the construction-time connect; assert the named paths
        d.reconcile()
        d._clear_heartbeats()
    finally:
        d.stop()

    assert captured, "expected reconcile + _clear_heartbeats to open fresh connects"
    assert all(t == 7 for t in captured), captured


def test_construction_connect_passes_connect_timeout(db_dsn: str, monkeypatch) -> None:
    _truncate(db_dsn)

    real_connect = psycopg.connect
    captured: list[object] = []

    def spy(*args, **kwargs):
        captured.append(kwargs.get("connect_timeout"))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(daemon_mod.psycopg, "connect", spy)

    d = Daemon(_cfg(db_dsn, connect_timeout=5), ssl=False,
               stop_event=threading.Event())
    try:
        assert captured, "expected _load_syncable_accounts to open a fresh connect"
        assert all(t == 5 for t in captured), captured
    finally:
        d.stop()


def test_fresh_connects_pass_statement_timeout_from_config(
    db_dsn: str, monkeypatch
) -> None:
    _truncate(db_dsn)

    real_connect = psycopg.connect
    captured: list[object] = []

    def spy(*args, **kwargs):
        captured.append(kwargs.get("options"))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(daemon_mod.psycopg, "connect", spy)

    d = Daemon(_cfg(db_dsn, connect_timeout=7, statement_timeout=11), ssl=False,
               stop_event=threading.Event())
    try:
        captured.clear()  # ignore the construction-time connect; assert the named paths
        d.reconcile()
        d._clear_heartbeats()
    finally:
        d.stop()

    assert captured, "expected reconcile + _clear_heartbeats to open fresh connects"
    assert all(opt == "-c statement_timeout=11s" for opt in captured), captured


def test_construction_connect_passes_statement_timeout(db_dsn: str, monkeypatch) -> None:
    _truncate(db_dsn)

    real_connect = psycopg.connect
    captured: list[object] = []

    def spy(*args, **kwargs):
        captured.append(kwargs.get("options"))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(daemon_mod.psycopg, "connect", spy)

    d = Daemon(_cfg(db_dsn, connect_timeout=5, statement_timeout=13), ssl=False,
               stop_event=threading.Event())
    try:
        assert captured, "expected _load_syncable_accounts to open a fresh connect"
        assert all(opt == "-c statement_timeout=13s" for opt in captured), captured
    finally:
        d.stop()


def test_fresh_connects_pass_tcp_user_timeout_from_config(
    db_dsn: str, monkeypatch
) -> None:
    _truncate(db_dsn)

    real_connect = psycopg.connect
    captured: list[object] = []

    def spy(*args, **kwargs):
        captured.append(kwargs.get("tcp_user_timeout"))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(daemon_mod.psycopg, "connect", spy)

    d = Daemon(_cfg(db_dsn, connect_timeout=7, tcp_user_timeout=9000), ssl=False,
               stop_event=threading.Event())
    try:
        captured.clear()  # ignore the construction-time connect; assert the named paths
        d.reconcile()
        d._clear_heartbeats()
    finally:
        d.stop()

    assert captured, "expected reconcile + _clear_heartbeats to open fresh connects"
    assert all(t == 9000 for t in captured), captured


def test_construction_connect_passes_tcp_user_timeout(db_dsn: str, monkeypatch) -> None:
    _truncate(db_dsn)

    real_connect = psycopg.connect
    captured: list[object] = []

    def spy(*args, **kwargs):
        captured.append(kwargs.get("tcp_user_timeout"))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(daemon_mod.psycopg, "connect", spy)

    d = Daemon(_cfg(db_dsn, connect_timeout=5, tcp_user_timeout=4000), ssl=False,
               stop_event=threading.Event())
    try:
        assert captured, "expected _load_syncable_accounts to open a fresh connect"
        assert all(t == 4000 for t in captured), captured
    finally:
        d.stop()
