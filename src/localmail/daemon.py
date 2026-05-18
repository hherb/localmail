"""Daemon: one worker thread per account, plus a per-account IDLE thread on INBOX."""

from __future__ import annotations

import logging
import signal
import threading
from typing import Any

from psycopg_pool import ConnectionPool

from .config import Config
from .db import open_pool
from .idle import run_inbox_idle_loop
from .poller import run_poll_loop
from .worker import WorkerContext

log = logging.getLogger(__name__)


class Daemon:
    def __init__(
        self,
        cfg: Config,
        *,
        ssl: bool = True,
        dsn: str | None = None,
        embedding_backend_factory=None,
    ) -> None:
        self.cfg = cfg
        self.ssl = ssl
        self._dsn = dsn or cfg.database.dsn
        self._stop_event = threading.Event()
        self.pool = open_pool(self._dsn, min_size=1, max_size=max(4, 2 * len(cfg.accounts)))
        self.threads: list[threading.Thread] = []
        self._embedding_backend_factory = embedding_backend_factory
        self._embed_pool: ConnectionPool | None = None
        self._started = False

    def _handle_signal(self, signum: int, frame: Any) -> None:
        log.info("received signal %s; stopping daemon", signum)
        self._stop_event.set()

    def start_workers(self) -> None:
        if self._started:
            return
        self._started = True
        gmail_secrets = (
            self.cfg.gmail_oauth.client_secrets_file if self.cfg.gmail_oauth else None
        )
        for account in self.cfg.accounts:
            ctx = WorkerContext(
                account=account,
                pool=self.pool,
                attachments_root=self.cfg.attachments.root,
                idle_renew_seconds=self.cfg.daemon.idle_renew_seconds,
                poll_seconds=account.poll_seconds or self.cfg.daemon.poll_seconds,
                gmail_client_secrets=gmail_secrets,
                stop=self._stop_event,
                ssl=self.ssl,
            )
            t_idle = threading.Thread(
                target=run_inbox_idle_loop,
                args=(ctx,),
                name=f"idle-{account.name}",
                daemon=True,
            )
            t_poll = threading.Thread(
                target=run_poll_loop,
                args=(ctx,),
                name=f"poll-{account.name}",
                daemon=True,
            )
            t_idle.start()
            t_poll.start()
            self.threads += [t_idle, t_poll]
            log.info("started workers for %s", account.name)

        if self.cfg.search.run_embed_worker:
            from localmail.search.embed_worker import run_embed_worker  # noqa: PLC0415
            from localmail.search.lang_detect import make_detector  # noqa: PLC0415

            if self._embedding_backend_factory is None:
                from localmail.search.embeddings import FastEmbedBackend  # noqa: PLC0415

                backend = FastEmbedBackend(self.cfg.search)
            else:
                backend = self._embedding_backend_factory(self.cfg.search)
            lang_detector = make_detector(self.cfg.search)
            embed_pool = open_pool(self._dsn)
            self._embed_pool = embed_pool
            t_embed = threading.Thread(
                target=run_embed_worker,
                args=(self._stop_event, embed_pool, self.cfg.search, backend),
                kwargs={"lang_detector": lang_detector},
                name="embed_worker",
                daemon=True,
            )
            t_embed.start()
            self.threads.append(t_embed)
            log.info(
                "started embed_worker thread (lang_detector=%s)",
                "on" if lang_detector is not None else "off",
            )

        if self.cfg.search.run_extract_worker:
            import psycopg  # noqa: PLC0415
            from localmail.search.extract_worker import run_extract_worker  # noqa: PLC0415

            dsn = self._dsn
            t_extract = threading.Thread(
                target=run_extract_worker,
                kwargs={
                    "conn_factory": lambda: psycopg.connect(dsn),
                    "cfg": self.cfg.search,
                    "stop_event": self._stop_event,
                },
                name="extract_worker",
                daemon=True,
            )
            t_extract.start()
            self.threads.append(t_extract)
            log.info("started extract_worker thread")

    def start(self) -> None:
        """Start all worker threads without blocking."""
        self.start_workers()

    def stop(self) -> None:
        """Signal all threads to stop."""
        self._stop_event.set()

    def _close_embed_pool(self) -> None:
        """Close the embed worker's connection pool, logging any error."""
        if self._embed_pool is None:
            return
        try:
            self._embed_pool.close()
        except Exception as exc:  # noqa: BLE001 — shutdown best-effort
            log.warning("error closing embed pool: %s", exc, exc_info=True)

    def join(self, timeout: float | None = None) -> None:
        """Wait for all worker threads to finish and close pools."""
        for t in self.threads:
            t.join(timeout=timeout)
        self._close_embed_pool()

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)
        if not self.cfg.accounts:
            log.warning("no accounts configured; daemon exiting")
            return
        self.start_workers()
        try:
            while not self._stop_event.is_set():
                self._stop_event.wait(60)
        finally:
            log.info("waiting for worker threads to finish")
            for t in self.threads:
                t.join(timeout=10)
            self.pool.close()
            self._close_embed_pool()
            log.info("daemon stopped")
