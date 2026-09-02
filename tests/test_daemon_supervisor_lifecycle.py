# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""DaemonSupervisor lifecycle robustness — #221 A (supervisor half) and C.

A: the supervisor's SIGKILL deadline must outlast the child's own shutdown
   budget, or every ordinary stop/restart kills a healthy child.
C: a `request_*` issued after `close()` must not leave the state machine stuck
   at `starting` forever.
"""
from __future__ import annotations

import sys
import time

import pytest

# Module scope, not function scope: the autouse pool-closing fixture reads
# sys.modules at test-setup time (#321, tests/_pool_leaks.py).
from localmail.serve import app as app_mod
from localmail.serve.daemon_supervisor import (
    DaemonSupervisor,
    SupervisorState,
    SupervisorUnavailable,
)
from localmail.shutdown_budget import supervisor_kill_after

_SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]


# --- A: the supervisor outwaits the child ---------------------------------

def test_serve_gives_the_supervisor_more_than_the_childs_own_grace() -> None:
    """The wiring claim: `create_app` must not hand the supervisor the child's
    raw `shutdown_grace_seconds`.

    Asserted against the source of the wiring rather than a live app because
    building an app needs a DB; the value it passes is what matters.
    """
    src = (app_mod.__file__ or "")
    text = open(src, encoding="utf-8").read()
    assert "supervisor_kill_after(" in text, (
        "create_app must derive the supervisor's kill deadline via "
        "supervisor_kill_after(), not pass shutdown_grace_seconds directly"
    )


def test_supervisor_grace_is_whatever_it_was_constructed_with() -> None:
    """The supervisor itself stays dumb — it waits the number it is given.
    Relating that number to the child's budget is the caller's job, which is
    why `supervisor_kill_after` is a shared pure helper rather than logic
    buried in `stop()`."""
    sup = DaemonSupervisor(argv=_SLEEPER, grace_seconds=supervisor_kill_after(2.0))
    try:
        assert sup._grace_seconds == 2.0 + 5.0
    finally:
        sup.stop()


def test_a_child_that_exits_within_the_margin_is_not_killed() -> None:
    """End-to-end: a child that takes slightly longer than the nominal grace to
    die still exits on its own SIGTERM rather than being SIGKILLed.

    Exit code 0 proves the child ran its own handler to completion; a SIGKILL
    shows up as -9.
    """
    # Handles SIGTERM, then takes 0.6s to "wind down" before exiting cleanly.
    slow_but_clean = [
        sys.executable, "-c",
        "import signal,sys,time\n"
        "def h(*a):\n"
        "    time.sleep(0.6)\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, h)\n"
        "time.sleep(60)\n",
    ]
    # A child budget of 0.1s: too short on its own, but the margin covers it.
    sup = DaemonSupervisor(
        argv=slow_but_clean, grace_seconds=supervisor_kill_after(0.1)
    )
    sup.start()
    time.sleep(0.3)  # let the handler install
    proc = sup._proc
    assert proc is not None
    sup.stop()
    assert proc.returncode == 0, (
        f"child was killed rather than allowed to exit (rc={proc.returncode})"
    )


# --- C: request_* after close() ------------------------------------------

def test_request_start_after_close_raises_instead_of_sticking_at_starting() -> None:
    """The #221 C bug: `request_start` set STARTING synchronously, then the
    background `start()` saw `_closing` and returned without resetting it — so
    `status()` reported `starting` forever and the admin panel showed a daemon
    that was never coming."""
    sup = DaemonSupervisor(argv=_SLEEPER, grace_seconds=1.0)
    sup.close()

    with pytest.raises(SupervisorUnavailable):
        sup.request_start()

    assert sup.status().state == SupervisorState.STOPPED


def test_request_restart_after_close_raises_and_leaves_state_stopped() -> None:
    sup = DaemonSupervisor(argv=_SLEEPER, grace_seconds=1.0)
    sup.close()

    with pytest.raises(SupervisorUnavailable):
        sup.request_restart()

    assert sup.status().state == SupervisorState.STOPPED


def test_request_stop_after_close_raises() -> None:
    """Stop is already the direction of travel, but reporting STOPPING for an
    op that will never run is the same lie as C; refuse uniformly."""
    sup = DaemonSupervisor(argv=_SLEEPER, grace_seconds=1.0)
    sup.close()

    with pytest.raises(SupervisorUnavailable):
        sup.request_stop()

    assert sup.status().state == SupervisorState.STOPPED


def test_blocking_start_after_close_is_still_a_silent_no_op() -> None:
    """`close()` itself calls the blocking `stop()`, and #149's guard lives in
    `start()`. That guard must stay a no-op (not a raise) — an async restart
    already in flight calls `start()` directly and must not blow up a
    background lifecycle thread during teardown."""
    sup = DaemonSupervisor(argv=_SLEEPER, grace_seconds=1.0)
    sup.close()

    sup.start()  # must not raise

    assert sup._proc is None
    assert sup.status().state == SupervisorState.STOPPED
