# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Daemon startup resilience: wait for Postgres rather than crash (#133).

``Daemon.__init__`` does DB IO during construction (enumerate syncable
accounts, then open the pool). If Postgres is briefly unreachable at launch
(DB still coming up under systemd, transient blip), construction must back off
and retry rather than raise — and a stop signal must still win over the retry
loop.
"""

from __future__ import annotations

import threading

import psycopg
import pytest

from localmail.config import LocalmailConfig
from localmail.daemon import Daemon
from localmail.retry import RetryAborted


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


def _fast_backoff_cfg(db_dsn):
    cfg = LocalmailConfig.model_validate(
        {
            "database": {"dsn": db_dsn},
            "daemon": {
                "startup_backoff_initial_s": 0.01,
                "startup_backoff_max_s": 0.05,
            },
        }
    )
    cfg.search.run_embed_worker = False
    cfg.search.run_extract_worker = False
    return cfg


def test_construction_retries_flaky_db_connect(db_dsn, db_conn, monkeypatch) -> None:
    """Two OperationalErrors then success → daemon constructs, no raise."""
    import localmail.daemon as daemon_mod

    real_connect = psycopg.connect
    calls = {"n": 0}

    def flaky_connect(conninfo, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise psycopg.OperationalError("db not ready")
        return real_connect(conninfo, *args, **kwargs)

    monkeypatch.setattr(daemon_mod.psycopg, "connect", flaky_connect)

    d = Daemon(
        cfg=_fast_backoff_cfg(db_dsn),
        dsn=db_dsn,
        embedding_backend_factory=lambda c: _FakeBackend(),
    )
    try:
        assert calls["n"] == 3  # failed twice, recovered on the third attempt
        assert d._syncable == []  # truncated DB → constructs cleanly
    finally:
        d.pool.close()


def test_construction_aborts_when_stop_fires_during_backoff(
    db_dsn, monkeypatch
) -> None:
    """A stop signal during the backoff wait aborts construction with
    RetryAborted instead of looping forever on a dead DB."""
    import localmail.daemon as daemon_mod

    stop = threading.Event()

    def fail_then_stop(*_args, **_kwargs):
        stop.set()  # trip stop so the post-failure wait returns True
        raise psycopg.OperationalError("never ready")

    monkeypatch.setattr(daemon_mod.psycopg, "connect", fail_then_stop)

    with pytest.raises(RetryAborted):
        Daemon(
            cfg=_fast_backoff_cfg(db_dsn),
            dsn=db_dsn,
            stop_event=stop,
            embedding_backend_factory=lambda c: _FakeBackend(),
        )
