# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Shared context for the per-account daemon workers (IDLE thread + poll thread)."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

from psycopg_pool import ConnectionPool

from .config import AccountConfig
from .fetch_retry import DEFAULT_MAX_BODY_FETCH_RETRIES
from .imap_client import DEFAULT_IMAP_TIMEOUT_SECONDS


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
    imap_timeout_s: float = DEFAULT_IMAP_TIMEOUT_SECONDS
    max_body_fetch_retries: int = DEFAULT_MAX_BODY_FETCH_RETRIES
