# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Test that Daemon spawns + cleanly joins the extract_worker thread."""

from __future__ import annotations

import threading

from localmail.config import LocalmailConfig
from localmail.daemon import Daemon


class _E:
    name = "s"; model = "s"; dimension = 768

    def embed_documents(self, t):
        return [[0.5] * 768 for _ in t]

    def embed_query(self, t):
        return [0.5] * 768

    def health_check(self) -> None:
        pass


def _live_thread_names() -> set[str]:
    """Thread names as of right now — no wait, and none should be added back.

    ``Daemon.start()`` calls ``start_workers()``, which calls ``Thread.start()``
    for every account and worker thread synchronously; ``Thread.start()`` returns
    only once the thread is registered, and ``threading.enumerate()`` covers both
    the active and limbo tables. So a fixed ``time.sleep()`` here waits for
    something that has already happened, and buys nothing but a race the test
    can lose on a loaded runner (#299).

    **What this proves and what it does not.** Registration happens *before* the
    target runs a line, so a name found here says the thread was created — never
    that it survived. The deleted sleeps were incidentally proving survival too,
    and only probabilistically (a worker raising 50 ms in was caught 9 runs in
    10). That signal is not lost, it is relocated and made deterministic: a
    worker that dies of an exception now fails the test through
    ``filterwarnings = error::pytest.PytestUnhandledThreadExceptionWarning``
    (`pyproject.toml`), which needs no timer at all. Do not reintroduce a sleep
    to "check it is still alive" — an ``is_alive()`` here would be True for a
    thread that has not yet been scheduled, i.e. a pin weaker than it reads.

    The post-stop uses answer the opposite question, and their guarantee is
    ``Daemon.join()`` having actually joined; ``join`` applies its timeout per
    thread and does not report a timeout, so a stalled worker surfaces here as
    the name still being present.
    """
    return {t.name for t in threading.enumerate()}


def test_daemon_starts_extract_worker_when_enabled(db_dsn, db_conn) -> None:
    cfg = LocalmailConfig.model_validate({"database": {"dsn": db_dsn}})
    cfg.search.run_extract_worker = True
    d = Daemon(cfg=cfg, dsn=db_dsn, embedding_backend_factory=lambda c: _E())
    d.start()
    names = _live_thread_names()
    assert any(n.startswith("extract_worker") for n in names)
    d.stop()
    d.join(timeout=5)
    names_after = _live_thread_names()
    assert not any(n.startswith("extract_worker") for n in names_after)


def test_daemon_skips_extract_worker_when_disabled(db_dsn, db_conn) -> None:
    cfg = LocalmailConfig.model_validate({"database": {"dsn": db_dsn}})
    cfg.search.run_extract_worker = False
    d = Daemon(cfg=cfg, dsn=db_dsn, embedding_backend_factory=lambda c: _E())
    d.start()
    names = _live_thread_names()
    assert not any(n.startswith("extract_worker") for n in names)
    d.stop()
    d.join(timeout=5)


def test_daemon_starts_idle_poll_extract_together_and_joins_on_stop(
    db_dsn, db_conn,
) -> None:
    """All three thread types must spawn alongside each other AND join on stop.

    The account is seeded into the DB — the daemon's canonical account source
    as of Sub-plan 2A.2b — pointing at an unreachable IMAP host, so the worker
    threads exercise their backoff path; the backoff uses ``stop_event.wait``
    so cancellation is prompt. This pins the shutdown sequence (IDLE + poll +
    embed + extract joined together via the shared ``stop_event``).
    """
    from localmail.api.admin.accounts import create_account

    create_account(
        db_conn, name="test-acct", email_address="test@example.invalid",
        auth_method="password", imap_host="127.0.0.1", imap_port=1,
        oauth_provider=None, folder_allow=None, folder_deny=None,
        folder_deny_flags=None,
    )
    db_conn.commit()

    cfg = LocalmailConfig.model_validate({"database": {"dsn": db_dsn}})
    cfg.search.run_extract_worker = True
    cfg.search.run_embed_worker = True
    d = Daemon(cfg=cfg, dsn=db_dsn, embedding_backend_factory=lambda c: _E())
    d.start()
    names = _live_thread_names()
    assert any(n == "idle-test-acct" for n in names), names
    assert any(n == "poll-test-acct" for n in names), names
    assert any(n == "embed_worker" for n in names), names
    assert any(n == "extract_worker" for n in names), names
    d.stop()
    d.join(timeout=10)
    names_after = _live_thread_names()
    assert not any(n.startswith("idle-") for n in names_after), names_after
    assert not any(n.startswith("poll-") for n in names_after), names_after
    assert "embed_worker" not in names_after
    assert "extract_worker" not in names_after


def test_daemon_start_then_run_forever_does_not_double_spawn(
    db_dsn, db_conn,
) -> None:
    """Calling ``start()`` then ``run_forever()`` must not duplicate workers.

    A regression where ``run_forever`` re-invokes ``start_workers`` would
    silently double every per-account thread — and double the per-account
    DB connection footprint. The idempotency flag on ``start_workers`` is
    the only thing preventing that, so it deserves a direct test.
    """
    cfg = LocalmailConfig.model_validate({"database": {"dsn": db_dsn}})
    cfg.search.run_extract_worker = True
    d = Daemon(cfg=cfg, dsn=db_dsn, embedding_backend_factory=lambda c: _E())
    d.start()
    d.start_workers()  # explicit second call — must be a no-op
    spawned = list(d._worker_threads)
    assert len(spawned) == len({id(t) for t in spawned})
    extract_threads = [t for t in spawned if t.name == "extract_worker"]
    assert len(extract_threads) == 1
    d.stop()
    d.join(timeout=5)
