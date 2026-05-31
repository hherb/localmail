"""Bounded connect for the daemon's fresh psycopg connects (#140).

Three daemon paths open a *fresh* ``psycopg.connect(self._dsn)`` rather than
borrowing from the shared pool: ``_load_syncable_accounts`` (construction),
``reconcile`` (each tick), and ``_clear_heartbeats`` (startup reset). Without a
bounded ``connect_timeout`` a network black-hole (host up, packets dropped)
blocks the connect for the OS TCP default — minutes — stalling startup and the
reconcile/hot-reload loop. Every fresh connect must pass a ``connect_timeout``
sourced from config (no magic literal).
"""

from __future__ import annotations

import threading

import psycopg

import localmail.daemon as daemon_mod
from localmail.config import LocalmailConfig
from localmail.daemon import Daemon


def _cfg(db_dsn: str, *, connect_timeout: int) -> LocalmailConfig:
    cfg = LocalmailConfig.model_validate(
        {
            "database": {"dsn": db_dsn},
            "daemon": {"db_connect_timeout_s": connect_timeout},
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
