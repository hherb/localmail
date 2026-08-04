# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Control-socket client robustness and socket-file permissions (#221 D, E).

D: `send_control_request` wrapped only `connect()`. A peer that accepted the
   connection and then stalled — a serve process mid-lifecycle-op, a wedged
   handler thread — made `sendall`/`recv` raise a bare `socket.timeout`
   (an `OSError`), which `daemon_cli.py`'s `except ControlSocketError` does not
   catch: the operator got a traceback instead of a clean message.

E: `bind()` ran before `os.chmod(path, 0o600)`, so the socket briefly existed at
   whatever the process umask allowed. Local-only and narrow, but closeable for
   free by pre-setting the umask so the socket is never wider than 0600.

These use a real `AF_UNIX` socket in `tmp_path`. On macOS the 104-byte sun_path
limit makes long pytest tmp paths overflow, which is why the module-level
`_short_socket_dir` fixture puts them under the system temp dir instead.
"""
from __future__ import annotations

import json
import os
import socket
import stat
import tempfile
import threading
from pathlib import Path

import pytest

from localmail.serve.daemon_control_socket import (
    ControlSocketError,
    ControlSocketServer,
    send_control_request,
)
from localmail.serve.daemon_supervisor import SupervisorState, SupervisorStatus


@pytest.fixture
def short_socket_dir():
    """A socket directory short enough for AF_UNIX's sun_path limit (104 on
    macOS). pytest's own tmp_path routinely exceeds it."""
    with tempfile.TemporaryDirectory(prefix="lm-") as d:
        yield Path(d)


class _StubSupervisor:
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def restart(self) -> None: ...
    def request_start(self) -> None: ...
    def request_stop(self) -> None: ...
    def request_restart(self) -> None: ...

    def status(self) -> SupervisorStatus:
        return SupervisorStatus(
            state=SupervisorState.STOPPED, pid=None, started_at=None
        )

    def recent_log_lines(self) -> list[str]:
        return []


# --- D: send/recv timeouts ------------------------------------------------

def test_a_stalled_peer_raises_ControlSocketError_not_socket_timeout(
    short_socket_dir: Path,
) -> None:
    """A server that accepts and then never replies must surface as the CLI's
    own error type. Pre-fix this escaped as `socket.timeout`."""
    path = short_socket_dir / "stall.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)
    accepted: list[socket.socket] = []

    def _accept_and_stall() -> None:
        conn, _ = listener.accept()
        accepted.append(conn)  # hold it open, never reply

    t = threading.Thread(target=_accept_and_stall, daemon=True)
    t.start()

    try:
        with pytest.raises(ControlSocketError) as excinfo:
            send_control_request(path, {"cmd": "status"}, timeout=0.3)
        assert "supervisor" in str(excinfo.value).lower()
    finally:
        for c in accepted:
            c.close()
        listener.close()


def test_a_peer_that_hangs_up_mid_exchange_raises_ControlSocketError(
    short_socket_dir: Path,
) -> None:
    """Accept then immediately close: the client's recv returns empty, so the
    JSON decode fails. That path was already wrapped; pin it so the D fix does
    not accidentally narrow it."""
    path = short_socket_dir / "hangup.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)

    def _accept_and_close() -> None:
        conn, _ = listener.accept()
        conn.close()

    threading.Thread(target=_accept_and_close, daemon=True).start()

    try:
        with pytest.raises(ControlSocketError):
            send_control_request(path, {"cmd": "status"}, timeout=1.0)
    finally:
        listener.close()


def test_a_missing_socket_still_raises_ControlSocketError(
    short_socket_dir: Path,
) -> None:
    """The pre-existing connect() guard must survive the refactor."""
    with pytest.raises(ControlSocketError):
        send_control_request(
            short_socket_dir / "absent.sock", {"cmd": "status"}, timeout=0.5
        )


def test_the_happy_path_still_returns_the_decoded_response(
    short_socket_dir: Path,
) -> None:
    """Wrapping the exchange must not change what a working request returns."""
    path = short_socket_dir / "ok.sock"
    server = ControlSocketServer(path=path, supervisor=_StubSupervisor())
    server.start()
    try:
        got = send_control_request(path, {"cmd": "status"}, timeout=5.0)
        assert got["ok"] is True
        assert got["status"]["state"] == SupervisorState.STOPPED
    finally:
        server.close()


# --- E: the socket is never wider than 0600 -------------------------------

def test_the_socket_file_is_private(short_socket_dir: Path) -> None:
    path = short_socket_dir / "perm.sock"
    server = ControlSocketServer(path=path, supervisor=_StubSupervisor())
    server.start()
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600, f"socket mode is {mode:o}, expected 600"
    finally:
        server.close()


def test_the_socket_is_never_created_wider_than_0600(
    short_socket_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The TOCTOU claim (#221 E): the mode must be right *at bind time*, not
    repaired afterwards.

    Observed by making `os.chmod` a no-op — the repair step is removed, so a
    permissive socket can only be explained by bind having created it wide. A
    deliberately permissive umask (0o000) makes the pre-fix bind produce 0777.
    """
    path = short_socket_dir / "toctou.sock"
    monkeypatch.setattr(os, "chmod", lambda *a, **k: None)
    old_umask = os.umask(0o000)
    try:
        server = ControlSocketServer(path=path, supervisor=_StubSupervisor())
        server.start()
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
            assert not (mode & 0o077), (
                f"socket was created group/other-accessible ({mode:o}) — "
                "bind() must run under a private umask, not be chmod-repaired"
            )
        finally:
            server.close()
    finally:
        os.umask(old_umask)


def test_the_process_umask_is_restored_after_start(
    short_socket_dir: Path,
) -> None:
    """The umask is process-global, so borrowing it must be strictly scoped —
    leaking a 0o177 umask would silently make every later file the serve
    process writes owner-only."""
    path = short_socket_dir / "umask.sock"
    before = os.umask(0o022)
    os.umask(before)
    server = ControlSocketServer(path=path, supervisor=_StubSupervisor())
    server.start()
    try:
        after = os.umask(0o022)
        os.umask(after)
        assert after == before
    finally:
        server.close()


def test_the_umask_is_restored_even_when_bind_fails(
    short_socket_dir: Path,
) -> None:
    """A bind failure (stale socket held by a live process, bad path) must not
    leave the borrowed umask installed."""
    before = os.umask(0o022)
    os.umask(before)
    # A path whose parent is a *file*, so mkdir/bind cannot succeed.
    blocker = short_socket_dir / "blocker"
    blocker.write_text("x")
    server = ControlSocketServer(
        path=blocker / "nested.sock", supervisor=_StubSupervisor()
    )
    with pytest.raises(OSError):
        server.start()
    after = os.umask(0o022)
    os.umask(after)
    assert after == before


def test_handle_control_request_still_dispatches(short_socket_dir: Path) -> None:
    """Sanity: the protocol itself is untouched by D/E."""
    path = short_socket_dir / "dispatch.sock"
    server = ControlSocketServer(path=path, supervisor=_StubSupervisor())
    server.start()
    try:
        got = send_control_request(path, {"cmd": "recent-log"}, timeout=5.0)
        assert got == {"ok": True, "lines": []}
        raw = send_control_request(path, {"cmd": "nope"}, timeout=5.0)
        assert raw["ok"] is False
        assert "unknown command" in raw["error"]
        assert json.dumps(raw)  # still JSON-serialisable
    finally:
        server.close()
