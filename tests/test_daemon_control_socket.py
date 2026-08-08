# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

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
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from localmail.serve.daemon_control_socket import (
    SUN_PATH_MAX_BYTES,
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

# macOS caps an AF_UNIX `sun_path` at 104 bytes (Linux's `sizeof` is 108, so
# 107 is its last usable length), and pytest's `tmp_path` — nested under
# `pytest-of-<user>/pytest-<n>/<test-name><n>` — overruns it, so `bind()` fails
# with a bare `OSError: AF_UNIX path too long` naming neither the limit nor the
# culprit. Production reaches the same cap only through an over-long
# `[serve] runtime_dir`, which `ControlSocketServer.start` now rejects by name.


@pytest.fixture
def socket_dir() -> Iterator[Path]:
    """A temp dir short enough to hold an AF_UNIX socket path.

    Rooted at `/tmp` rather than `$TMPDIR`: `tempfile` honours TMPDIR, which CI
    images and agent harnesses routinely set to a deep per-session path, and the
    skip below would then silently delete every test in this section — including
    the only guard on the 0600 bind (#221 E). `pytest -q` prints skip counts,
    not reasons, so nobody would notice.
    """
    root = "/tmp" if os.path.isdir("/tmp") else None
    with tempfile.TemporaryDirectory(prefix="lm-ctl-", dir=root) as raw:
        path = Path(raw)
        # Measured in bytes, as the kernel does: a non-ASCII TMPDIR (an accented
        # username, a CJK home) overruns the cap at a *character* count the
        # guard would still call safe, delivering the exact bare OSError this
        # fixture exists to prevent.
        if len(os.fsencode(path / "ctl.sock")) >= SUN_PATH_MAX_BYTES:
            pytest.skip(f"temp dir too long for an AF_UNIX path: {path}")
        yield path


@pytest.fixture
def server(socket_dir: Path):
    sock = socket_dir / "ctl.sock"
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


def test_socket_file_removed_on_close(socket_dir: Path) -> None:
    sock = socket_dir / "ctl.sock"
    srv = ControlSocketServer(path=sock, supervisor=ExternalDaemonSupervisor())
    srv.start()
    assert sock.exists()
    srv.close()
    assert not sock.exists()


def test_send_to_missing_socket_raises(socket_dir: Path) -> None:
    # Uses the short dir so the raise is provably "no such socket" rather than
    # the path-length OSError, which would pass this assertion for the wrong
    # reason.
    with pytest.raises(ControlSocketError):
        send_control_request(socket_dir / "nope.sock", {"cmd": "status"}, timeout=1.0)


def test_silent_client_does_not_wedge_the_server(socket_dir: Path) -> None:
    """A client that connects but never sends must not freeze the accept loop:
    a concurrent well-formed request still succeeds promptly (per-connection
    threads + bounded recv timeout)."""
    sock = socket_dir / "ctl.sock"
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


def test_stale_socket_file_is_replaced(socket_dir: Path) -> None:
    sock = socket_dir / "ctl.sock"
    sock.write_text("stale")  # a leftover file from a crashed prior run
    srv = ControlSocketServer(path=sock, supervisor=ExternalDaemonSupervisor())
    srv.start()
    try:
        resp = send_control_request(sock, {"cmd": "status"}, timeout=5.0)
        assert resp["ok"] is True
    finally:
        srv.close()


def test_over_long_path_names_the_limit_and_the_setting(socket_dir: Path) -> None:
    """An over-long `[serve] runtime_dir` must not surface as the bare
    `OSError: AF_UNIX path too long`, which names neither the cap nor the key
    that caused it — serve aborts on this at lifespan startup."""
    deep = socket_dir / ("d" * (SUN_PATH_MAX_BYTES + 32))
    srv = ControlSocketServer(
        path=deep / "ctl.sock", supervisor=ExternalDaemonSupervisor()
    )
    with pytest.raises(ControlSocketError) as excinfo:
        srv.start()
    msg = str(excinfo.value)
    assert str(SUN_PATH_MAX_BYTES) in msg
    assert "runtime_dir" in msg


def test_non_ascii_path_is_measured_in_bytes(socket_dir: Path) -> None:
    """The cap is a byte count. A path whose *character* count clears it but
    whose UTF-8 encoding does not must still be rejected by name — this is the
    accented-username / CJK-home case."""
    # 'é' is one character but two UTF-8 bytes. Size the component so the whole
    # path lands just under the cap by len() yet well over it once encoded.
    overhead = len(str(socket_dir / "x" / "ctl.sock")) - 1
    n = SUN_PATH_MAX_BYTES - overhead - 1
    if n < 1:
        pytest.skip(f"no room under the cap to build the case: {socket_dir}")
    sock = socket_dir / ("é" * n) / "ctl.sock"
    assert len(str(sock)) < SUN_PATH_MAX_BYTES <= len(os.fsencode(sock))

    srv = ControlSocketServer(path=sock, supervisor=ExternalDaemonSupervisor())
    with pytest.raises(ControlSocketError):
        srv.start()
