# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The gated supervisor test double is itself pinned (#299).

`tests/_gated_supervisor.py` is what makes two busy-guard assertions
deterministic, so a silent regression in it would quietly hand both of them
back their wall-clock race — the failure mode this repo keeps re-learning: a
pin that goes quiet with nothing failing. Both branches of the park are covered
here, because the released branch alone leaves the timeout branch free to be
permanently unreachable, and `gate_timed_out` is asserted by both callers.
"""
from __future__ import annotations

import sys
import threading

from localmail.serve.daemon_supervisor import SupervisorState

from tests import _gated_supervisor
from tests._gated_supervisor import GATED_GRACE_S, GatedStopSupervisor

_SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]

#: Long enough that a released park is never mistaken for an expired one, short
#: enough that the expiry test does not dominate the file's runtime.
_SHORT_GATE_S = 0.05

#: How long a *broken* gate is given to reveal itself by letting the lifecycle
#: body run to completion. Never spent on the passing path — a working gate is
#: parked for `GATE_TIMEOUT_S`, so this bound is one-sided by construction.
_PARK_PROOF_S = 0.5


def _supervisor() -> GatedStopSupervisor:
    return GatedStopSupervisor(argv=_SLEEPER, grace_seconds=GATED_GRACE_S)


def _lifecycle_thread() -> threading.Thread:
    """The supervisor's one lifecycle thread, by its production name."""
    threads = [
        t for t in threading.enumerate()
        if t.name == "daemon-supervisor-lifecycle"
    ]
    assert len(threads) == 1, f"expected exactly one, got {threads}"
    return threads[0]


def test_a_gated_stop_parks_instead_of_finishing() -> None:
    """While the gate is shut the lifecycle body has not reached the real stop.

    Asserting only that the state is still STOPPING does NOT pin this — it was
    written that way first, and removing the park left it green, because the
    real stop takes milliseconds (SIGTERM plus a reap) against a microsecond
    assertion path. That is the same lucky-win the gate exists to remove, so
    the pin joins the thread instead.

    The timeout here bounds only how long a *broken* gate is given to reveal
    itself; a working gate parks for `GATE_TIMEOUT_S`, so the passing path
    never depends on the clock and no amount of load can make it flake.
    """
    sup = _supervisor()
    sup.start()
    try:
        sup.request_stop()
        assert sup.stop_entered.wait(_gated_supervisor.GATE_TIMEOUT_S)
        thread = _lifecycle_thread()
        thread.join(_PARK_PROOF_S)
        assert thread.is_alive(), "the lifecycle body ran straight past the gate"
        assert sup.status().state == SupervisorState.STOPPING
        assert sup.gate_timed_out is False
    finally:
        sup.release()
        sup.stop()


def test_release_lets_the_parked_stop_finish() -> None:
    sup = _supervisor()
    sup.start()
    try:
        sup.request_stop()
        assert sup.stop_entered.wait(_gated_supervisor.GATE_TIMEOUT_S)
        sup.release()
        # The lifecycle thread is the one that must finish, not the caller's
        # own stop() — join it rather than re-entering stop() from here.
        thread = _lifecycle_thread()
        thread.join(timeout=_gated_supervisor.GATE_TIMEOUT_S)
        assert not thread.is_alive()
        assert sup.status().state == SupervisorState.STOPPED
        assert sup.gate_timed_out is False
    finally:
        sup.release()
        sup.stop()


def test_an_unreleased_gate_expires_and_says_so(monkeypatch) -> None:
    """The timeout branch must be reachable and must set the flag.

    Both busy-guard pins assert `not gate_timed_out`, so a flag that could
    never become True would make those assertions decoration — and an expiring
    park lets the lifecycle thread finish, which the guard then correctly
    answers with a 202. Without the flag that reads as a broken guard.
    """
    monkeypatch.setattr(_gated_supervisor, "GATE_TIMEOUT_S", _SHORT_GATE_S)
    sup = _supervisor()
    sup.start()
    try:
        sup.request_stop()
        thread = _lifecycle_thread()
        thread.join(timeout=10.0)
        assert not thread.is_alive(), "the park never expired"
        assert sup.gate_timed_out is True
        assert sup.status().state == SupervisorState.STOPPED
    finally:
        sup.release()
        sup.stop()
