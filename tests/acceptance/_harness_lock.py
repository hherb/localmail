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
holds **every** half — :func:`harness_db_lock`, which takes the lock;
:func:`harness_lock_error`, which says a harness must; and
:func:`acceptance_coverage_error`, which says which files that rule has to be
applied to — for the reason ``blob_temps.py`` mints and matches its temp names
in one place: written apart, a rename or an omission strands the others
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
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol

from tests._db_session_lock import (
    DatabaseSessionBusy,
    acquire_exclusive,
    verify_still_held,
)

#: Where the real helper lives. A harness must import it from here: the rule
#: below matches a *name*, and a locally defined `harness_db_lock` that
#: yields nothing satisfies that while taking no lock at all.
#:
#: Read off ``__name__`` rather than written out, so moving this module
#: cannot leave the rule demanding an import path that no longer exists —
#: which would reject every compliant harness. Loud either way; this makes
#: it correct instead.
HARNESS_LOCK_MODULE = __name__

#: The helper a harness must call. Named once and shared with the rule that
#: enforces it, so renaming it cannot leave the rule looking for a function
#: that no longer exists — which would pass every harness silently.
LOCK_HELPER_NAME = "harness_db_lock"

#: The calls that reach the shared database. A harness making any of these
#: outside the lock is racing whatever else is running, so these are what
#: :func:`harness_lock_error` requires to be covered. ``connect`` is the
#: attribute half of ``psycopg.connect``, so ``import psycopg as pg`` cannot
#: dodge it — the attribute name is what is compared.
#:
#: **Known limitation, deliberate:** a *callee* rename does dodge it.
#: ``from psycopg import connect as pg_connect`` rebinds the name the call
#: site spells, and following that means resolving imports rather than
#: reading call names. No harness does it; the cost of the gap is one
#: unreported harness, against a second import resolver to keep in step with
#: Python's.
#:
#: Pinned by ``test_every_db_entry_call_name_is_reached_by_some_harness``,
#: which asserts every member is actually called by a harness — so a name
#: dropped from here fails. (The per-file
#: ``test_the_db_entry_calls_are_the_ones_the_harnesses_actually_use``
#: cannot: it asserts a non-empty *intersection*, which ``apply_migrations``
#: satisfies for all five. This comment used to cite it, and a dropped name
#: was a surviving mutation.)
DB_ENTRY_CALLS = frozenset({"apply_migrations", "open_pool", "connect"})

#: Modules in this directory that reach the database but are **not** entry
#: points, because they are imported by one and run inside its lock. Kept as
#: an explicit allowlist rather than inferred, and checked: an entry here may
#: do no database work at *import* time, since that would run before the
#: importing harness takes the lock. See :func:`acceptance_coverage_error`.
COVERED_LIBRARIES = frozenset({"browse_explain_lib.py"})

#: Exit status for a database another session is already using. Non-zero so
#: a shell loop or CI step stops rather than reporting a run that never
#: happened as a pass, and distinct from the statuses a harness reaches for
#: its own reasons: 1 (an eval failing its acceptance gates) and 2 (argparse).
#:
#: Pinned against literals by
#: ``test_the_busy_exit_code_is_a_status_no_other_outcome_uses``. The two
#: behavioural tests cannot do it — both compare the observed status against
#: this constant, so both sides move together and ``BUSY_EXIT_CODE = 0`` was
#: a surviving mutation, i.e. a refused database reporting success.
BUSY_EXIT_CODE = 3


class LockConnection(Protocol):
    """What the lock holder needs of the connection it is handed.

    A Protocol rather than ``psycopg.Connection`` so the tests can inject a
    recorder without claiming to be a real connection — the ``Closable``
    shape ``tests/_pool_leaks.py`` already uses.
    """

    def close(self) -> None: ...


def _stderr_notice(message: str) -> None:
    """Put a wait notice in front of the operator.

    A plain write, where conftest's ``_announce`` has to reach past pytest's
    fixture-output capture: a harness is normally started from a shell. Not
    always — the end-to-end test starts one from pytest with
    ``capture_output=True`` — but a captured stream is still delivered,
    which is what that case needs.

    ``flush=True`` is belt and braces rather than the mechanism: CPython
    line-buffers ``sys.stderr`` even to a pipe, so the notice is readable
    during the wait either way. It is cheap and it removes the dependence on
    that remaining true.
    """
    print(message, file=sys.stderr, flush=True)


