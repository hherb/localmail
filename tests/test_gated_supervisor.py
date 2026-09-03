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

#: How long the park is watched before concluding it is holding. Spent in full
#: on the *passing* path — the thread is parked, so the join times out, and that
#: timeout IS the assertion. A broken gate returns from the join at once. So the
#: bound is one-sided: it can only delay a pass, never turn one into a failure.
_PARK_PROOF_S = 0.5

#: How long an *expiring* park is given to finish: the shortened gate plus a real
#: SIGTERM and reap. Named rather than written inline as `10.0`, which is also
#: `GATE_TIMEOUT_S`'s value — a reader would infer a relationship that does not
#: exist, and if the monkeypatch ever stopped landing the two deadlines would
#: race and the failure would name the wrong cause.
_EXPIRY_SETTLE_S = 10.0


def _supervisor() -> GatedStopSupervisor:
    return GatedStopSupervisor(argv=_SLEEPER, grace_seconds=GATED_GRACE_S)


def _lifecycle_thread(sup: GatedStopSupervisor) -> threading.Thread:
    """The supervisor's own lifecycle thread, read off the instance.

    Never `threading.enumerate()`. A process-wide scan for the production thread
    name would also see one left behind by another test — those threads are
    daemons and no `stop()` joins them — so asserting "exactly one" would make
    this file's determinism rest on a cross-test timing margin, which is the very
    thing it exists to remove. `_spawn_lifecycle` assigns the attribute under
    `_lock` before `Thread.start()`, so it is set the moment `request_stop`
    returns; the same idiom is already used in `test_daemon_supervisor.py`.
    """
    thread = sup._lifecycle_thread
    assert thread is not None, "request_stop() spawned no lifecycle thread"
    return thread


def test_a_gated_stop_parks_instead_of_finishing() -> None:
    """While the gate is shut the lifecycle body has not reached the real stop.

    Asserting only that the state is still STOPPING does NOT pin this — it was
    written that way first, and removing the park left it green, because the
    real stop takes milliseconds (SIGTERM plus a reap) against a microsecond
    assertion path. That is the same lucky-win the gate exists to remove, so
    the pin joins the thread instead.

    The join here is one-sided: it can only delay a pass. The passing path is
    bounded by the 10 s `GATE_TIMEOUT_S` backstop rather than by a window the
    assertion must beat — not "cannot flake under any load", which would be
    false, but "the only stall that breaks it also reports itself".
    """
    sup = _supervisor()
    sup.start()
    try:
        sup.request_stop()
        assert sup.stop_entered.wait(_gated_supervisor.GATE_TIMEOUT_S)
        thread = _lifecycle_thread(sup)
        thread.join(_PARK_PROOF_S)
        # Observe, then read the flag, then judge: an expired park makes the
        # thread finish, and `still_parked` would then blame the gate for a
        # window that simply closed.
        still_parked = thread.is_alive()
        assert sup.gate_timed_out is False, "the gate expired; window not open"
        assert still_parked, "the lifecycle body ran straight past the gate"
        assert sup.status().state == SupervisorState.STOPPING
    finally:
        sup.release()
        sup.stop()


def test_release_lets_the_parked_stop_finish() -> None:
    sup = _supervisor()
    sup.start()
    try:
        sup.request_stop()
        assert sup.stop_entered.wait(_gated_supervisor.GATE_TIMEOUT_S)
        # Resolve the thread before releasing: after `release()` it may already
        # have finished, and the lookup would have nothing to return.
        # The lifecycle thread is the one that must finish, not the caller's
        # own stop() — join it rather than re-entering stop() from here.
        thread = _lifecycle_thread(sup)
        sup.release()
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
        thread = _lifecycle_thread(sup)
        thread.join(timeout=_EXPIRY_SETTLE_S)
        assert not thread.is_alive(), "the park never expired"
        assert sup.gate_timed_out is True
        assert sup.status().state == SupervisorState.STOPPED
    finally:
        sup.release()
        sup.stop()
