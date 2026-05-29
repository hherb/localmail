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

import time

from localmail.api.admin.accounts import create_account, update_account
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


def test_daemon_pool_max_size_auto_computed(db_dsn, db_conn) -> None:
    """No override → daemon picks the compute_daemon_pool_size value.

    db_conn truncates accounts so the DB-backed enumeration sees zero.
    """
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


def test_daemon_uses_single_pool_for_all_workers(db_dsn, db_conn, monkeypatch) -> None:
    """Embed + extract workers must run off the *same* ``self.pool`` object.

    The previous design opened a second pool for embed_worker and used a raw
    ``psycopg.connect`` per sweep for extract_worker. That meant the
    three pool sources didn't share a budget and could over-subscribe
    ``max_connections`` without any one of them noticing.

    Patching both worker entry points lets us pin pool *identity* rather
    than just the absence of a private attribute — a stronger invariant
    that survives renames.
    """
    captured: dict[str, object] = {}

    def fake_embed_worker(stop, pool, cfg, backend, **_kwargs) -> None:
        captured["embed_pool"] = pool
        stop.wait(timeout=5)

    def fake_extract_worker(*, pool, cfg, stop_event) -> None:
        captured["extract_pool"] = pool
        stop_event.wait(timeout=5)

    import localmail.search.embed_worker as embed_mod
    import localmail.search.extract_worker as extract_mod

    monkeypatch.setattr(embed_mod, "run_embed_worker", fake_embed_worker)
    monkeypatch.setattr(extract_mod, "run_extract_worker", fake_extract_worker)

    cfg = LocalmailConfig.model_validate({"database": {"dsn": db_dsn}})
    cfg.search.run_embed_worker = True
    cfg.search.run_extract_worker = True
    d = Daemon(cfg=cfg, dsn=db_dsn, embedding_backend_factory=lambda c: _FakeBackend())
    d.start()
    try:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and len(captured) < 2:
            time.sleep(0.05)
        assert captured["embed_pool"] is d.pool
        assert captured["extract_pool"] is d.pool
    finally:
        d.stop()
        d.join(timeout=5)


def test_daemon_config_defaults_pool_max_size_to_none() -> None:
    """The auto-compute path is opt-out, not opt-in."""
    cfg = DaemonConfig()
    assert cfg.pool_max_size is None


def _seed_two_syncable_plus_noise(conn) -> None:
    create_account(conn, name="pw", email_address="a@x.com",
                   auth_method="password", imap_host="h", imap_port=993,
                   oauth_provider=None, folder_allow=None, folder_deny=None,
                   folder_deny_flags=None)
    create_account(conn, name="oauth", email_address="b@x.com",
                   auth_method="oauth2", imap_host="h", imap_port=993,
                   oauth_provider="gmail", folder_allow=None, folder_deny=None,
                   folder_deny_flags=None)
    create_account(conn, name="arch", email_address="c@x.com",
                   auth_method="archive", imap_host=None, imap_port=None,
                   oauth_provider=None, folder_allow=None, folder_deny=None,
                   folder_deny_flags=None)
    off = create_account(conn, name="off", email_address="d@x.com",
                         auth_method="password", imap_host="h", imap_port=993,
                         oauth_provider=None, folder_allow=None,
                         folder_deny=None, folder_deny_flags=None)
    update_account(conn, off.id, sync_enabled=False)
    conn.commit()


def test_daemon_syncable_excludes_archive_and_disabled(db_dsn, db_conn) -> None:
    _seed_two_syncable_plus_noise(db_conn)
    cfg = LocalmailConfig.model_validate({"database": {"dsn": db_dsn}})
    cfg.search.run_embed_worker = False
    cfg.search.run_extract_worker = False
    d = Daemon(cfg=cfg, dsn=db_dsn, embedding_backend_factory=lambda c: _FakeBackend())
    try:
        assert [r.name for r in d._syncable] == ["pw", "oauth"]
    finally:
        d.pool.close()


def test_daemon_pool_sizes_from_db_account_count(db_dsn, db_conn) -> None:
    _seed_two_syncable_plus_noise(db_conn)
    cfg = LocalmailConfig.model_validate({"database": {"dsn": db_dsn}})
    cfg.search.run_embed_worker = False
    cfg.search.run_extract_worker = False
    d = Daemon(cfg=cfg, dsn=db_dsn, embedding_backend_factory=lambda c: _FakeBackend())
    try:
        assert d.pool.max_size == compute_daemon_pool_size(
            n_accounts=2, run_embed=False, run_extract=False
        )
    finally:
        d.pool.close()
