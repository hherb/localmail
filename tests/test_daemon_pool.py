"""Tests for unified daemon connection-pool sizing (#37, closes #9).

These pin down the contract that:
1. The pool budget is a pure function of (#accounts, embed-enabled, extract-enabled).
2. ``DaemonConfig.pool_max_size`` overrides the auto-computed budget.
3. The ``Daemon`` builds a single shared pool for IDLE/poll/embed/extract —
   no separate ``_embed_pool`` and no ad-hoc ``psycopg.connect`` per extract
   sweep. Holding three independent budgets that don't share a cap was the
   bug #37 set out to fix.
"""

from __future__ import annotations

import threading
import time

from localmail.config import DaemonConfig, LocalmailConfig
from localmail.daemon import Daemon
from localmail.db import (
    POOL_BASELINE_MIN,
    POOL_HEADROOM,
    compute_daemon_pool_size,
)


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


# --- pure function ---------------------------------------------------------


def test_compute_daemon_pool_size_baseline_when_idle() -> None:
    """With zero accounts and both workers off, the floor wins."""
    assert (
        compute_daemon_pool_size(
            n_accounts=0, run_embed=False, run_extract=False
        )
        == POOL_BASELINE_MIN
    )


def test_compute_daemon_pool_size_scales_with_accounts() -> None:
    """Each account contributes 2 slots (IDLE + poll); workers add 1 each."""
    n_accounts = 5
    expected = max(
        POOL_BASELINE_MIN,
        2 * n_accounts + 2 + POOL_HEADROOM,
    )
    assert (
        compute_daemon_pool_size(
            n_accounts=n_accounts, run_embed=True, run_extract=True
        )
        == expected
    )


def test_compute_daemon_pool_size_embed_only_vs_extract_only() -> None:
    """Each worker independently adds exactly one slot."""
    embed_only = compute_daemon_pool_size(
        n_accounts=3, run_embed=True, run_extract=False
    )
    extract_only = compute_daemon_pool_size(
        n_accounts=3, run_embed=False, run_extract=True
    )
    neither = compute_daemon_pool_size(
        n_accounts=3, run_embed=False, run_extract=False
    )
    assert embed_only == extract_only == neither + 1


# --- Daemon integration ---------------------------------------------------


def test_daemon_pool_max_size_auto_computed(db_dsn) -> None:
    """No override → daemon picks the compute_daemon_pool_size value."""
    cfg = LocalmailConfig.model_validate({"database": {"dsn": db_dsn}})
    cfg.search.run_embed_worker = True
    cfg.search.run_extract_worker = True
    d = Daemon(cfg=cfg, dsn=db_dsn, embedding_backend_factory=lambda c: _FakeBackend())
    try:
        expected = compute_daemon_pool_size(
            n_accounts=0, run_embed=True, run_extract=True
        )
        assert d.pool.max_size == expected
    finally:
        d.pool.close()


def test_daemon_pool_max_size_respects_explicit_override(db_dsn) -> None:
    """An explicit DaemonConfig.pool_max_size wins over the auto formula."""
    cfg = LocalmailConfig.model_validate(
        {
            "database": {"dsn": db_dsn},
            "daemon": {"pool_max_size": 42},
        }
    )
    d = Daemon(cfg=cfg, dsn=db_dsn, embedding_backend_factory=lambda c: _FakeBackend())
    try:
        assert d.pool.max_size == 42
    finally:
        d.pool.close()


def test_daemon_uses_single_pool_for_all_workers(db_dsn) -> None:
    """Embed + extract workers must run off ``self.pool``, no second pool.

    The previous design opened a second pool for embed_worker and used a raw
    ``psycopg.connect`` per sweep for extract_worker. That meant the
    three pool sources didn't share a budget and could over-subscribe
    ``max_connections`` without any one of them noticing.
    """
    cfg = LocalmailConfig.model_validate({"database": {"dsn": db_dsn}})
    cfg.search.run_embed_worker = True
    cfg.search.run_extract_worker = True
    d = Daemon(cfg=cfg, dsn=db_dsn, embedding_backend_factory=lambda c: _FakeBackend())
    d.start()
    try:
        time.sleep(0.4)
        names = {t.name for t in threading.enumerate()}
        assert "embed_worker" in names
        assert "extract_worker" in names
        # No separate embed pool object; the single self.pool is reused.
        assert getattr(d, "_embed_pool", None) is None
    finally:
        d.stop()
        d.join(timeout=5)


def test_daemon_config_defaults_pool_max_size_to_none() -> None:
    """The auto-compute path is opt-out, not opt-in."""
    cfg = DaemonConfig()
    assert cfg.pool_max_size is None
