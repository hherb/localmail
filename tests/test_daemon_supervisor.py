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

# A child that ignores SIGTERM so stop() blocks the full grace window,
# giving a deterministic interval to observe STOPPING / hit the busy-guard.
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
    s = DaemonSupervisor(argv=_DEAF_SLEEPER, grace_seconds=1.0)
    try:
        s.request_start()
        _wait_state(s, SupervisorState.RUNNING)
        s.request_stop()  # now in flight, blocking on the 1s grace wait
        assert s.status().state == SupervisorState.STOPPING
        with pytest.raises(SupervisorUnavailable):
            s.request_stop()
        _wait_state(s, SupervisorState.STOPPED)
    finally:
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
