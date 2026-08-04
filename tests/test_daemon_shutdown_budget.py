# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""`shutdown_budget.wind_down_threads` — one wall-clock budget for the whole
teardown (#221 A).

The bug: `run_forever`'s teardown joined every thread with its *own*
`shutdown_grace_seconds` timeout — idle then poll per account, sequentially —
so the real worst case was `2 × accounts × grace` while `DaemonSupervisor.stop()`
waited exactly one `grace` before SIGKILL. With two or more accounts a healthy
child was killed on every stop and restart.

These tests drive a fake clock and fake threads, so they assert the *budget
arithmetic* deterministically — no sleeping, no real threads, no flakiness.
"""
from __future__ import annotations

import threading

from localmail.shutdown_budget import wind_down_threads


class _FakeClock:
    """Monotonic clock the test advances by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _NeverFinishingThread:
    """A thread that always burns its whole join timeout — the worst case, and
    the one that used to multiply the budget."""

    def __init__(self, name: str, clock: _FakeClock, log: list) -> None:
        self.name = name
        self._clock = clock
        self._log = log
        self.timeouts: list[float] = []

    def join(self, timeout: float | None = None) -> None:
        assert timeout is not None
        self._log.append(("join", self.name, timeout))
        self.timeouts.append(timeout)
        self._clock.advance(timeout)

    def is_alive(self) -> bool:
        return True


class _RecordingEvent:
    def __init__(self, name: str, log: list) -> None:
        self.name = name
        self._log = log
        self.is_set_flag = False

    def set(self) -> None:
        self.is_set_flag = True
        self._log.append(("set", self.name))


def test_every_stop_event_is_set_before_the_first_join() -> None:
    """The load-bearing ordering claim.

    Signalling all workers *first* is what turns the budget into a total: the
    threads then wind down concurrently, so the deadline bounds the slowest one
    rather than the sum. Interleaving set/join (the old `_teardown_account`
    loop) means account 2 does not even learn it should stop until account 1's
    joins have expired.
    """
    log: list = []
    clock = _FakeClock()
    events = [_RecordingEvent(f"ev{i}", log) for i in range(3)]
    threads = [_NeverFinishingThread(f"t{i}", clock, log) for i in range(3)]

    wind_down_threads(
        stop_events=events, threads=threads, grace_seconds=30.0, clock=clock
    )

    kinds = [entry[0] for entry in log]
    assert kinds.count("set") == 3
    first_join = kinds.index("join")
    assert set(kinds[:first_join]) == {"set"}, (
        f"a join happened before every stop event was set: {log}"
    )


def test_total_join_time_never_exceeds_the_budget() -> None:
    """Six never-finishing threads (three accounts × idle+poll) must still cost
    at most one `grace`, not `2 × 3 × grace`."""
    clock = _FakeClock()
    threads = [_NeverFinishingThread(f"t{i}", clock, []) for i in range(6)]
    started = clock.now

    wind_down_threads(
        stop_events=[], threads=threads, grace_seconds=30.0, clock=clock
    )

    assert clock.now - started == 30.0
    assert sum(sum(t.timeouts) for t in threads) == 30.0


def test_threads_after_the_budget_is_spent_are_joined_with_zero_not_negative() -> None:
    """Past the deadline the remaining joins must still be *called* (so a
    thread that already finished is reaped) but must not block, and must never
    receive a negative timeout — `join(-1)` silently returns without waiting."""
    clock = _FakeClock()
    threads = [_NeverFinishingThread(f"t{i}", clock, []) for i in range(4)]

    wind_down_threads(
        stop_events=[], threads=threads, grace_seconds=10.0, clock=clock
    )

    assert threads[0].timeouts == [10.0]
    for t in threads[1:]:
        assert t.timeouts == [0.0]
    assert all(to >= 0.0 for t in threads for to in t.timeouts)


def test_returns_the_unspent_remainder() -> None:
    """Quick threads leave budget on the table; the caller logs the remainder
    so an operator can see whether `shutdown_grace_seconds` is tight."""
    clock = _FakeClock()

    class _Instant:
        def join(self, timeout: float | None = None) -> None:
            pass

    left = wind_down_threads(
        stop_events=[], threads=[_Instant(), _Instant()],
        grace_seconds=30.0, clock=clock,
    )
    assert left == 30.0


def test_works_with_real_events_and_threads() -> None:
    """Integration sanity: the real `threading` types satisfy the same shape."""
    stop = threading.Event()
    ran = threading.Event()

    def _body() -> None:
        stop.wait(timeout=5.0)
        ran.set()

    t = threading.Thread(target=_body, daemon=True)
    t.start()

    left = wind_down_threads(stop_events=[stop], threads=[t], grace_seconds=5.0)

    assert ran.is_set()
    assert not t.is_alive()
    assert 0.0 <= left <= 5.0
