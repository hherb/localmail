# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""DaemonSupervisor — subprocess lifecycle, state machine, ring buffer (2B.4).

Plane B: the serve process owns `localmail run` as a child. These tests drive
the supervisor against dummy Python subprocesses (a long sleeper, a quick
exiter, a chatty printer) — no real daemon, no DB — so the lifecycle and crash
detection are exercised honestly without IMAP/Postgres.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from localmail.serve.daemon_supervisor import (
    DaemonSupervisor,
    ExternalDaemonSupervisor,
    SupervisorState,
    SupervisorUnavailable,
    default_daemon_argv,
    resolve_runtime_dir,
    socket_path,
)

from tests._gated_supervisor import (
    GATE_TIMEOUT_S,
    GATED_GRACE_S,
    GatedStopSupervisor,
)


# --- pure helpers ---------------------------------------------------------

def test_resolve_runtime_dir_prefers_configured() -> None:
    got = resolve_runtime_dir("/run/localmail", env={"XDG_RUNTIME_DIR": "/xdg"})
    assert got == Path("/run/localmail")


def test_resolve_runtime_dir_uses_xdg_when_unconfigured() -> None:
    got = resolve_runtime_dir("", env={"XDG_RUNTIME_DIR": "/xdg/run"})
    assert got == Path("/xdg/run")


def test_resolve_runtime_dir_falls_back_to_tmp(tmp_path: Path) -> None:
    got = resolve_runtime_dir("", env={}, tmp_dir=str(tmp_path))
    assert got == tmp_path


def test_socket_path_filename() -> None:
    assert socket_path(Path("/run/localmail")).name == "localmail-supervisor.sock"
    assert socket_path(Path("/run/localmail")) == Path(
        "/run/localmail/localmail-supervisor.sock"
    )


def test_default_daemon_argv_without_config() -> None:
    argv = default_daemon_argv(config_path=None)
    assert argv == [sys.executable, "-m", "localmail", "run"]


def test_default_daemon_argv_threads_config_path() -> None:
    argv = default_daemon_argv(config_path=Path("/etc/localmail/config.toml"))
    assert argv == [
        sys.executable, "-m", "localmail",
        "--config", "/etc/localmail/config.toml", "run",
    ]


def test_default_daemon_argv_appends_extra() -> None:
    argv = default_daemon_argv(config_path=None, extra=["--no-ssl"])
    assert argv[-2:] == ["run", "--no-ssl"]


# --- lifecycle against dummy subprocesses ---------------------------------

_SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]


@pytest.fixture
def sup() -> DaemonSupervisor:
    s = DaemonSupervisor(argv=_SLEEPER, grace_seconds=2.0)
    yield s
    s.stop()  # always clean up the child


def test_initial_state_is_stopped(sup: DaemonSupervisor) -> None:
    st = sup.status()
    assert st.state == SupervisorState.STOPPED
    assert st.pid is None
    assert st.started_at is None


def test_start_transitions_to_running(sup: DaemonSupervisor) -> None:
    sup.start()
    st = sup.status()
    assert st.state == SupervisorState.RUNNING
    assert st.pid is not None and st.pid > 0
    assert st.started_at is not None


def test_start_is_idempotent_while_running(sup: DaemonSupervisor) -> None:
    sup.start()
    pid1 = sup.status().pid
    sup.start()  # no-op, same child
    assert sup.status().pid == pid1


def test_stop_terminates_and_returns_to_stopped(sup: DaemonSupervisor) -> None:
    sup.start()
    sup.stop()
    st = sup.status()
    assert st.state == SupervisorState.STOPPED
    assert st.pid is None


def test_restart_spawns_a_new_process(sup: DaemonSupervisor) -> None:
    sup.start()
    pid1 = sup.status().pid
    sup.restart()
    st = sup.status()
    assert st.state == SupervisorState.RUNNING
    assert st.pid is not None and st.pid != pid1


