"""Pure job-state helper tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from localmail.importer.job_state import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    is_stale,
    is_terminal,
)


def test_status_partitions():
    assert set(ACTIVE_STATUSES) == {"pending", "running"}
    assert set(TERMINAL_STATUSES) == {"completed", "failed", "cancelled"}
    assert is_terminal("completed") is True
    assert is_terminal("running") is False


def _now():
    return datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_is_stale_only_when_running_and_overdue():
    now = _now()
    old = now - timedelta(seconds=120)
    fresh = now - timedelta(seconds=10)
    assert is_stale(status="running", last_progress_at=old, now=now, stale_seconds=60) is True
    assert is_stale(status="running", last_progress_at=fresh, now=now, stale_seconds=60) is False
    # Non-running statuses are never stale.
    assert is_stale(status="completed", last_progress_at=old, now=now, stale_seconds=60) is False
    # No heartbeat yet (pending → running not yet checkpointed) is not stale.
    assert is_stale(status="running", last_progress_at=None, now=now, stale_seconds=60) is False
