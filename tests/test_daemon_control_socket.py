"""Unix control socket for the daemon supervisor (2B.4).

Newline-delimited JSON: the CLI connects to `${runtime_dir}/
localmail-supervisor.sock`, sends one request line, reads one response line.
`handle_control_request` is a pure dispatcher (tested directly); the
`ControlSocketServer` + `send_control_request` pair is exercised end-to-end
over a real Unix socket in a tmp dir.
"""
from __future__ import annotations

import os
import socket
import stat
import sys
import time
from pathlib import Path

import pytest

from localmail.serve.daemon_control_socket import (
    ControlSocketError,
    ControlSocketServer,
    handle_control_request,
    send_control_request,
)
from localmail.serve.daemon_supervisor import (
    DaemonSupervisor,
    ExternalDaemonSupervisor,
    SupervisorState,
)


_SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]


# --- pure dispatch --------------------------------------------------------

def test_dispatch_status_external() -> None:
    resp = handle_control_request(ExternalDaemonSupervisor(), {"cmd": "status"})
    assert resp["ok"] is True
    assert resp["status"]["state"] == SupervisorState.EXTERNAL
    assert resp["status"]["pid"] is None


def test_dispatch_unknown_command() -> None:
    resp = handle_control_request(ExternalDaemonSupervisor(), {"cmd": "frobnicate"})
    assert resp["ok"] is False
    assert "frobnicate" in resp["error"]


def test_dispatch_missing_cmd_key() -> None:
    resp = handle_control_request(ExternalDaemonSupervisor(), {})
    assert resp["ok"] is False


def test_dispatch_lifecycle_on_external_reports_error() -> None:
    resp = handle_control_request(ExternalDaemonSupervisor(), {"cmd": "start"})
    assert resp["ok"] is False
    assert "external" in resp["error"].lower()


def test_dispatch_recent_log() -> None:
    resp = handle_control_request(ExternalDaemonSupervisor(), {"cmd": "recent-log"})
    assert resp["ok"] is True
    assert resp["lines"] == []


def _wait_state(sup, target, timeout=6.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if sup.status().state == target:
            return
        time.sleep(0.02)
    raise AssertionError(f"never reached {target}; now {sup.status().state}")


def test_dispatch_start_stop_real_child() -> None:
    sup = DaemonSupervisor(argv=_SLEEPER, grace_seconds=2.0)
    try:
        r1 = handle_control_request(sup, {"cmd": "start"})
        assert r1["ok"] is True
        assert r1["status"]["state"] in (
            SupervisorState.STARTING, SupervisorState.RUNNING
        )
        _wait_state(sup, SupervisorState.RUNNING)
        r2 = handle_control_request(sup, {"cmd": "status"})
        assert r2["status"]["state"] == SupervisorState.RUNNING
    finally:
        handle_control_request(sup, {"cmd": "stop"})
        _wait_state(sup, SupervisorState.STOPPED)
    assert sup.status().state == SupervisorState.STOPPED


_DEAF_SLEEPER = [
    sys.executable, "-c",
    "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    "print('up', flush=True); time.sleep(60)",
]


def test_dispatch_stop_returns_before_grace_elapses() -> None:
    sup = DaemonSupervisor(argv=_DEAF_SLEEPER, grace_seconds=3.0)
    try:
        handle_control_request(sup, {"cmd": "start"})
        _wait_state(sup, SupervisorState.RUNNING)
        # Wait for the child's SIGTERM handler to be installed (avoids macOS race
        # where SIGTERM arrives before the handler is registered, causing an
        # immediate exit that completes stop() instantly, skipping STOPPING).
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if any(line == "up" for line in sup.recent_log_lines()):
                break
            time.sleep(0.02)
        started = time.monotonic()
        resp = handle_control_request(sup, {"cmd": "stop"})
        # Returned promptly (async), not after the 3s grace wait.
        assert time.monotonic() - started < 1.0
        assert resp["status"]["state"] == SupervisorState.STOPPING
        _wait_state(sup, SupervisorState.STOPPED)
    finally:
        sup.stop()


# --- end-to-end socket round trip -----------------------------------------

@pytest.fixture
def server(tmp_path: Path):
    sock = tmp_path / "ctl.sock"
    srv = ControlSocketServer(path=sock, supervisor=ExternalDaemonSupervisor())
    srv.start()
    yield srv, sock
    srv.close()


def test_socket_roundtrip_status(server) -> None:
    _srv, sock = server
    resp = send_control_request(sock, {"cmd": "status"}, timeout=5.0)
    assert resp["ok"] is True
    assert resp["status"]["state"] == SupervisorState.EXTERNAL


def test_socket_roundtrip_lifecycle_error(server) -> None:
    _srv, sock = server
    resp = send_control_request(sock, {"cmd": "start"}, timeout=5.0)
    assert resp["ok"] is False


def test_socket_file_is_mode_0600(server) -> None:
    _srv, sock = server
    mode = stat.S_IMODE(os.stat(sock).st_mode)
    assert mode == 0o600


def test_socket_file_removed_on_close(tmp_path: Path) -> None:
    sock = tmp_path / "ctl.sock"
    srv = ControlSocketServer(path=sock, supervisor=ExternalDaemonSupervisor())
    srv.start()
    assert sock.exists()
    srv.close()
    assert not sock.exists()


def test_send_to_missing_socket_raises(tmp_path: Path) -> None:
    with pytest.raises(ControlSocketError):
        send_control_request(tmp_path / "nope.sock", {"cmd": "status"}, timeout=1.0)


def test_silent_client_does_not_wedge_the_server(tmp_path: Path) -> None:
    """A client that connects but never sends must not freeze the accept loop:
    a concurrent well-formed request still succeeds promptly (per-connection
    threads + bounded recv timeout)."""
    sock = tmp_path / "ctl.sock"
    srv = ControlSocketServer(
        path=sock, supervisor=ExternalDaemonSupervisor(), conn_timeout=0.5
    )
    srv.start()
    silent = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    silent.connect(str(sock))  # connect, then send nothing
    try:
        started = time.monotonic()
        resp = send_control_request(sock, {"cmd": "status"}, timeout=5.0)
        assert resp["ok"] is True
        assert time.monotonic() - started < 2.0  # not blocked behind the silent peer
    finally:
        silent.close()
        srv.close()


def test_stale_socket_file_is_replaced(tmp_path: Path) -> None:
    sock = tmp_path / "ctl.sock"
    sock.write_text("stale")  # a leftover file from a crashed prior run
    srv = ControlSocketServer(path=sock, supervisor=ExternalDaemonSupervisor())
    srv.start()
    try:
        resp = send_control_request(sock, {"cmd": "status"}, timeout=5.0)
        assert resp["ok"] is True
    finally:
        srv.close()
