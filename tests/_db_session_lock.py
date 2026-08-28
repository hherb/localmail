# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""One pytest session at a time per test database (#335, #329).

`db_conn` opens every test with ``TRUNCATE … RESTART IDENTITY CASCADE`` over
every data table. That is correct for one session and destructive for two:
a second pytest process on the same ``localmail_test`` deletes the rows the
first has just seeded, and seeds rows of its own into the first one's
queries. Nothing errors — the truncate *succeeds* — so the damage surfaces
as impossible archive states and as tests that pass alone and fail in
company, which reads as a product bug rather than as contention.

#335 attributed this to ``TRUNCATE`` blocking on a connection left open by a
previous test's ``open_pool``. Measured against that: with a ``lock_timeout``
armed on the truncate, three full-suite runs and seven targeted runs recorded
**zero** blocked truncates, while one concurrent pytest process reproduced
the exact tests the issue names on the first attempt. Blocking is not the
mechanism; sharing is.

The guard is a **session-level Postgres advisory lock** keyed on the database
name, taken once per pytest session before migrations run. Three properties
earn it that shape:

* it needs no privileges — the test role has ``CREATEDB`` but not superuser,
  and migration ``0001`` needs ``CREATE EXTENSION vector``, so the per-worker
  database #335 suggests cannot actually be built by the test role;
* an uncontended acquisition is one round trip, so the single-session case
  (overwhelmingly the common one) pays nothing measurable;
* the lock dies with its backend, so a run killed with SIGKILL releases it
  rather than wedging every later run — which a row in a table would not.

Serialising is deliberate and is not a parallelism regression: two sessions
sharing this database were never running concurrently in any useful sense,
they were corrupting each other. A session that wants real parallelism points
``LOCALMAIL_TEST_DSN`` at its own database, which the per-database key already
keeps independent.

**Adding ``pytest-xdist`` needs this module changed first.** It is not a
dependency today. Each xdist worker is its own process, so under one shared
DSN exactly one would acquire and the rest would block for
``DEFAULT_LOCK_TIMEOUT_S`` and then fail — which looks like this guard being
broken rather than like the workers sharing a database they must not share.
The fix at that point is per-worker DSNs (``PYTEST_XDIST_WORKER`` suffixing),
which the per-database key already supports and which is the direction #335
suggested; note it needs a database the test role can *create*, and see the
privilege constraint above before assuming it can.
"""
from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Callable
from urllib.parse import urlsplit

import psycopg

#: Namespace mixed into the digest so a key derived here cannot collide with
#: an advisory lock some other tool takes on the same server for its own
#: reasons. Advisory-lock keyspace is global to the cluster, not per-database.
_KEY_NAMESPACE = b"localmail.tests.session-lock.v1"

#: Postgres `pg_advisory_lock(bigint)` takes a signed 64-bit key, so the
#: digest is truncated to exactly that width.
_KEY_BITS = 64
_KEY_BYTES = _KEY_BITS // 8

#: How long a waiting session sleeps between attempts. Short enough that the
#: wait ends promptly when the holder finishes, long enough that a session
#: blocked for minutes is not spinning on the server.
POLL_INTERVAL_S = 1.0

#: How long a second session waits before giving up. A full suite is ~3
#: minutes on the reference hardware, so a shorter default would turn a
#: correct wait into a spurious failure for whoever started second.
#: Overridable for a slower host or a longer run.
DEFAULT_LOCK_TIMEOUT_S = float(os.environ.get("LOCALMAIL_TEST_DB_LOCK_TIMEOUT_S", "600"))


class DatabaseSessionBusy(RuntimeError):
    """Another pytest session holds this test database.

    A named class rather than a bare ``TimeoutError`` so a caller can tell
    "someone else is running the suite" — which is a wait, not a fault —
    from a Postgres that is genuinely unreachable.

    Deliberately **not** named ``Test…``: pytest collects any module-level
    class whose name starts with that prefix, and warns that it cannot,
    because this one has ``__init__``. Same call as ``probe_connection``.
    """


def database_name(dsn: str) -> str:
    """Return the database a libpq URI addresses.

    The key is per-database so that a session pointed at its own scratch
    database never blocks on the shared one. A DSN naming no database is
    refused rather than keyed as the empty string: every such DSN would
    otherwise share one key and serialise unrelated databases against each
    other, which is worse than not locking at all.
    """
    name = urlsplit(dsn).path.strip("/")
    if not name:
        raise ValueError(f"DSN names no database, so no per-database lock key exists: {dsn!r}")
    return name


def advisory_lock_key(database: str) -> int:
    """Return the stable advisory-lock key for ``database``.

    Derived by digest rather than by hash() — the latter is salted per
    process, so two pytest sessions would compute different keys and the
    guard would exclude nothing, which is the one way this can fail silently.
    """
    digest = hashlib.blake2b(_KEY_NAMESPACE + database.encode("utf-8"), digest_size=_KEY_BYTES).digest()
    return int.from_bytes(digest, "big", signed=True)


def busy_message(database: str, *, timeout_s: float) -> str:
    """Return the operator-facing explanation for a contended database."""
    return (
        f"another pytest session is using the test database {database!r} "
        f"(waited {timeout_s:g}s). Two sessions sharing one test database "
        f"corrupt each other's runs: every test truncates every table. "
        f"Wait for the other run to finish, or point LOCALMAIL_TEST_DSN at a "
        f"different database."
    )


def waiting_message(database: str) -> str:
    """Return the notice a session prints once, when it has to wait."""
    return (
        f"waiting for another pytest session to release the test database "
        f"{database!r}; set LOCALMAIL_TEST_DSN to run against your own."
    )


def acquire_exclusive(
    dsn: str,
    *,
    timeout_s: float = DEFAULT_LOCK_TIMEOUT_S,
    poll_interval_s: float = POLL_INTERVAL_S,
    on_wait: Callable[[str], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> psycopg.Connection:
    """Take the session lock for ``dsn`` and return the connection holding it.

    The caller owns the returned connection: the lock lives for exactly as
    long as it stays open, which is why it is returned rather than closed
    here. Closing it releases the lock.

    ``on_wait`` is called at most once, with :func:`waiting_message`, the
    first time an attempt fails — a session that blocks for minutes in
    silence is indistinguishable from a hung fixture.
    """
    database = database_name(dsn)
    key = advisory_lock_key(database)
    conn = psycopg.connect(dsn, autocommit=True)
    announced = False
    deadline = clock() + timeout_s
    try:
        while True:
            row = conn.execute("SELECT pg_try_advisory_lock(%s)", (key,)).fetchone()
            if row is not None and row[0]:
                return conn
            if clock() >= deadline:
                raise DatabaseSessionBusy(busy_message(database, timeout_s=timeout_s))
            if not announced:
                announced = True
                if on_wait is not None:
                    on_wait(waiting_message(database))
            sleep(poll_interval_s)
    except BaseException:
        conn.close()
        raise
