"""DaemonSupervisor — Plane B process lifecycle for `localmail run` (2B.4).

The serve process optionally owns the sync daemon as a child subprocess so the
admin UI / CLI can start, stop, and restart it. This module is the OS-facing
half of daemon control; Plane A (reload / restart-account) is DB-mediated and
lives in `localmail.api.admin.daemon`.

Design:
  * `DaemonSupervisor` spawns one child via `subprocess.Popen`, drains its
    combined stdout/stderr into a bounded ring buffer (a reader thread), and
    runs a small state machine
    `stopped → starting → running → stopping → stopped` with `crashed` as the
    terminal for an unexpected child exit.
  * `ExternalDaemonSupervisor` is the stub used when the operator runs the
    daemon under systemd (`[serve] supervise_daemon = false`): status reports
    `external`; lifecycle ops raise `SupervisorUnavailable`.
  * Pure helpers (`resolve_runtime_dir`, `socket_path`, `default_daemon_argv`)
    have no IO and are shared by the serve wiring and the CLI so both derive
    the same socket path / launch argv.

No magic numbers: the grace period and ring-buffer size are constructor
arguments threaded from `DaemonConfig` / `ServeConfig`.
"""
from __future__ import annotations

import signal
import subprocess
import sys
import tempfile
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

_SOCKET_FILENAME = "localmail-supervisor.sock"
# Default ring-buffer depth for captured child log lines (spec: last 200).
DEFAULT_LOG_MAX_LINES = 200


class SupervisorState:
    """String state constants (plain str so they JSON-serialise as-is)."""

    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    CRASHED = "crashed"
    EXTERNAL = "external"


class SupervisorUnavailable(RuntimeError):
    """Raised when a lifecycle op is attempted on an externally-supervised
    daemon (no child process is owned here)."""


@dataclass(frozen=True)
class SupervisorStatus:
    state: str
    pid: int | None
    started_at: datetime | None


def status_to_dict(status: SupervisorStatus) -> dict:
    """JSON-serialisable view of a SupervisorStatus (shared by the control
    socket protocol and the HTTP route so the wire shape can't drift)."""
    return {
        "state": status.state,
        "pid": status.pid,
        "started_at": (
            status.started_at.isoformat() if status.started_at is not None else None
        ),
    }


# --- pure helpers ---------------------------------------------------------

def resolve_runtime_dir(
    configured: str,
    *,
    env: Mapping[str, str],
    tmp_dir: str | None = None,
) -> Path:
    """Resolve the directory holding the control socket.

    Precedence: an explicit `[serve] runtime_dir` wins; otherwise
    `$XDG_RUNTIME_DIR`; otherwise the platform temp dir. `env` and `tmp_dir`
    are injected so this stays pure and testable.
    """
    if configured:
        return Path(configured)
    xdg = env.get("XDG_RUNTIME_DIR")
    if xdg:
        return Path(xdg)
    return Path(tmp_dir if tmp_dir is not None else tempfile.gettempdir())


def socket_path(runtime_dir: Path) -> Path:
    """Absolute path of the supervisor control socket within `runtime_dir`."""
    return runtime_dir / _SOCKET_FILENAME


def default_daemon_argv(
    *,
    config_path: Path | None,
    extra: Sequence[str] | None = None,
) -> list[str]:
    """Build the argv that launches the sync daemon as a child.

    Uses `python -m localmail run` (portable — no dependence on the
    console-script being on PATH). `config_path`, when set, is threaded so the
    child loads the same config as the supervising serve process. `extra`
    appends `run` options (e.g. `--no-ssl`).
    """
    argv = [sys.executable, "-m", "localmail"]
    if config_path is not None:
        argv += ["--config", str(config_path)]
    argv += ["run"]
    if extra:
        argv += list(extra)
    return argv


# --- the supervisor -------------------------------------------------------

