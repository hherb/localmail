# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The pure shutdown-budget arithmetic shared by the daemon and its supervisor
(#221 A).

Two budgets used to be the same number read from two places
(`daemon.shutdown_grace_seconds`), while the daemon actually spent a multiple of
it and the supervisor waited exactly one. These helpers are the one place that
relationship is written down.
"""
from __future__ import annotations

import pytest

from localmail.shutdown_budget import (
    SUPERVISOR_KILL_MARGIN_S,
    remaining_seconds,
    supervisor_kill_after,
)


# --- remaining_seconds ----------------------------------------------------

def test_remaining_seconds_is_the_gap_to_the_deadline() -> None:
    assert remaining_seconds(deadline=100.0, now=40.0) == 60.0


def test_remaining_seconds_clamps_to_zero_at_the_deadline() -> None:
    assert remaining_seconds(deadline=100.0, now=100.0) == 0.0


def test_remaining_seconds_clamps_to_zero_past_the_deadline() -> None:
    """A join() timeout must never go negative.

    `Thread.join(timeout=-1)` returns immediately rather than raising, so a
    negative value would *look* like it worked while silently skipping the wait
    for every remaining thread. Clamping makes an exhausted budget mean
    "poll once, don't block", which is what the caller intends.
    """
    assert remaining_seconds(deadline=100.0, now=250.0) == 0.0


def test_remaining_seconds_is_pure() -> None:
    """Same inputs, same answer — no clock read inside."""
    assert remaining_seconds(deadline=10.0, now=3.0) == remaining_seconds(
        deadline=10.0, now=3.0
    )


# --- supervisor_kill_after ------------------------------------------------

def test_supervisor_kill_after_exceeds_the_childs_own_budget() -> None:
    """The whole point of #221 A: the supervisor must outwait the child.

    The child spends up to `shutdown_grace_seconds` joining its workers and
    then still has to close its pool and exit. A supervisor that waits exactly
    the same number SIGKILLs a child that was shutting down perfectly normally.
    """
    assert supervisor_kill_after(30.0) > 30.0


def test_supervisor_kill_after_adds_the_named_margin() -> None:
    assert supervisor_kill_after(30.0) == 30.0 + SUPERVISOR_KILL_MARGIN_S


def test_supervisor_kill_after_still_exceeds_a_zero_budget() -> None:
    """grace=0 is a legal config ("kill it now"); the margin still applies so
    the child gets a chance to exit on its own SIGTERM."""
    assert supervisor_kill_after(0.0) == SUPERVISOR_KILL_MARGIN_S


@pytest.mark.parametrize("grace", [0.0, 0.5, 5.0, 30.0, 600.0])
def test_supervisor_kill_after_is_monotone_in_the_child_budget(grace: float) -> None:
    assert supervisor_kill_after(grace) > grace
