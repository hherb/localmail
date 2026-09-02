# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The acceptance harnesses' half of the test-database session lock (#337).

#336 made "one pytest session at a time per test database" hold by
construction, via the ``db_session_lock`` fixture. It covered *pytest*, not
the *database*. The five standalone harnesses under this directory truncate
the same tables against the same ``LOCALMAIL_TEST_DSN`` and took no lock at
all, so starting one beside a suite reproduced #329's corruption in both
directions — and with the same silence, since a ``TRUNCATE`` that deletes
another run's rows succeeds.

They cannot be covered the way pytest sessions are. Nothing collects them:
they match no ``python_files`` pattern, so no conftest fixture reaches them
and the call has to be written into each ``main()``. That is why this module
holds **both** halves — the helper that takes the lock and the rule that says
you must — for the reason ``blob_temps.py`` mints and matches its temp names
in one place: written apart, a rename or an omission strands the other half
silently, which is exactly the failure being fixed.

The policy split against :mod:`tests._db_session_lock` is deliberate and
matches ``account_names.py``: that module answers "can this lock be taken?"
and returns facts, this one decides what a harness *does* about the answer —
one line and a non-zero exit for a contended database, rather than a
traceback that reads as a crash in the harness.
"""
from __future__ import annotations

import ast
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

from tests._db_session_lock import DatabaseSessionBusy, acquire_exclusive

#: The helper a harness must call. Named once and shared with the rule that
#: enforces it, so renaming it cannot leave the rule looking for a function
#: that no longer exists — which would pass every harness silently.
LOCK_HELPER_NAME = "harness_db_lock"

#: The calls that reach the shared database. A harness making any of these
#: outside the lock is racing whatever else is running, so these are what
#: :func:`harness_lock_error` requires to be covered. ``connect`` is the
#: attribute half of ``psycopg.connect``; the rule compares the attribute
#: name, so an aliased import cannot dodge it.
#:
#: Pinned against the harnesses themselves by
#: ``test_the_db_entry_calls_are_the_ones_the_harnesses_actually_use``: a
#: name dropped from here makes the rule quieter without failing anything,
#: which is how a guard goes inert.
DB_ENTRY_CALLS = frozenset({"apply_migrations", "open_pool", "connect"})

#: Exit status for a database another session is already using. Non-zero so
#: a shell loop or CI step stops rather than reporting a run that never
#: happened as a pass, and distinct from 1 so it is greppable.
BUSY_EXIT_CODE = 3


class LockConnection(Protocol):
    """What the lock holder needs of the connection it is handed.

    A Protocol rather than ``psycopg.Connection`` so the tests can inject a
    recorder without claiming to be a real connection — the ``Closable``
    shape ``tests/_pool_leaks.py`` already uses.
    """

    def close(self) -> None: ...


def _stderr_notice(message: str) -> None:
    """Put a wait notice in front of the operator, unbuffered.

    Harness output is not captured (there is no pytest here), so unlike
    conftest's ``_announce`` a plain write is enough — but it must be
    flushed, since the whole point is to be seen *during* the wait.
    """
    print(message, file=sys.stderr, flush=True)


@contextmanager
def harness_db_lock(
    dsn: str,
    *,
    acquire: Callable[..., LockConnection] = acquire_exclusive,
    announce: Callable[[str], None] = _stderr_notice,
) -> Iterator[LockConnection]:
    """Hold the session lock for ``dsn`` for the duration of the block.

    Wrap everything a harness does to the database, starting at
    ``apply_migrations`` — the lock has to be held before the first touch,
    not merely at some point during the run, which is the ordering
    :func:`harness_lock_error` enforces.

    Releases on the way out whatever happened, because a harness raising is
    ordinary (an unreachable DSN, an embedding model that will not load) and
    must not leave the next run waiting on a socket the OS has yet to reap.

    A contended database announces one line and exits with
    :data:`BUSY_EXIT_CODE`. That is the call conftest makes for the same
    condition: someone else running is a wait, not a fault, and a traceback
    out of a harness reads as the harness having crashed.
    """
    try:
        holder = acquire(dsn, on_wait=announce)
    except DatabaseSessionBusy as exc:
        # Announced, then exited with a *code*. `SystemExit(str)` would print
        # the string and exit 1 — a status a harness has many other reasons to
        # return, so a caller could not tell "someone else is running" from
        # "the eval failed its gates". The two channels are separate on
        # purpose: the message is for the operator, the code for the shell.
        announce(str(exc))
        raise SystemExit(BUSY_EXIT_CODE) from None
    try:
        yield holder
    finally:
        holder.close()


def harness_entry_points(directory: Path) -> list[Path]:
    """Return the harness scripts under ``directory``, sorted.

    Entry points only: ``browse_explain_lib.py`` touches the database too,
    but it is imported by ``run_browse_explain.py`` and runs inside that
    harness's lock. Holding the rule to the scripts an operator actually
    starts is what keeps its message addressed to whoever can act on it.
    """
    return sorted(directory.glob("run_*.py"))


def _lock_covered_calls(main: ast.FunctionDef) -> set[int]:
    """Return the ids of Call nodes sitting inside a lock-held block."""
    covered: set[int] = set()
    for node in ast.walk(main):
        if not isinstance(node, ast.With):
            continue
        if not any(_is_lock_call(item.context_expr) for item in node.items):
            continue
        for statement in node.body:
            covered.update(
                id(inner) for inner in ast.walk(statement) if isinstance(inner, ast.Call)
            )
    return covered


def _is_lock_call(expr: ast.expr) -> bool:
    """Is ``expr`` a call of the lock helper?

    Read off the AST, never the text. The reason for this rule is written in
    prose in every harness that follows it, and a substring scan reads that
    prose as compliance — the mistake #291 paid for once already.
    """
    if not isinstance(expr, ast.Call):
        return False
    func = expr.func
    if isinstance(func, ast.Name):
        return func.id == LOCK_HELPER_NAME
    if isinstance(func, ast.Attribute):
        return func.attr == LOCK_HELPER_NAME
    return False


def _called_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def harness_lock_error(source: str, filename: str) -> str | None:
    """Return why ``source`` fails the lock rule, or ``None`` if it passes.

    A message or ``None``, the ``account_names.py::account_name_error``
    shape: the caller decides what an error *is*, so the same rule serves
    the test suite here and could serve a lint step later.

    The rule is that every call in :data:`DB_ENTRY_CALLS` made by ``main``
    sits inside a ``with`` block holding :data:`LOCK_HELPER_NAME`. Requiring
    only that the helper appear *somewhere* would accept a harness that
    migrates first and locks afterwards, which is taking no lock at all —
    the truncate has already run by then.
    """
    tree = ast.parse(source, filename=filename)
    mains = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    ]
    if not mains:
        return (
            f"{filename} defines no module-level main(), so the acceptance-harness "
            f"lock rule cannot be applied to it. Give it a main() that wraps its "
            f"database work in `with {LOCK_HELPER_NAME}(dsn):`, or move it out of "
            f"the run_*.py entry-point namespace if it is not a harness."
        )

    covered = _lock_covered_calls(mains[0])
    uncovered = sorted(
        {
            name
            for node in ast.walk(mains[0])
            if isinstance(node, ast.Call)
            and id(node) not in covered
            and (name := _called_name(node)) in DB_ENTRY_CALLS
        }
    )
    if not uncovered:
        return None
    return (
        f"{filename} reaches the test database outside the session lock: "
        f"{', '.join(uncovered)}. Two runs sharing one database truncate each "
        f"other's tables and neither errors (#329, #337). Wrap the database "
        f"work — from apply_migrations onward — in "
        f"`with {LOCK_HELPER_NAME}(dsn):`."
    )
