# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Unix-domain control socket for the daemon supervisor (2B.4).

The serve process owns the supervisor; the `localmail daemon` CLI runs in a
*separate* process and reaches the running supervisor over a Unix socket at
`${runtime_dir}/localmail-supervisor.sock` (mode 0600). The protocol is
newline-delimited JSON: one request object per connection, one response object
back, then close.

`handle_control_request` is a pure dispatcher (supervisor in, dict out) so it
unit-tests without any socket. `ControlSocketServer` wraps it with the accept
loop; `send_control_request` is the client half used by the CLI.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import sys
import threading
from pathlib import Path
from typing import Protocol

from localmail.serve.daemon_supervisor import (
    SupervisorStatus,
    SupervisorUnavailable,
    status_to_dict,
)

logger = logging.getLogger("localmail.serve")

# How long the accept loop blocks before re-checking the stop flag. Bounds the
# socket server's shutdown latency without busy-spinning.
DEFAULT_ACCEPT_TIMEOUT_S = 0.5
# Per-connection recv/send timeout. Bounds a stuck/slow client so it can't wedge
# its handler thread forever; the lifecycle op itself is not socket-bound, so a
# slow stop() (up to shutdown_grace_seconds) is unaffected by this.
DEFAULT_CONN_TIMEOUT_S = 10.0
# Cap on a single request/response line (defensive against an unbounded read
# from a misbehaving peer). Control messages are tiny.
_MAX_LINE_BYTES = 1 << 20
# AF_UNIX bounds `sun_path` in *bytes* — 104 on darwin, 108 on Linux — with one
# reserved for the NUL terminator, so the last usable length is one below this.
# Past it `bind()` raises a bare `OSError: AF_UNIX path too long` naming neither
# the limit nor the setting responsible; `[serve] runtime_dir` is returned
# verbatim by `resolve_runtime_dir` and carries no length validation, so an
# operator can reach this with a config edit alone.
SUN_PATH_MAX_BYTES = 104 if sys.platform == "darwin" else 108


class ControlSocketError(RuntimeError):
    """Client-side failure talking to the control socket (not running,
    refused, malformed reply)."""


class _Supervisor(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def restart(self) -> None: ...
    def request_start(self) -> None: ...
    def request_stop(self) -> None: ...
    def request_restart(self) -> None: ...
    def status(self) -> SupervisorStatus: ...
    def recent_log_lines(self) -> list[str]: ...


def handle_control_request(supervisor: _Supervisor, request: dict) -> dict:
    """Dispatch one control request against the supervisor. Pure w.r.t. IO
    (only touches the supervisor). Never raises — lifecycle failures and
    unknown commands come back as ``{"ok": False, "error": ...}``."""
    cmd = request.get("cmd")
    if cmd == "status":
        return {"ok": True, "status": status_to_dict(supervisor.status())}
    if cmd == "recent-log":
        return {"ok": True, "lines": supervisor.recent_log_lines()}
    if cmd in ("start", "stop", "restart"):
        try:
            getattr(supervisor, f"request_{cmd}")()
        except SupervisorUnavailable as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "status": status_to_dict(supervisor.status())}
    return {"ok": False, "error": f"unknown command: {cmd!r}"}


def _read_line(conn: socket.socket) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = conn.recv(4096)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if b"\n" in chunk or total > _MAX_LINE_BYTES:
            break
    return b"".join(chunks)


