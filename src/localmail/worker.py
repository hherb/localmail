"""Shared context for the per-account daemon workers (IDLE thread + poll thread)."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

from psycopg_pool import ConnectionPool

from .config import AccountConfig


@dataclass
class WorkerContext:
    account: AccountConfig
    account_id: int
    pool: ConnectionPool
    attachments_root: Path
    idle_renew_seconds: int
    poll_seconds: int
    gmail_client_secrets: Path | None
    stop: threading.Event
    ssl: bool = True
