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


def should_checkpoint(
    *,
    processed: int,
    processed_at_last_checkpoint: int,
    seconds_since_checkpoint: float,
    checkpoint_every: int,
    checkpoint_seconds: float,
) -> bool:
    """Decide whether to flush progress + poll for cancel after a message.

    Decouples import progress/cancel responsiveness from per-message cost
    (issue #163). Fires when there is at least one unflushed message AND any of:

      * it is the FIRST processed message — so a small or slow import shows
        progress and becomes cancellable immediately, not only once the first
        count boundary is reached;
      * `checkpoint_every` messages have been processed since the last flush —
        the original count-based cadence;
      * `checkpoint_seconds` of wall-clock have elapsed since the last flush —
        bounds latency for an import that is small in count but slow per
        message (e.g. a handful of very large attachments).

    `checkpoint_every <= 0` disables the count cadence and `checkpoint_seconds
    <= 0` disables the time cadence; the first-message flush always fires.
    """
    unflushed = processed - processed_at_last_checkpoint
    if unflushed <= 0:
        return False
    if processed_at_last_checkpoint == 0:
        return True
    if checkpoint_every > 0 and unflushed >= checkpoint_every:
        return True
    if checkpoint_seconds > 0 and seconds_since_checkpoint >= checkpoint_seconds:
        return True
    return False
