# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The shutdown budget, and how each side of it is spent.

Two processes hold a stake in one number. The daemon
(`Daemon.run_forever`'s teardown) spends `daemon.shutdown_grace_seconds`
winding its worker threads down; `DaemonSupervisor.stop()` waits before
SIGKILLing the child. Before #221 both read *the same* config value, but the
daemon's teardown joined each thread with its own timeout — `2 × grace` per
account, non-overlapping — so with two or more accounts a perfectly healthy
child was SIGKILLed on every stop and restart.

The fix has two halves and both live here so they cannot drift apart:

* `wind_down_threads` (with `remaining_seconds`) lets the daemon spend the
  budget as **one wall-clock deadline** across every join instead of a
  per-thread timeout, making `shutdown_grace_seconds` mean what its name says.
* `supervisor_kill_after` derives the supervisor's kill deadline from that
  budget, adding the margin the child needs *after* its last join (closing the
  pool, final log line, interpreter teardown).

`remaining_seconds` and `supervisor_kill_after` are pure. `wind_down_threads`
lives beside them rather than in `daemon.py` because it is the sole consumer of
the first and the counterpart to the second — split across two files, one is
edited without seeing the other, which is exactly how the budgets drifted apart
in the first place. It does no IO: it only calls `set()` and `join()` on the
objects it is handed, and reads an injected clock.

Top-level rather than under `serve/` because both a serve-side module
(`serve/app.py`) and the daemon itself import it — same placement reasoning as
`retry.py` and `ocr_policy.py`.
"""
from __future__ import annotations

import time
from typing import Callable, Protocol, Sequence

# Slack between the child's own shutdown budget and the supervisor's SIGKILL.
# It covers the fixed work that follows the last thread join — `pool.close()`,
# the final log line, interpreter teardown — none of which is inside the
# grace budget. Deliberately a constant rather than a config knob: it is not a
# policy an operator would tune, it is the fixed cost of an orderly exit. Any
# value > 0 restores the contract ("the supervisor outwaits the child"); 5s is
# generous enough that a loaded host does not turn a clean stop into a SIGKILL.
SUPERVISOR_KILL_MARGIN_S = 5.0


def remaining_seconds(*, deadline: float, now: float) -> float:
    """Seconds left until `deadline`, never negative.

    Both arguments come from the same monotonic clock. The clamp matters:
    `threading.Thread.join(timeout=<negative>)` returns immediately instead of
    raising, so passing a negative remainder would silently skip the wait for
    every thread after the budget ran out while still *looking* like a join.
    Clamped, an exhausted budget means "check once, don't block" — which is
    what a caller past its deadline actually wants.
    """
    return max(0.0, deadline - now)


def supervisor_kill_after(child_grace_seconds: float) -> float:
    """How long the supervisor waits after SIGTERM before it SIGKILLs.

    Strictly greater than the child's own shutdown budget: the child is still
    doing bounded, healthy work (closing its connection pool, logging, exiting)
    when its last join returns, and killing it at that exact instant turns
    every ordinary stop into a SIGKILL.
    """
    return child_grace_seconds + SUPERVISOR_KILL_MARGIN_S


class Stoppable(Protocol):
    """A `threading.Event`-shaped stop signal."""

    def set(self) -> None: ...


class Joinable(Protocol):
    """A `threading.Thread`-shaped joinable worker."""

    def join(self, timeout: float | None = None) -> None: ...


def wind_down_threads(
    *,
    stop_events: Sequence[Stoppable],
    threads: Sequence[Joinable],
    grace_seconds: float,
    clock: Callable[[], float] = time.monotonic,
) -> float:
    """Signal every worker, then join them all against ONE deadline (#221 A).

    Setting every stop event *before* the first join is the load-bearing part.
    The old teardown interleaved them — `Daemon._teardown_account` per account,
    each joining idle then poll with a full `shutdown_grace_seconds` timeout
    apiece — so account 2 did not even learn it should stop until account 1's
    joins had expired, and the real worst case was `2 × accounts × grace`.
    Meanwhile `DaemonSupervisor.stop()` waited exactly one `grace` before
    SIGKILL, so with two or more accounts an ordinary stop or restart killed a
    perfectly healthy child.

    Signalled up-front the workers wind down concurrently, so the budget bounds
    the *slowest* one instead of their sum and `shutdown_grace_seconds` means
    what its name says. Returns the unspent remainder for the caller to log.

    `clock` is injected so the budget arithmetic is unit-testable without
    sleeping.
    """
    for event in stop_events:
        event.set()
    deadline = clock() + grace_seconds
    for thread in threads:
        thread.join(timeout=remaining_seconds(deadline=deadline, now=clock()))
    return remaining_seconds(deadline=deadline, now=clock())
