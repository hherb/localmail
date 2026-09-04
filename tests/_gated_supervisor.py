# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""A ``DaemonSupervisor`` whose ``stop()`` parks until the test releases it.

The busy-guard (``_admit_lifecycle_request`` -> ``_lifecycle_in_flight``) is
keyed on the lifecycle thread being *alive*, and the only thing that kept it
alive in the original tests was the child's grace period. Every busy-guard
assertion therefore had to land inside a wall-clock window — for the route-level
test, two HTTP round trips and a DB-backed status poll had to finish inside
three seconds — which is a race the test can only lose (#299). Measured, the
route test used 6.7 ms of its 3000 ms budget: a 450x margin, so the window was
never what actually failed in #299 (that was a concurrent pytest session,
closed by #336), but a pin whose correctness rests on winning a timer is one a
loaded runner eventually breaks and a future session then learns to ignore.

Parking ``stop()`` on an event makes the window the test's to open and close:
the second request is issued while the first is *provably* still in flight. The
only remaining timer is the 10 s ``GATE_TIMEOUT_S`` backstop, and its expiry is
reported (``gate_timed_out``) rather than silently answering the assertion.

``stop()`` is the only *behaviour* overridden (``__init__`` merely adds the two
events). ``request_stop`` and the guard it consults are the production ones,
which is what keeps this a test double rather than a reimplementation of the
thing under test — and the guard reads the thread, not what the thread is
running, so gating the body changes nothing it can observe.

The kill-vs-grace *decision* is asserted in ``test_daemon_supervisor_lifecycle.py``
by ``test_a_child_that_exits_within_the_margin_is_not_killed`` (exit 0, not -9,
proves no SIGKILL). The SIGTERM-deaf children still in
``test_daemon_supervisor.py`` and ``test_daemon_control_socket.py`` execute the
``TimeoutExpired -> proc.kill()`` branch but assert nothing about it — they hold
a window open to observe STOPPING, which is the shape this module replaces.

The parked thread holds no lock: ``stop()`` waits *before* delegating to
``super().stop()``, which is the call that takes ``_lock``. That is load-bearing
— parking under the lock would block the very ``request_stop`` whose refusal is
being asserted, and the test would hang instead of failing.
"""
from __future__ import annotations

import threading
from typing import Any

from localmail.serve.daemon_supervisor import DaemonSupervisor

#: Bound on every gate wait. A test that fails before releasing the gate must
#: fail on its own assertion rather than hang the suite, so neither wait is
#: allowed to be indefinite. Generous: it is never reached on a passing run.
GATE_TIMEOUT_S = 10.0

#: Grace for the gated supervisor's real stop. The child is an ordinary sleeper
#: that dies on SIGTERM, so the grace wait returns at once and this value is
#: never spent — it exists only because the constructor requires one.
GATED_GRACE_S = 2.0


class GatedStopSupervisor(DaemonSupervisor):
    """A supervisor whose ``stop()`` announces itself and then waits.

    ``stop_entered`` is set as the first statement of the body, so waiting on it
    proves the lifecycle thread is *inside* ``stop()``; ``stop_released`` is
    what lets it leave. Between the two the thread cannot finish, so the
    busy-guard's answer is a property of the test, not of the clock.

    **One gate per instance — do not reuse one across two stop cycles.** Both
    events latch and ``release()`` is permanent, so every signal here means "at
    some point, ever", not "right now". A caller that parked once and parks
    again gets a ``stop_entered.wait()`` that returns instantly on the *previous*
    cycle's signal, and its busy-guard assertion is back on the wall clock with
    nothing failing — #299 reintroduced one caller later. Build a fresh
    supervisor per cycle; if a second cycle is ever genuinely needed, count
    entries rather than latching a flag.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.stop_entered = threading.Event()
        self.stop_released = threading.Event()
        #: Set iff a park gave up on ``GATE_TIMEOUT_S`` instead of being
        #: released. The bound cannot be removed (a test that failed before
        #: reaching its ``finally`` would otherwise hang the suite), so what is
        #: removed instead is its ability to answer wrongly: expiring here lets
        #: the lifecycle thread finish, and the busy-guard would then correctly
        #: admit the second request — a 202 that looks like a broken guard.
        #: A caller that asserts on this flag is told the window closed, not
        #: that the guard is broken — provided it reads the flag BEFORE the
        #: verdict the flag explains, since the verdict aborts the test first.
        #: Same rule as #299's own lesson: a test whose subject is a refusal
        #: must pin *why* it was refused. (No risk number: `NEXT_SESSION.md` is
        #: rewritten every session, so its numbering rots with nothing failing.)
        self.gate_timed_out = False

    def stop(self) -> None:
        """Announce entry, park until released, then stop for real."""
        self.stop_entered.set()
        if not self.stop_released.wait(GATE_TIMEOUT_S):
            self.gate_timed_out = True
        super().stop()

    def release(self) -> None:
        """Let the parked ``stop()`` — and every later one — through.

        Idempotent, and safe to call from a ``finally`` on a failed test: the
        teardown ``stop()`` then passes straight through the gate instead of
        spending ``GATE_TIMEOUT_S``.
        """
        self.stop_released.set()
