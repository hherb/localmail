# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Attachment-blob temp files: the naming convention and its collector (#237).

`attachments.write_attachments` stages every blob payload at a **private** temp
name before atomically `replace`-ing it onto the canonical content-addressed
path. The private name is what makes concurrent writers of the same blob safe
(#231) — but it also means a hard kill (SIGKILL, OOM killer, power loss) landing
between the write and the replace strands a file nothing will ever reuse.

This module owns **both halves** so they cannot drift: `temp_name`/`new_temp_path`
mint the name, `is_writer_temp` recognises it, and `sweep_blob_temps` collects
the expired ones.

Collection is gated on **age**, not on pid liveness, even though the pid is in
the name. The pid belongs to a process that may be long gone *and* whose number
may have been recycled; unlike `import_jobs.owner_pid` (#162) there is no row
recording the owning host to disambiguate. An age gate needs no such knowledge:
a single attachment write completes in milliseconds, so any generous threshold
separates "crashed hours ago" from "being written right now" with an enormous
margin.
"""

from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

BLOBS_SUBDIR = "blobs"
TEMP_SUFFIX = ".tmp"

# `<sha256-hex>.<pid>.<uuid4-hex>.tmp` — deliberately strict. A loose `*.tmp`
# glob would also collect files an operator or another tool put in the blob
# tree, and the sweep deletes without asking.
_TEMP_NAME_RE = re.compile(r"^[0-9a-f]{64}\.[0-9]+\.[0-9a-f]{32}\.tmp$")


def temp_name(blob_name: str, *, pid: int, token: str) -> str:
    """Return the private temp filename a writer stages `blob_name` under."""
    return f"{blob_name}.{pid}.{token}{TEMP_SUFFIX}"


def new_temp_path(blob_path: Path) -> Path:
    """Return a fresh, writer-private temp path beside `blob_path`.

    Two calls never collide: the uuid4 token separates threads within a process
    just as the pid separates processes.
    """
    return blob_path.with_name(
        temp_name(blob_path.name, pid=os.getpid(), token=uuid.uuid4().hex)
    )


def is_writer_temp(name: str) -> bool:
    """True iff `name` has the exact shape `new_temp_path` mints."""
    return _TEMP_NAME_RE.match(name) is not None


def is_expired(*, mtime: float, now: float, max_age_s: float) -> bool:
    """True iff a temp last modified at `mtime` is older than the age gate.

    Strict: a temp exactly at the gate survives this sweep and is collected by
    the next one. A future `mtime` (clock skew, a touched file) is never
    expired — erring towards keeping a file we might still be writing.
    """
    return (now - mtime) > max_age_s


@dataclass(frozen=True)
class SweepResult:
    """Outcome of one sweep. `scanned` counts recognised temps, not all files."""

    scanned: int = 0
    removed: int = 0
    bytes_reclaimed: int = 0
    errors: int = 0


def sweep_blob_temps(
    root: Path,
    *,
    max_age_s: float,
    now: float,
    dry_run: bool = False,
) -> SweepResult:
    """Delete expired writer temps under `<root>/blobs/`; report what it did.

    `root` is `[attachments] root`. A missing blob tree is a clean no-op (fresh
    install, or a root that has never received an attachment). Per-file errors
    are counted, not raised, so one unreadable file cannot abort the sweep —
    this runs at daemon startup, where raising would cost more than the leak.
    """
    blobs = Path(root) / BLOBS_SUBDIR
    if not blobs.is_dir():
        return SweepResult()

    scanned = removed = reclaimed = errors = 0
    for path in blobs.rglob(f"*{TEMP_SUFFIX}"):
        if not is_writer_temp(path.name):
            continue
        scanned += 1
        try:
            stat = path.stat()
        except FileNotFoundError:
            # Vanished before the age gate could judge it, so the sweep never
            # decided anything about it — reporting it as removed (or, under
            # `--dry-run`, as one we *would* remove) claims an intent we never
            # formed about a file that may well have been young.
            continue
        except OSError:
            errors += 1
            continue

        if not is_expired(mtime=stat.st_mtime, now=now, max_age_s=max_age_s):
            continue
        if not dry_run:
            try:
                path.unlink()
            except FileNotFoundError:
                # Another sweep (or a writer's own cleanup) won the race. The
                # file is gone, which is the outcome we wanted — but we did not
                # free those bytes, so they are not counted below.
                removed += 1
                continue
            except OSError:
                errors += 1
                continue
        removed += 1
        reclaimed += stat.st_size

    return SweepResult(
        scanned=scanned, removed=removed, bytes_reclaimed=reclaimed, errors=errors
    )
