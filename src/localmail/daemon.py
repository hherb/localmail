"""Daemon: one worker thread per account, plus a per-account IDLE thread on INBOX."""

from __future__ import annotations

import logging
import signal
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg

from .api.admin.accounts import Account, list_syncable_accounts
from .config import Config
from .daemon_accounts import account_config_from_row
from .daemon_reconcile import plan_reconcile
from .db import compute_daemon_pool_size, open_pool
from .heartbeat import clear_all_heartbeats, safe_heartbeat
from .idle import run_inbox_idle_loop
from .poller import run_poll_loop
from .retry import retry_with_backoff
from .worker import WorkerContext

log = logging.getLogger(__name__)


@dataclass
class AccountThreads:
    account_id: int
    updated_at: datetime
    stop_event: threading.Event
    idle_thread: threading.Thread
    poll_thread: threading.Thread


class Daemon:
    def __init__(
        self,
        cfg: Config,
        *,
        ssl: bool = True,
        dsn: str | None = None,
        embedding_backend_factory=None,
        stop_event: threading.Event | None = None,
    ) -> None:
        self.cfg = cfg
        self.ssl = ssl
        self._dsn = dsn or cfg.database.dsn
        self._stop_event = stop_event or threading.Event()
        self._syncable = retry_with_backoff(
            self._load_syncable_accounts,
            stop_event=self._stop_event,
            initial_s=cfg.daemon.startup_backoff_initial_s,
            max_s=cfg.daemon.startup_backoff_max_s,
            description="loading syncable accounts from the DB",
            log=log,
        )
        n_accounts = len(self._syncable)
        resolved_min_size, resolved_max_size = self._pool_sizes(n_accounts)
        log.info(
            "daemon pool sizing: max_size=%d min_size=%d (accounts=%d, embed=%s, extract=%s)",
            resolved_max_size,
            resolved_min_size,
            n_accounts,
            cfg.search.run_embed_worker,
            cfg.search.run_extract_worker,
        )
        # `open_pool` opens with `wait=False`: it returns immediately and fills
        # lazily on background threads, so it never raises synchronously on an
        # unreachable DB — no retry wrapper here would catch a connectivity
        # failure. The synchronous gate is `_load_syncable_accounts` above; by
        # the time it returns, Postgres has answered, and any brief blip in the
        # window before a worker first acquires a connection is absorbed by the
        # IDLE/poll loops' own 1s→60s backoff.
        self.pool = open_pool(
            self._dsn, min_size=resolved_min_size, max_size=resolved_max_size
        )
        self._account_threads: dict[int, AccountThreads] = {}
        self._worker_threads: list[threading.Thread] = []
        self._current_max_size = resolved_max_size
        self._embedding_backend_factory = embedding_backend_factory
        self._started = False

    def _load_syncable_accounts(self) -> list[Account]:
        """Enumerate live, sync-enabled accounts from the DB (one-shot conn).

        Done before the pool opens because pool sizing depends on the count.
        """
        with psycopg.connect(self._dsn) as conn:
            return list_syncable_accounts(conn)

    def _handle_signal(self, signum: int, frame: Any) -> None:
        log.info("received signal %s; stopping daemon", signum)
        self._stop_event.set()

    def _gmail_secrets(self) -> Path | None:
        return (
            self.cfg.gmail_oauth.client_secrets_file
            if self.cfg.gmail_oauth
            else None
        )

    def _spawn_account(self, account_row: Account) -> None:
        stop_event = threading.Event()
        ctx = WorkerContext(
            account=account_config_from_row(account_row),
            account_id=account_row.id,
            pool=self.pool,
            attachments_root=self.cfg.attachments.root,
            idle_renew_seconds=self.cfg.daemon.idle_renew_seconds,
            poll_seconds=self.cfg.daemon.poll_seconds,
            gmail_client_secrets=self._gmail_secrets(),
            stop=stop_event,
            ssl=self.ssl,
        )
        t_idle = threading.Thread(
            target=run_inbox_idle_loop, args=(ctx,),
            name=f"idle-{account_row.name}", daemon=True,
        )
        t_poll = threading.Thread(
            target=run_poll_loop, args=(ctx,),
            name=f"poll-{account_row.name}", daemon=True,
        )
        t_idle.start()
        t_poll.start()
        self._account_threads[account_row.id] = AccountThreads(
            account_id=account_row.id,
            updated_at=account_row.updated_at,
            stop_event=stop_event,
            idle_thread=t_idle,
            poll_thread=t_poll,
        )
        log.info("started workers for %s", account_row.name)

    def _teardown_account(self, account_id: int) -> None:
        bundle = self._account_threads.pop(account_id, None)
        if bundle is None:
            return
        bundle.stop_event.set()
        grace = self.cfg.daemon.shutdown_grace_seconds
        bundle.idle_thread.join(timeout=grace)
        bundle.poll_thread.join(timeout=grace)
        log.info("stopped workers for account_id=%s", account_id)

    def _running_fingerprints(self) -> dict[int, datetime]:
        return {
            aid: bundle.updated_at
            for aid, bundle in self._account_threads.items()
        }

    def _pool_sizes(self, n_accounts: int) -> tuple[int, int]:
        configured = self.cfg.daemon.pool_max_size
        if configured is None:
            max_size = compute_daemon_pool_size(
                n_accounts=n_accounts,
                run_embed=self.cfg.search.run_embed_worker,
                run_extract=self.cfg.search.run_extract_worker,
            )
        else:
            max_size = configured
        min_size = min(
            n_accounts * 2
            + (1 if self.cfg.search.run_embed_worker else 0)
            + (1 if self.cfg.search.run_extract_worker else 0)
            or 1,
            max_size,
        )
        return min_size, max_size

    def _resize_pool(self) -> None:
        if self.cfg.daemon.pool_max_size is not None:
            return  # operator pinned the size; never auto-resize
        min_size, max_size = self._pool_sizes(len(self._account_threads))
        if max_size != self._current_max_size:
            self.pool.resize(min_size=min_size, max_size=max_size)
            self._current_max_size = max_size
            log.info("daemon pool resized: max_size=%d (accounts=%d)",
                     max_size, len(self._account_threads))

    def reconcile(self) -> None:
        """Converge the running per-account threads on the DB's syncable set.

        A transient DB read failure is logged and swallowed for this tick;
        existing threads keep running and the next tick retries. Apply order is
        teardown -> respawn -> spawn so freed pool slots are reused first.
        """
        try:
            with psycopg.connect(self._dsn) as conn:
                desired_rows = list_syncable_accounts(conn)
        except Exception:
            log.warning(
                "reconcile: failed to read accounts; keeping current threads",
                exc_info=True,
            )
            return

        safe_heartbeat(self.pool, worker_kind="reconcile",
                       account_id=None, state="idle")

        rows_by_id = {row.id: row for row in desired_rows}
        desired = {row.id: row.updated_at for row in desired_rows}
        plan = plan_reconcile(self._running_fingerprints(), desired)
        if plan.is_empty:
            return

        for account_id in plan.to_teardown:
            self._teardown_account(account_id)
        for account_id in plan.to_respawn:
            self._teardown_account(account_id)
            self._spawn_account(rows_by_id[account_id])
        for account_id in plan.to_spawn:
            self._spawn_account(rows_by_id[account_id])

        self._resize_pool()
        log.info(
            "reconcile: spawned=%d torn_down=%d respawned=%d",
            len(plan.to_spawn), len(plan.to_teardown), len(plan.to_respawn),
        )

    def start_workers(self) -> None:
        if self._started:
            return
        self._started = True
        self._clear_heartbeats()
        for account_row in self._syncable:
            self._spawn_account(account_row)
        self._spawn_worker_threads()

    def _clear_heartbeats(self) -> None:
        """Single-instance reset: drop any heartbeat rows from a previous run
        so a crashed predecessor's rows never read as live. Best-effort."""
        try:
            with psycopg.connect(self._dsn) as conn:
                clear_all_heartbeats(conn)
                conn.commit()
        except Exception:
            log.warning("startup heartbeat clear failed", exc_info=True)

    def _spawn_worker_threads(self) -> None:
        if self.cfg.search.run_embed_worker:
            from localmail.search.embed_worker import run_embed_worker  # noqa: PLC0415
            from localmail.search.lang_detect import make_detector  # noqa: PLC0415

            if self._embedding_backend_factory is None:
                from localmail.search.embeddings import FastEmbedBackend  # noqa: PLC0415

                backend = FastEmbedBackend(self.cfg.search)
            else:
                backend = self._embedding_backend_factory(self.cfg.search)
            lang_detector = make_detector(self.cfg.search)
            t_embed = threading.Thread(
                target=run_embed_worker,
                args=(self._stop_event, self.pool, self.cfg.search, backend),
                kwargs={"lang_detector": lang_detector},
                name="embed_worker",
                daemon=True,
            )
            t_embed.start()
            self._worker_threads.append(t_embed)
            log.info(
                "started embed_worker thread (lang_detector=%s)",
                "on" if lang_detector is not None else "off",
            )

        if self.cfg.search.run_extract_worker:
            from localmail.search.extract_worker import run_extract_worker  # noqa: PLC0415

            t_extract = threading.Thread(
                target=run_extract_worker,
                kwargs={
                    "pool": self.pool,
                    "cfg": self.cfg.search,
                    "stop_event": self._stop_event,
                },
                name="extract_worker",
                daemon=True,
            )
            t_extract.start()
            self._worker_threads.append(t_extract)
            log.info("started extract_worker thread")

    def start(self) -> None:
        """Start all worker threads without blocking."""
        self.start_workers()

    def stop(self) -> None:
        """Signal every thread to stop (master event + all per-account events)."""
        self._stop_event.set()
        # Snapshot: reconcile() may mutate _account_threads on the daemon
        # thread while stop() runs from another thread (signal / supervisor).
        for bundle in list(self._account_threads.values()):
            bundle.stop_event.set()

    def join(self, timeout: float | None = None) -> None:
        """Wait for all worker threads to finish."""
        for bundle in list(self._account_threads.values()):
            bundle.idle_thread.join(timeout=timeout)
            bundle.poll_thread.join(timeout=timeout)
        for t in self._worker_threads:
            t.join(timeout=timeout)

    def run_forever(self) -> None:
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, self._handle_signal)
            signal.signal(signal.SIGINT, self._handle_signal)
        self.start_workers()  # initial account spawn + embed/extract workers
        log.info("daemon running; reconciling every %ds",
                 self.cfg.daemon.reload_seconds)
        try:
            while not self._stop_event.wait(self.cfg.daemon.reload_seconds):
                self.reconcile()
        finally:
            log.info("waiting for worker threads to finish")
            for account_id in list(self._account_threads):
                self._teardown_account(account_id)
            for t in self._worker_threads:
                t.join(timeout=self.cfg.daemon.shutdown_grace_seconds)
            self.pool.close()
            log.info("daemon stopped")
