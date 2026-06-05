"""Pure helpers for import-job status reasoning (no DB)."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

JobStatus = Literal["pending", "running", "completed", "failed", "cancelled"]

ACTIVE_STATUSES: tuple[str, ...] = ("pending", "running")
TERMINAL_STATUSES: tuple[str, ...] = ("completed", "failed", "cancelled")


def is_terminal(status: str) -> bool:
    return status in TERMINAL_STATUSES


def is_stale(
    *, status: str, last_progress_at: datetime | None, now: datetime, stale_seconds: int,
) -> bool:
    """True iff a running job has not checkpointed within `stale_seconds`.

    Only `running` jobs can be stale. A job with no `last_progress_at` yet
    (just flipped to running, not yet checkpointed) is treated as fresh.
    """
    if status != "running" or last_progress_at is None:
        return False
    return (now - last_progress_at).total_seconds() > stale_seconds