class ControlSocketServer:
    """Bind a Unix socket and serve one control request per connection."""

    def __init__(
        self,
        *,
        path: Path,
        supervisor: _Supervisor,
        accept_timeout: float = DEFAULT_ACCEPT_TIMEOUT_S,
        conn_timeout: float = DEFAULT_CONN_TIMEOUT_S,
    ) -> None:
        self._path = path
        self._supervisor = supervisor
        self._accept_timeout = accept_timeout
        self._conn_timeout = conn_timeout
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        encoded_len = len(os.fsencode(self._path))
        if encoded_len >= SUN_PATH_MAX_BYTES:
            raise ControlSocketError(
                f"control socket path is {encoded_len} bytes, over this "
                f"platform's {SUN_PATH_MAX_BYTES}-byte AF_UNIX limit: "
                f"{self._path} — set a shorter [serve] runtime_dir"
            )
        # Replace a stale socket file left by a crashed prior run.
        if self._path.exists():
            self._path.unlink()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        # Bind under a private umask so the socket is 0600 from the instant it
        # exists (#221 E). chmod-after-bind left a window — however brief — in
        # which the control socket sat at whatever the process umask allowed,
        # and anything that connects through it can stop the sync daemon. The
        # umask is process-global, so it is restored in `finally`; the chmod is
        # kept as the belt to this braces (a umask cannot *widen*, so it alone
        # does not guarantee 0600 if some parent set something exotic).
        old_umask = os.umask(0o177)
        try:
            sock.bind(str(self._path))
            os.chmod(self._path, 0o600)
        except BaseException:
            sock.close()
            raise
        finally:
            os.umask(old_umask)
        sock.listen(8)
        sock.settimeout(self._accept_timeout)
        self._sock = sock
        self._thread = threading.Thread(
            target=self._serve_loop, name="daemon-control-socket", daemon=True
        )
        self._thread.start()

    def _serve_loop(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break  # socket closed during shutdown
            # Handle each connection on its own daemon thread: a lifecycle op
            # (stop/restart) can take up to shutdown_grace_seconds, and a slow
            # client shouldn't be able to wedge the accept loop for that long.
            threading.Thread(
                target=self._handle_conn_safe, args=(conn,), daemon=True
            ).start()

    def _handle_conn_safe(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(self._conn_timeout)
            try:
                self._handle_conn(conn)
            except Exception:  # noqa: BLE001 — one bad client must not kill the server
                logger.exception("control socket: request handling failed")

    def _handle_conn(self, conn: socket.socket) -> None:
        raw = _read_line(conn)
        try:
            request = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            response = {"ok": False, "error": "malformed request"}
        else:
            response = handle_control_request(self._supervisor, request)
        conn.sendall((json.dumps(response) + "\n").encode("utf-8"))

    def close(self) -> None:
        """Stop accepting and remove the socket file.

        Joins only the accept thread; in-flight per-connection handler threads
        are daemon threads and are intentionally not joined — a handler mid
        lifecycle op (a stop() up to shutdown_grace_seconds) is allowed to run
        to completion in the background. That converges safely with the
        lifespan's own supervisor.close(): both call the idempotent, lock-guarded
        stop(), so a concurrent teardown can't double-act on the child.
        """
        self._stop.set()
        if self._sock is not None:
            self._sock.close()
        if self._thread is not None:
            self._thread.join(timeout=self._accept_timeout * 4)
        if self._path.exists():
            try:
                self._path.unlink()
            except OSError:
                pass


def send_control_request(path: Path, request: dict, *, timeout: float) -> dict:
    """Client half: connect to `path`, send one JSON request line, return the
    decoded JSON response.

    Every failure mode raises ControlSocketError, which is the only exception
    `daemon_cli.py` catches. The *whole exchange* is wrapped, not just the
    connect (#221 D): a peer that accepts and then stalls — a serve process
    inside a lifecycle op, a wedged handler thread — used to surface as a bare
    `socket.timeout`, and one that hangs up mid-write as a `BrokenPipeError`.
    Both are `OSError`, both escaped the CLI's handler, and both showed the
    operator a traceback where a one-line "supervisor is not reachable" was
    the whole story.
    """
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        try:
            sock.connect(str(path))
        except OSError as e:
            raise ControlSocketError(
                f"cannot reach supervisor control socket at {path}: {e}"
            ) from e
        try:
            sock.sendall((json.dumps(request) + "\n").encode("utf-8"))
            raw = _read_line(sock)
        except OSError as e:
            raise ControlSocketError(
                f"lost contact with the supervisor at {path} "
                f"(it accepted the connection but did not answer): {e}"
            ) from e
    finally:
        sock.close()
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise ControlSocketError("malformed response from supervisor") from e
