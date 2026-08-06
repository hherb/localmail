# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Daemon: one worker thread per account, plus a per-account IDLE thread on INBOX."""

from __future__ import annotations

import logging
import signal
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import psycopg

from .api.admin.accounts import Account, list_syncable_accounts
from .api.admin.daemon import (
    DaemonCommand,
    claim_commands,
    mark_command,
)
from .blob_temps import sweep_blob_temps
from .config import Config
from .daemon_accounts import account_config_from_row
from .daemon_reconcile import plan_reconcile
from .db import compute_daemon_pool_size, open_pool
from .heartbeat import (
    clear_account_heartbeats,
    clear_all_heartbeats,
    safe_heartbeat,
)
from .idle import run_inbox_idle_loop
from .poller import run_poll_loop
from .retry import retry_with_backoff
from .shutdown_budget import Joinable, wind_down_threads
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
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = cfg
        self.ssl = ssl
        self._dsn = dsn or cfg.database.dsn
        # Injected so the shutdown budget is testable without sleeping.
        self._clock = clock
        self._stop_event = stop_event or threading.Event()
        self._reconcile_wake = threading.Event()
        # Set while the command listener holds a LISTEN connection so stop() can
        # close it cross-thread and interrupt the in-flight notifies() wait (the
        # only way to exit faster than command_listen_poll_seconds on shutdown).
        self._listener_conn: psycopg.Connection | None = None
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

    def _connect(self) -> psycopg.Connection:
        """Open a fresh (non-pool) connection with every phase bounded.

        The daemon opens fresh connects in three places — startup account read,
        reconcile, and the heartbeat clear — none of which borrow from the pool.
        Routing them all through here applies three complementary bounds so no
        phase can hang indefinitely on a network fault (#140, #142):

        - ``connect_timeout`` (``db_connect_timeout_s``) — the TCP handshake.
        - ``statement_timeout`` (``db_statement_timeout_s``) — server-side query
          execution; catches a slow / stuck query, not a network black-hole.
        - ``tcp_user_timeout`` (``db_tcp_user_timeout_ms``) — the actual
          post-connect black-hole bound: forces the socket closed after that
          many ms of unacknowledged data, the only one of the three that breaks
          a client stuck in ``recv`` when packets are dropped *after* connect
          (Linux-effective; libpq ignores it where ``TCP_USER_TIMEOUT`` is
          absent, e.g. macOS).

        ``self._dsn`` must not itself carry an ``options=`` conninfo entry — the
        kwarg below replaces rather than merges it; the daemon's DSN never does.
        """
        return psycopg.connect(
            self._dsn,
            connect_timeout=self.cfg.daemon.db_connect_timeout_s,
            tcp_user_timeout=self.cfg.daemon.db_tcp_user_timeout_ms,
            options=f"-c statement_timeout={self.cfg.daemon.db_statement_timeout_s}s",
        )

    def _load_syncable_accounts(self) -> list[Account]:
        """Enumerate live, sync-enabled accounts from the DB (one-shot conn).

        Done before the pool opens because pool sizing depends on the count.
        """
        with self._connect() as conn:
            return list_syncable_accounts(conn)

    def _handle_signal(self, signum: int, frame: Any) -> None:
        log.info("received signal %s; stopping daemon", signum)
        self._stop_event.set()
        self._reconcile_wake.set()
        self._interrupt_listener()

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
            imap_timeout_s=self.cfg.daemon.imap_timeout_s,
            max_body_fetch_hold_s=self.cfg.daemon.max_body_fetch_hold_s,
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
        self._clear_account_heartbeats(account_id)
        log.info("stopped workers for account_id=%s", account_id)

    def _clear_account_heartbeats(self, account_id: int) -> None:
        """Drop the torn-down account's idle/poll heartbeat rows so a paused or
        removed account no longer reads as a live thread. Best-effort — a
        deleted account's rows also vanish via ON DELETE CASCADE."""
        try:
            with self.pool.connection() as conn:
                clear_account_heartbeats(conn, account_id)
        except Exception:
            log.warning("heartbeat clear failed for account_id=%s",
                        account_id, exc_info=True)

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

    def _drain_commands(self) -> None:
        """Claim and apply every queued daemon command, marking each done/failed.

        Runs at the top of each reconcile tick on a fresh bounded connection. The
        FOR UPDATE lock is held across apply+mark until the single commit, so a
        concurrent consumer (defensive — single daemon assumed) skips claimed
        rows. For restart-account that hold spans the thread-join inside
        _teardown_account (bounded by shutdown_grace_seconds per account); no
        idle_in_transaction timeout is set on these connections, so a stalled
        worker can hold the lock that long — acceptable under the single-daemon
        model. A drain failure is logged and swallowed: the transaction rolls
        back (claimed rows revert to 'queued'), existing threads keep running,
        and the next tick re-claims and retries."""
        try:
            with self._connect() as conn:
                commands = claim_commands(conn)
                for cmd in commands:
                    try:
                        msg = self._apply_command(cmd)
                        mark_command(conn, cmd.id, state="done", result_msg=msg)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("daemon command %s (id=%s) failed",
                                    cmd.command, cmd.id, exc_info=True)
                        # If even the failure-mark can't be written (connection
                        # in an error state), let the outer handler roll the
                        # whole batch back so it's re-claimed next tick rather
                        # than half-committed.
                        mark_command(conn, cmd.id, state="failed",
                                     result_msg=str(exc))
                conn.commit()
        except Exception:
            log.warning("command drain failed; will retry next tick", exc_info=True)

    def _apply_command(self, cmd: DaemonCommand) -> str:
        """Apply one command against the live thread registry; return a result
        message. `restart-account` only tears the bundle down — the same-tick
        reconcile diff respawns it if the account is still syncable (running set
        now lacks it; desired set still has it)."""
        if cmd.command == "reload-now":
            return "reconcile triggered"
        if cmd.command == "drain-stop":
            self._stop_event.set()
            self._reconcile_wake.set()
            return "daemon stopping"
        if cmd.command == "restart-account":
            assert cmd.account_id is not None  # DB CHECK guarantees this
            self._teardown_account(cmd.account_id)
            return f"account {cmd.account_id} torn down for restart"
        raise ValueError(f"unknown daemon command {cmd.command!r}")

    def _run_command_listener(self) -> None:
        """LISTEN the daemon_commands channel; set the reconcile wake on each
        NOTIFY so run_forever reconciles early instead of waiting out
        reload_seconds. A dedicated autocommit connection (LISTEN must be visible
        immediately and notifications are only delivered outside a transaction).
        statement_timeout is disabled on this long-lived connection (it would be
        irrelevant during a socket wait, but disable it for clarity). Reconnects
        with the same fresh-connect bounds on any error; exits on the stop event.
        The poll path remains authoritative — this loop only reduces latency."""
        poll = self.cfg.daemon.command_listen_poll_seconds
        while not self._stop_event.is_set():
            try:
                with self._connect() as conn:
                    conn.autocommit = True
                    conn.execute("SET statement_timeout = 0")
                    conn.execute("LISTEN daemon_commands")
                    # Publish for stop()/_handle_signal to close cross-thread,
                    # interrupting the notifies() wait below immediately.
                    self._listener_conn = conn
                    try:
                        while not self._stop_event.is_set():
                            for _note in conn.notifies(timeout=poll, stop_after=1):
                                self._reconcile_wake.set()
                    finally:
                        self._listener_conn = None
            except Exception:
                if self._stop_event.is_set():
                    break
                log.warning("command listener error; reconnecting",
                            exc_info=True)
                self._stop_event.wait(poll)  # brief backoff before retry

    def _interrupt_listener(self) -> None:
        """Close the listener's LISTEN connection (if any) to break it out of a
        blocking notifies() wait at once. Best-effort and idempotent: the
        connection may already be gone (None) or mid-reconnect, and closing it
        cross-thread races the listener's own `with` cleanup — either order ends
        with a closed connection, and the listener's `except Exception` swallows
        the resulting error while `_stop_event` is set."""
        conn = self._listener_conn
        if conn is None:
            return
        try:
            conn.close()
        except Exception:
            pass

    def reconcile(self) -> None:
        """Converge the running per-account threads on the DB's syncable set.

        A transient DB read failure is logged and swallowed for this tick;
        existing threads keep running and the next tick retries. Apply order is
        teardown -> respawn -> spawn so freed pool slots are reused first.
        """
        self._drain_commands()
        if self._stop_event.is_set():
            return  # drain-stop fired; run_forever handles shutdown
        try:
            with self._connect() as conn:
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
        self._sweep_blob_temps()
        for account_row in self._syncable:
            self._spawn_account(account_row)
        self._spawn_worker_threads()

    def _sweep_blob_temps(self) -> None:
        """Collect attachment temps a hard kill stranded (#237). Best-effort.

        Startup is the natural moment: whatever killed the previous process is
        exactly what leaves these behind. Never fatal — a leaked temp costs
        disk, a raise here costs the whole daemon.
        """
        try:
            # #269: on a cold cache this walk has taken minutes, during which
            # the heartbeat table is empty (just wiped above) and the last log
            # line was "pool sizing" — announce it, and report unconditionally.
            log.info("sweeping blob temps under %s ...", self.cfg.attachments.root)
            started = time.monotonic()
            result = sweep_blob_temps(
                self.cfg.attachments.root,
                max_age_s=self.cfg.attachments.temp_max_age_s,
                now=time.time(),
            )
            log.info(
                "blob-temp sweep done: walked=%d scanned=%d removed=%d bytes=%d "
                "errors=%d took=%.1fs",
                result.walked, result.scanned, result.removed,
                result.bytes_reclaimed, result.errors, time.monotonic() - started,
            )
        except Exception:
            log.warning("startup blob-temp sweep failed", exc_info=True)

    def _clear_heartbeats(self) -> None:
        """Single-instance reset: drop any heartbeat rows from a previous run
        so a crashed predecessor's rows never read as live. Best-effort."""
        try:
            with self._connect() as conn:
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
        self._reconcile_wake.set()
        self._interrupt_listener()
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
        listener: threading.Thread | None = None
        if self.cfg.daemon.command_listen_enabled:
            listener = threading.Thread(
                target=self._run_command_listener,
                name="command_listener", daemon=True,
            )
            listener.start()
            log.info("started command listener thread")
        log.info("daemon running; reconciling every %ds (wake on NOTIFY)",
                 self.cfg.daemon.reload_seconds)
        try:
            while True:
                # Wake on a NOTIFY (listener) or stop (signal/drain-stop), else
                # fall through after reload_seconds for the authoritative poll.
                self._reconcile_wake.wait(self.cfg.daemon.reload_seconds)
                self._reconcile_wake.clear()
                if self._stop_event.is_set():
                    break
                self.reconcile()
                if self._stop_event.is_set():
                    break  # drain-stop fired inside reconcile
        finally:
            self._shutdown_all_threads(listener)
            self.pool.close()
            log.info("daemon stopped")

    def _shutdown_all_threads(self, listener: threading.Thread | None) -> None:
        """Wind every worker down against one shared budget (#221 A).

        Distinct from `_teardown_account`, which keeps its own per-account
        timeout: that path removes *one* account from a daemon that keeps
        running, so it has no global deadline to share. Here the whole process
        is leaving and `shutdown_grace_seconds` is the total the supervisor
        waits on, so it is spent once across everything.
        """
        log.info("waiting for worker threads to finish")
        # Defensive: reaching the teardown by an exception rather than the stop
        # path would otherwise leave the embed/extract workers and the command
        # listener unsignalled, so every one of them would burn the full budget.
        self._stop_event.set()
        self._interrupt_listener()

        bundles = [
            self._account_threads.pop(account_id)
            for account_id in list(self._account_threads)
        ]
        threads: list[Joinable] = [
            t for b in bundles for t in (b.idle_thread, b.poll_thread)
        ]
        threads += self._worker_threads
        if listener is not None:
            threads.append(listener)

        left = wind_down_threads(
            stop_events=[b.stop_event for b in bundles],
            threads=threads,
            grace_seconds=self.cfg.daemon.shutdown_grace_seconds,
            clock=self._clock,
        )
        if left <= 0.0:
            log.warning(
                "shutdown grace of %.1fs was exhausted; some worker threads "
                "may not have finished",
                self.cfg.daemon.shutdown_grace_seconds,
            )
        # After the joins, so slow DB IO cannot eat the join budget.
        for bundle in bundles:
            self._clear_account_heartbeats(bundle.account_id)