class DaemonSupervisor:
    """Owns one `localmail run` child process.

    Thread-safe: all state transitions take `_lock`. The reader thread only
    grabs the lock briefly to flip `running → crashed` on an unexpected EOF.
    `stop()` deliberately releases the lock before waiting on the child so the
    reader can never deadlock against the grace-period wait.
    """

    def __init__(
        self,
        *,
        argv: Sequence[str],
        grace_seconds: float,
        log_max_lines: int = DEFAULT_LOG_MAX_LINES,
    ) -> None:
        self._argv = list(argv)
        self._grace_seconds = grace_seconds
        self._log_max_lines = log_max_lines
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._lifecycle_thread: threading.Thread | None = None
        # A fresh deque is bound per start() and handed to that run's reader
        # thread, so a crashed child's still-draining reader can never leak
        # stale lines into the next run's buffer.
        self._log: deque[str] = deque(maxlen=log_max_lines)
        self._state = SupervisorState.STOPPED
        self._started_at: datetime | None = None

    # -- public API --

    def start(self) -> None:
        """Spawn the child if not already running. Idempotent: a no-op when a
        live child already exists."""
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return
            self._state = SupervisorState.STARTING
            self._log = deque(maxlen=self._log_max_lines)
            self._proc = subprocess.Popen(
                self._argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            self._started_at = datetime.now(timezone.utc)
            self._reader = threading.Thread(
                target=self._drain_output,
                args=(self._proc, self._log),
                name="daemon-supervisor-reader",
                daemon=True,
            )
            self._reader.start()
            self._state = SupervisorState.RUNNING

    def stop(self) -> None:
        """SIGTERM the child, wait `grace_seconds`, then SIGKILL. No-op when no
        child is running."""
        with self._lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                self._proc = None
                self._started_at = None
                self._state = SupervisorState.STOPPED
                return
            self._state = SupervisorState.STOPPING
        # Wait outside the lock so the reader thread's EOF handler can proceed.
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=self._grace_seconds)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        with self._lock:
            self._proc = None
            self._started_at = None
            self._state = SupervisorState.STOPPED

    def restart(self) -> None:
        self.stop()
        self.start()

    # -- async lifecycle (request_*): return immediately, run on one thread --

    def _lifecycle_in_flight(self) -> bool:
        t = self._lifecycle_thread
        return t is not None and t.is_alive()

    def _spawn_lifecycle(self, body: Callable[[], None]) -> None:
        t = threading.Thread(
            target=body, name="daemon-supervisor-lifecycle", daemon=True
        )
        self._lifecycle_thread = t
        t.start()

    def request_start(self) -> None:
        """Start the child on a background thread; return at once.

        Idempotent no-op if already running. Raises SupervisorUnavailable if a
        lifecycle op is already in flight (the busy-guard)."""
        with self._lock:
            if self._lifecycle_in_flight():
                raise SupervisorUnavailable(
                    "a lifecycle operation is already in progress"
                )
            if self._proc is not None and self._proc.poll() is None:
                return
            self._state = SupervisorState.STARTING
            self._spawn_lifecycle(self.start)

    def request_stop(self) -> None:
        """Stop the child on a background thread; return at once.

        Idempotent no-op if already stopped. Sets STOPPING synchronously so a
        clean shutdown is never misread as a crash. Busy-guarded."""
        with self._lock:
            if self._lifecycle_in_flight():
                raise SupervisorUnavailable(
                    "a lifecycle operation is already in progress"
                )
            if self._proc is None or self._proc.poll() is not None:
                self._proc = None
                self._started_at = None
                self._state = SupervisorState.STOPPED
                return
            self._state = SupervisorState.STOPPING
            self._spawn_lifecycle(self.stop)

    def request_restart(self) -> None:
        """Restart the child on a background thread; return at once. Busy-guarded.

        Settle target is RUNNING; a transient STOPPED mid-restart is expected."""
        with self._lock:
            if self._lifecycle_in_flight():
                raise SupervisorUnavailable(
                    "a lifecycle operation is already in progress"
                )
            self._state = SupervisorState.STOPPING
            self._spawn_lifecycle(self.restart)

    def status(self) -> SupervisorStatus:
        """Current state, refreshing crash detection defensively (in case the
        reader thread hasn't yet observed the EOF)."""
        with self._lock:
            proc = self._proc
            if (
                self._state == SupervisorState.RUNNING
                and proc is not None
                and proc.poll() is not None
            ):
                self._state = SupervisorState.CRASHED
            pid = proc.pid if (proc is not None and self._state != SupervisorState.STOPPED) else None
            return SupervisorStatus(
                state=self._state, pid=pid, started_at=self._started_at
            )

    def recent_log_lines(self) -> list[str]:
        with self._lock:
            return list(self._log)

    def close(self) -> None:
        """Stop the child on serve shutdown — the supervisor owns it, so an
        orphan would outlive its parent."""
        self.stop()

    # -- internals --

    def _drain_output(self, proc: subprocess.Popen[str], log: deque[str]) -> None:
        # Appends to the run-specific `log` deque (bound in start()), not
        # self._log, so a late-draining reader never contaminates a newer run.
        stream = proc.stdout
        if stream is not None:
            for line in stream:
                log.append(line.rstrip("\n"))
        # EOF: the child closed stdout. If we did not initiate the stop, this
        # is an unexpected exit → crashed.
        with self._lock:
            if self._state == SupervisorState.RUNNING and self._proc is proc:
                self._state = SupervisorState.CRASHED


class ExternalDaemonSupervisor:
    """Stub for `supervise_daemon = false`: no child is owned here.

    Status reports `external`; lifecycle ops raise so the HTTP/CLI layers can
    translate to a clear "managed externally" message. Read-only daemon status
    still comes from the DB heartbeats plane independently of this object.
    """

    def start(self) -> None:
        raise SupervisorUnavailable("daemon is supervised externally")

    def stop(self) -> None:
        raise SupervisorUnavailable("daemon is supervised externally")

    def restart(self) -> None:
        raise SupervisorUnavailable("daemon is supervised externally")

    def request_start(self) -> None:
        raise SupervisorUnavailable("daemon is supervised externally")

    def request_stop(self) -> None:
        raise SupervisorUnavailable("daemon is supervised externally")

    def request_restart(self) -> None:
        raise SupervisorUnavailable("daemon is supervised externally")

    def status(self) -> SupervisorStatus:
        return SupervisorStatus(
            state=SupervisorState.EXTERNAL, pid=None, started_at=None
        )

    def recent_log_lines(self) -> list[str]:
        return []


DaemonSupervisorT = DaemonSupervisor | ExternalDaemonSupervisor
