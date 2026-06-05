"""Pure job-state helper tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from localmail.importer.job_state import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    is_stale,
    is_terminal,
    should_checkpoint,
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


def _checkpoint(**overrides) -> bool:
    """should_checkpoint with sane defaults; override one axis per assertion."""
    kwargs = dict(
        processed=10,
        processed_at_last_checkpoint=5,
        seconds_since_checkpoint=0.0,
        checkpoint_every=50,
        checkpoint_seconds=2.0,
    )
    kwargs.update(overrides)
    return should_checkpoint(**kwargs)


def test_should_checkpoint_fires_after_first_message():
    # First processed message: nothing flushed yet -> always checkpoint, so a
    # small/slow import shows progress and becomes cancellable immediately.
    assert _checkpoint(processed=1, processed_at_last_checkpoint=0) is True


def test_should_checkpoint_no_unflushed_work_is_false():
    # Already flushed at this count (or a stale equal/over count) -> no-op.
    assert _checkpoint(processed=5, processed_at_last_checkpoint=5) is False
    assert _checkpoint(processed=4, processed_at_last_checkpoint=5) is False


def test_should_checkpoint_count_cadence():
    # Exactly checkpoint_every messages since the last flush fires; one short does not.
    assert _checkpoint(
        processed=55, processed_at_last_checkpoint=5,
        seconds_since_checkpoint=0.0) is True
    assert _checkpoint(
        processed=54, processed_at_last_checkpoint=5,
        seconds_since_checkpoint=0.0) is False


def test_should_checkpoint_time_cadence():
    # Count short of the boundary, but enough wall-clock elapsed -> fire. This
    # decouples responsiveness from per-message cost (issue #163).
    assert _checkpoint(
        processed=6, processed_at_last_checkpoint=5,
        seconds_since_checkpoint=2.0) is True
    assert _checkpoint(
        processed=6, processed_at_last_checkpoint=5,
        seconds_since_checkpoint=1.9) is False


def test_should_checkpoint_disabled_cadences_still_allow_first_message():
    # Both cadences off (<=0): no periodic flush, but the first message still flushes.
    assert _checkpoint(
        processed=1, processed_at_last_checkpoint=0,
        checkpoint_every=0, checkpoint_seconds=0.0) is True
    assert _checkpoint(
        processed=100, processed_at_last_checkpoint=5,
        seconds_since_checkpoint=3600.0,
        checkpoint_every=0, checkpoint_seconds=0.0) is False