def test_crash_is_detected() -> None:
    # A child that exits on its own (we never call stop) → crashed.
    s = DaemonSupervisor(
        argv=[sys.executable, "-c", "print('bye'); "],
        grace_seconds=2.0,
    )
    s.start()
    # Give the child time to exit and the reader thread to observe EOF.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if s.status().state == SupervisorState.CRASHED:
            break
        time.sleep(0.05)
    assert s.status().state == SupervisorState.CRASHED


def test_recent_log_lines_capture_child_output() -> None:
    s = DaemonSupervisor(
        argv=[
            sys.executable, "-u", "-c",
            "print('line-one'); print('line-two'); import time; time.sleep(60)",
        ],
        grace_seconds=2.0,
    )
    try:
        s.start()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if len(s.recent_log_lines()) >= 2:
                break
            time.sleep(0.05)
        lines = s.recent_log_lines()
        assert "line-one" in lines
        assert "line-two" in lines
    finally:
        s.stop()


def test_ring_buffer_is_bounded() -> None:
    s = DaemonSupervisor(
        argv=[
            sys.executable, "-u", "-c",
            "import time\n"
            "for i in range(50): print(i)\n"
            "time.sleep(60)\n",
        ],
        grace_seconds=2.0,
        log_max_lines=10,
    )
    try:
        s.start()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if len(s.recent_log_lines()) >= 10:
                break
            time.sleep(0.05)
        assert len(s.recent_log_lines()) <= 10
    finally:
        s.stop()


# --- external stub --------------------------------------------------------

def test_external_stub_reports_external() -> None:
    s = ExternalDaemonSupervisor()
    st = s.status()
    assert st.state == SupervisorState.EXTERNAL
    assert st.pid is None
    assert s.recent_log_lines() == []


@pytest.mark.parametrize("method", ["start", "stop", "restart"])
def test_external_stub_refuses_lifecycle(method: str) -> None:
    s = ExternalDaemonSupervisor()
    with pytest.raises(SupervisorUnavailable):
        getattr(s, method)()


# --- async lifecycle (request_*) -----------------------------------------

# A child that ignores SIGTERM so stop() blocks the full grace window, giving a
# deterministic interval in which STOPPING is observable. The busy-guard pin used
# to lean on this too and no longer does — it holds its own window open now, see
# `tests/_gated_supervisor.py`.
_DEAF_SLEEPER = [
    sys.executable, "-c",
    "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    "print('up', flush=True); time.sleep(60)",
]


