# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Unit tests for the pure account-reconcile diff planner (2B.1)."""

from __future__ import annotations

from datetime import datetime, timezone

from localmail.daemon_reconcile import ReconcilePlan, plan_reconcile


def _ts(day: int) -> datetime:
    return datetime(2026, 1, day, tzinfo=timezone.utc)


def test_empty_both_sides_is_noop():
    plan = plan_reconcile({}, {})
    assert plan == ReconcilePlan(to_spawn=(), to_teardown=(), to_respawn=())
    assert plan.is_empty


def test_spawn_only_for_new_account():
    plan = plan_reconcile({}, {7: _ts(1)})
    assert plan.to_spawn == (7,)
    assert plan.to_teardown == ()
    assert plan.to_respawn == ()
    assert not plan.is_empty


def test_teardown_only_for_vanished_account():
    plan = plan_reconcile({7: _ts(1)}, {})
    assert plan.to_teardown == (7,)
    assert plan.to_spawn == ()
    assert plan.to_respawn == ()


def test_respawn_when_updated_at_changes():
    plan = plan_reconcile({7: _ts(1)}, {7: _ts(2)})
    assert plan.to_respawn == (7,)
    assert plan.to_spawn == ()
    assert plan.to_teardown == ()


def test_noop_when_identical():
    plan = plan_reconcile({7: _ts(1), 9: _ts(3)}, {7: _ts(1), 9: _ts(3)})
    assert plan.is_empty


def test_combined_plan_is_sorted_and_disjoint():
    running = {1: _ts(1), 2: _ts(1), 3: _ts(1)}
    desired = {2: _ts(2), 3: _ts(1), 4: _ts(1)}  # 1 gone, 2 changed, 3 same, 4 new
    plan = plan_reconcile(running, desired)
    assert plan.to_spawn == (4,)
    assert plan.to_teardown == (1,)
    assert plan.to_respawn == (2,)
