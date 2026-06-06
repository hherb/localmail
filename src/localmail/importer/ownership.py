"""Pure helpers for import-job ownership / orphan detection (#162).

Each import_jobs row records the host + pid of the process that created and runs
it. At serve startup, reconcile reaps an active row only when its owner is gone.
`should_reap` is the pure decision (no syscalls); `pid_is_alive` is the single
process-liveness syscall, isolated so `should_reap` stays unit-testable without
real pids.
"""
from __future__ import annotations

import os


def pid_is_alive(pid: int) -> bool:
    """True iff a process with `pid` currently exists.

    `os.kill(pid, 0)` sends no signal: it returns normally when the process is
    alive, raises ProcessLookupError when it is dead, and raises PermissionError
    when the process exists but is owned by another user (still alive).
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def should_reap(
    *,
    owner_host: str | None,
    owner_pid: int | None,
    current_host: str,
    pid_alive: bool,
) -> bool:
    """Decide whether an active import_jobs row should be reaped as orphaned.

    Reap iff the owning process is gone:
      * ``owner_pid is None``          -> reap (legacy pre-0027 row, or a row
        that crashed between INSERT and commit -- unverifiable, treat as
        orphaned);
      * ``owner_host != current_host`` -> keep (another host's job; single-host
        is the project model, so we never reap what we cannot verify);
      * otherwise                      -> reap iff ``not pid_alive``.

    `pid_alive` is supplied by the caller (via `pid_is_alive`) so this predicate
    stays pure.
    """
    if owner_pid is None:
        return True
    if owner_host != current_host:
        return False
    return not pid_alive