def checkpoint(
    holder: LockConnection,
    *,
    verify: Callable[[Any], None] = verify_still_held,
) -> None:
    """Re-check the lock immediately before a destructive statement.

    :mod:`tests._db_session_lock` states the obligation: the lock rides the
    most idle connection in the run and dies silently with its backend — a
    restart, an ``idle_session_timeout``, a failover, a reaped TCP flow —
    while ``conn.closed`` still reads ``False``. ``db_conn`` discharges it
    before every ``TRUNCATE``; a harness has no per-test seam, so it calls
    this before the truncates that are *far* from acquisition.

    Near ones do not need it (the window is microseconds). The two that do
    are ``run_browse_explain``'s final clean-up, after a full probe run, and
    ``run_chunk_insert_bench``'s per-mode truncate, which on the default
    ``--mode both`` fires after a complete benchmark.
    """
    verify(holder)


@contextmanager
def harness_db_lock(
    dsn: str,
    *,
    acquire: Callable[..., LockConnection] = acquire_exclusive,
    announce: Callable[[str], None] = _stderr_notice,
    verify: Callable[[Any], None] = verify_still_held,
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

    **The lock is re-checked on the way out**, raising
    :class:`~tests._db_session_lock.SessionLockLost` if it lapsed during the
    run. It cannot undo a truncate that already raced, but it is what turns
    "this run's numbers are quietly wrong" into a failed run — the whole
    point of a guard whose failure mode is silence. It runs only when the
    body completed: a body that raised has its own diagnosis, and rewriting
    every harness crash into ``SessionLockLost`` would bury it.
    :func:`checkpoint` is the finer-grained half, for truncates far from
    acquisition.
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
        verify(holder)
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


def _lock_covered_calls(func: ast.FunctionDef) -> set[int]:
    """Return the ids of Call nodes in ``func`` inside a lock-held block.

    Called for ``main`` and for every helper the walk follows into, so the
    parameter is any function, not the entry point.
    """
    covered: set[int] = set()
    for node in ast.walk(func):
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


def _local_functions(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    """Module-level ``def``s, by the name Python will resolve.

    A later definition wins, because that is what the interpreter binds. The
    rule used to read the *first* ``main``, so a module defining it twice was
    audited on the one that never runs.
    """
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }


def _module_level_db_calls(tree: ast.Module) -> list[str]:
    """DB entry calls made at import time, outside every function body.

    No ``with`` inside ``main`` can cover these — they have already run by
    the time ``main`` is called — so they are unlocked by construction
    rather than by position, and get their own message.
    """
    names: set[str] = set()
    for statement in tree.body:
        if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        for node in ast.walk(statement):
            if not isinstance(node, ast.Call):
                continue
            name = _called_name(node)
            if name is not None and name in DB_ENTRY_CALLS:
                names.add(name)
    return sorted(names)


def _uncovered_db_calls(
    func: ast.FunctionDef,
    functions: Mapping[str, ast.FunctionDef],
    seen: frozenset[str],
) -> set[str]:
    """DB entry names reachable from ``func`` without the lock held.

    Follows calls into module-level helpers, because the rule is about what
    a harness *does*, not about where it is written. ``run_chunk_insert_bench``
    already keeps its ``psycopg.connect`` and its ``TRUNCATE`` in a helper,
    so a rule reading ``main`` alone saw neither: hoisting that helper's call
    out of the ``with`` left every truncate unlocked and reported nothing.

    A helper reached from *inside* the lock is not followed — it is covered
    by construction, whatever it does. One reached from outside is followed
    with its own lock-covered set computed, so a helper that takes the lock
    itself is compliant. ``seen`` bounds mutual recursion.
    """
    covered = _lock_covered_calls(func)
    names: set[str] = set()
    for node in ast.walk(func):
        if not isinstance(node, ast.Call) or id(node) in covered:
            continue
        name = _called_name(node)
        if name is None:
            continue
        if name in DB_ENTRY_CALLS:
            names.add(name)
        elif name in functions and name not in seen:
            names |= _uncovered_db_calls(
                functions[name], functions, seen | {name}
            )
    return names


def _imports_the_helper(tree: ast.Module) -> bool:
    """Does the module import the real :data:`LOCK_HELPER_NAME`?

    The lock check matches a bare name, so a local ``harness_db_lock`` that
    yields ``None`` passes it while locking nothing. Requiring the import
    closes that without teaching the matcher to resolve names.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.module != HARNESS_LOCK_MODULE:
            continue
        if any(
            alias.name == LOCK_HELPER_NAME and alias.asname is None
            for alias in node.names
        ):
            return True
    return False


def harness_lock_error(source: str, filename: str) -> str | None:
    """Return why ``source`` fails the lock rule, or ``None`` if it passes.

    A message or ``None``, the ``account_names.py::account_name_error``
    shape: the caller decides what an error *is*, so the same rule serves
    the test suite here and could serve a lint step later.

    The rule is that every :data:`DB_ENTRY_CALLS` member reachable from
    ``main`` — directly, or through a module-level helper it calls — sits
    inside a ``with`` block holding :data:`LOCK_HELPER_NAME`, and that the
    module does nothing to the database at import time. Requiring only that
    the helper appear *somewhere* would accept a harness that migrates first
    and locks afterwards, which is taking no lock at all — the truncate has
    already run by then.

    **Known imprecision, deliberate.** A call listed as the lock's *sibling*
    context expression — ``with harness_db_lock(dsn), psycopg.connect(dsn):``
    — is reported, although the lock is genuinely entered first. Covering
    only the ``with`` body is what makes "some other context manager" fail,
    and that is the more valuable half; the cost is that the combined form
    must be written nested. It fails closed, so the imprecision costs a
    spurious report, never a missed one.
    """
    tree = ast.parse(source, filename=filename)
    functions = _local_functions(tree)

    main = functions.get("main")
    if main is None:
        return (
            f"{filename} defines no module-level main(), so the acceptance-harness "
            f"lock rule cannot be applied to it. Give it a main() that wraps its "
            f"database work in `with {LOCK_HELPER_NAME}(dsn):`, or move it out of "
            f"the run_*.py entry-point namespace if it is not a harness."
        )

    at_import = _module_level_db_calls(tree)
    if at_import:
        return (
            f"{filename} reaches the test database outside the session lock: "
            f"{', '.join(at_import)}. These run at import, before main() is "
            f"called, so no `with` inside main() can cover them. Move them into "
            f"main()'s `with {LOCK_HELPER_NAME}(dsn):` block."
        )

    uncovered = sorted(_uncovered_db_calls(main, functions, frozenset({"main"})))
    if uncovered:
        return (
            f"{filename} reaches the test database outside the session lock: "
            f"{', '.join(uncovered)}. Two runs sharing one database truncate each "
            f"other's tables and neither errors (#329, #337). Wrap the database "
            f"work — from apply_migrations onward — in "
            f"`with {LOCK_HELPER_NAME}(dsn):`."
        )

    if not _imports_the_helper(tree):
        return (
            f"{filename} wraps its database work in something spelled "
            f"{LOCK_HELPER_NAME!r} that it does not import from "
            f"{HARNESS_LOCK_MODULE}. A local definition of that name satisfies "
            f"the position rule while taking no lock at all. Import the real "
            f"one: `from {HARNESS_LOCK_MODULE} import {LOCK_HELPER_NAME}`."
        )
    return None


def acceptance_coverage_error(directory: Path) -> str | None:
    """Return why ``directory``'s database modules are unaccounted for.

    :func:`harness_entry_points` globs ``run_*.py``, which is a naming
    habit, not a rule — a harness called ``bench_*.py``, or dropped in a
    subdirectory, took no lock and was never asked to, with every test still
    green. This is the reverse cross-check ``tests/_pool_leaks.py`` builds
    for the same reason (``pool_constructor_calls`` requires the scanned set
    to *equal* ``POOL_SEAMS``, because ``missing_seam_error`` asks only
    whether a name is present).

    Every module here that names a :data:`DB_ENTRY_CALLS` member must be an
    entry point, and so subject to :func:`harness_lock_error`, or be listed
    in :data:`COVERED_LIBRARIES` — in which case it must do nothing to the
    database at import, since that runs before the importing harness locks.
    """
    entry_points = {p.name for p in harness_entry_points(directory)}
    problems: list[str] = []
    for path in sorted(directory.rglob("*.py")):
        relative = path.relative_to(directory)
        if path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text(), filename=path.name)
        touches = any(
            _called_name(node) in DB_ENTRY_CALLS
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
        )
        if not touches:
            continue
        if str(relative) in entry_points:
            continue
        if path.name in COVERED_LIBRARIES:
            at_import = _module_level_db_calls(tree)
            if at_import:
                problems.append(
                    f"{relative} is allowlisted as a library, but reaches the "
                    f"database at import time ({', '.join(at_import)}) — which "
                    f"runs before the harness importing it takes the lock"
                )
            continue
        problems.append(
            f"{relative} reaches the test database but is neither a "
            f"`run_*.py` entry point (so the lock rule never sees it) nor "
            f"listed in COVERED_LIBRARIES"
        )
    if not problems:
        return None
    return (
        f"unaccounted database modules under {directory.name}/: "
        + "; ".join(problems)
        + ". Rename it to `run_*.py` if an operator starts it, or add it to "
        "COVERED_LIBRARIES if a harness imports it."
    )