def _wait_state(sup: DaemonSupervisor, target: str, timeout: float = 6.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if sup.status().state == target:
            return
        time.sleep(0.02)
    raise AssertionError(f"never reached {target}; now {sup.status().state}")


def test_request_start_settles_to_running() -> None:
    s = DaemonSupervisor(argv=_SLEEPER, grace_seconds=2.0)
    try:
        s.request_start()
        _wait_state(s, SupervisorState.RUNNING)
        assert s.status().pid is not None
    finally:
        s.stop()


def test_request_stop_sets_transitional_then_stopped() -> None:
    s = DaemonSupervisor(argv=_DEAF_SLEEPER, grace_seconds=1.0)
    try:
        s.request_start()
        _wait_state(s, SupervisorState.RUNNING)
        s.request_stop()
        # Transitional state is visible immediately, before the grace wait ends.
        assert s.status().state == SupervisorState.STOPPING
        _wait_state(s, SupervisorState.STOPPED)
    finally:
        s.stop()


def test_busy_guard_rejects_second_lifecycle_op() -> None:
    """The guard refuses a second request while the first is in flight.

    Gated rather than timed: this had the same wall-clock shape as the
    route-level pin #299 was filed about — the window was the child's grace
    period, so the assertion had to beat a timer. See
    `tests/_gated_supervisor.py`.
    """
    s = GatedStopSupervisor(argv=_SLEEPER, grace_seconds=GATED_GRACE_S)
    # Start synchronously so the only lifecycle thread in play is the stop's.
    s.start()
    try:
        s.request_stop()
        assert s.stop_entered.wait(GATE_TIMEOUT_S), "stop body never ran"
        # Observe first, judge second. `pytest.raises` would abort on the verdict
        # and never reach the flag below, so an expired window would be reported
        # as "DID NOT RAISE" — the misleading message the flag exists to replace.
        state = s.status().state
        refused: SupervisorUnavailable | None = None
        try:
            s.request_stop()
        except SupervisorUnavailable as exc:
            refused = exc
        assert not s.gate_timed_out, "the gate expired; the window was not open"
        assert state == SupervisorState.STOPPING
        assert refused is not None, "the busy-guard admitted a second request"
        # The refused request must not have wedged the accepted one — asserted
        # inside the `try`, because the teardown `stop()` below sets STOPPED from
        # the main thread and would satisfy this poll on its own.
        s.release()
        _wait_state(s, SupervisorState.STOPPED)
    finally:
        s.release()
        s.stop()


def test_request_start_idempotent_when_running() -> None:
    s = DaemonSupervisor(argv=_SLEEPER, grace_seconds=2.0)
    try:
        s.request_start()
        _wait_state(s, SupervisorState.RUNNING)
        pid1 = s.status().pid
        s.request_start()  # no-op, not a busy error
        assert s.status().state == SupervisorState.RUNNING
        assert s.status().pid == pid1
    finally:
        s.stop()


def test_request_restart_settles_to_running_new_pid() -> None:
    s = DaemonSupervisor(argv=_SLEEPER, grace_seconds=2.0)
    try:
        s.request_start()
        _wait_state(s, SupervisorState.RUNNING)
        pid1 = s.status().pid
        s.request_restart()
        _wait_state(s, SupervisorState.RUNNING)
        assert s.status().pid != pid1
    finally:
        s.stop()


@pytest.mark.parametrize("method", ["request_start", "request_stop", "request_restart"])
def test_external_stub_refuses_request_lifecycle(method: str) -> None:
    s = ExternalDaemonSupervisor()
    with pytest.raises(SupervisorUnavailable):
        getattr(s, method)()


# --- close() during an in-flight async restart (#149) ---------------------

class _BarrierRestartSupervisor(DaemonSupervisor):
    """Pauses `restart()` exactly between its stop() and start() halves so a
    test can land close() in that window deterministically. close() uses the
    real (unhooked) stop(), so it is not blocked by the barrier."""

    def __init__(self, *args, between, proceed, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._between = between
        self._proceed = proceed

    def restart(self) -> None:
        self.stop()
        self._between.set()
        assert self._proceed.wait(timeout=5.0)
        self.start()


def test_close_during_async_restart_does_not_respawn() -> None:
    """#149: if close() lands between an async restart's stop() and start(),
    the start() half must NOT re-spawn an orphaned child."""
    import threading

    between = threading.Event()
    proceed = threading.Event()
    s = _BarrierRestartSupervisor(
        argv=_SLEEPER, grace_seconds=2.0, between=between, proceed=proceed
    )
    s.start()
    _wait_state(s, SupervisorState.RUNNING)

    s.request_restart()  # lifecycle thread: stop() … [barrier] … start()
    assert between.wait(timeout=5.0)  # restart's stop() half has completed

    s.close()  # serve shutdown lands in the gap; sets the closing flag
    proceed.set()  # release the restart to attempt its start() half

    lifecycle = s._lifecycle_thread
    assert lifecycle is not None
    lifecycle.join(timeout=5.0)
    assert not lifecycle.is_alive()

    st = s.status()
    assert st.state == SupervisorState.STOPPED
    assert st.pid is None
    assert s._proc is None or s._proc.poll() is not None


def test_start_after_close_is_a_noop() -> None:
    """The closing flag makes start() inert after close(), independent of the
    restart path — no child is spawned once the supervisor is closing."""
    s = DaemonSupervisor(argv=_SLEEPER, grace_seconds=2.0)
    s.close()
    s.start()
    st = s.status()
    assert st.state == SupervisorState.STOPPED
    assert st.pid is None
