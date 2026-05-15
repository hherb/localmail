"""Daemon: one worker thread per account, plus a per-account IDLE thread on INBOX."""

from __future__ import annotations

import logging
import signal
import threading
from typing import Any

from .config import Config
from .db import open_pool
from .idle import run_inbox_idle_loop
from .poller import run_poll_loop
from .worker import WorkerContext

log = logging.getLogger(__name__)


class Daemon:
    def __init__(self, cfg: Config, *, ssl: bool = True) -> None:
        self.cfg = cfg
        self.ssl = ssl
        self.stop = threading.Event()
        self.pool = open_pool(cfg.database.dsn, min_size=1, max_size=max(4, 2 * len(cfg.accounts)))
        self.threads: list[threading.Thread] = []

    def _handle_signal(self, signum: int, frame: Any) -> None:
        log.info("received signal %s; stopping daemon", signum)
        self.stop.set()

    def start_workers(self) -> None:
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
                stop=self.stop,
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

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)
        if not self.cfg.accounts:
            log.warning("no accounts configured; daemon exiting")
            return
        self.start_workers()
        try:
            while not self.stop.is_set():
                self.stop.wait(60)
        finally:
            log.info("waiting for worker threads to finish")
            for t in self.threads:
                t.join(timeout=10)
            self.pool.close()
            log.info("daemon stopped")
