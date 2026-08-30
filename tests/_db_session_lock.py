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

* it needs no privileges of its own, where the per-worker database #335
  suggests may. Migration ``0004`` opens with ``CREATE EXTENSION vector``,
  and ``vector`` is not a *trusted* extension, so creating it needs
  superuser. On the reference cluster the test role is ``CREATEDB`` but not
  superuser, so a migrated per-worker database cannot be built there. Note
  this does **not** generalise: CI passes ``POSTGRES_USER: localmail`` to the
  ``pgvector`` image, which makes that role the bootstrap superuser, so the
  constraint does not bind on CI. Measure before relying on it;
* an uncontended acquisition is one connect plus one query, and never
  sleeps, so the single-session case (overwhelmingly the common one) costs a
  dedicated connection held open for the run and no waiting;
* the lock dies with its backend, so a run killed with SIGKILL releases it
  rather than wedging every later run — which a row in a table would not.

That last property cuts both ways, and :func:`verify_still_held` is the
answer to the other edge: a backend killed by a restart, an
``idle_session_timeout`` or a reaped TCP flow releases the lock just as
willingly, while ``conn.closed`` still reads ``False``. So callers holding
this for a long time must re-check it at the point it protects — for
``db_conn``, immediately before the ``TRUNCATE`` — rather than assume it
survived.

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
from collections.abc import Callable, Mapping

import psycopg
from psycopg.conninfo import conninfo_to_dict

#: Namespace mixed into the digest so a key derived here is very unlikely to
#: collide with an advisory lock some other tool takes for its own reasons.
#: Unlikely, not impossible — nothing stops another tool picking an arbitrary
#: bigint that happens to equal ours.
#:
#: Note Postgres already scopes advisory locks **per database** (`pg_locks`
#: keys them by database OID), so the per-database key below is not what
#: provides that isolation; it is defence in depth, and it is what keeps the
#: `busy_message` honest about which database is contended. An earlier
#: comment here claimed the keyspace was cluster-global. It is not: the same
#: key is granted concurrently in two databases, which is measurable in one
#: query and was the basis of two other rationales that have also been
#: corrected.
_KEY_NAMESPACE = b"localmail.tests.session-lock.v1"

#: Postgres `pg_advisory_lock(bigint)` takes a signed 64-bit key, so the
#: digest is requested at exactly that width (blake2b is parameterised by
#: output size; nothing is truncated after the fact).
_KEY_BITS = 64
_KEY_BYTES = _KEY_BITS // 8

#: How long a waiting session sleeps between attempts. Short enough that the
#: wait ends promptly when the holder finishes, long enough that a session
#: blocked for minutes is not spinning on the server.
POLL_INTERVAL_S = 1.0

#: How long a second session waits before giving up. A full suite is ~3
#: minutes on the reference hardware, so a shorter default would turn a
#: correct wait into a spurious failure for whoever started second.
#: This is the *literal* default; `resolve_lock_timeout_s` applies the
#: operator's override. Kept apart because a test pinning "long enough for a
#: full suite" must assert on the constant — asserting on the resolved value
#: turns the documented override into a red suite.
DEFAULT_LOCK_TIMEOUT_S = 600.0

#: Environment variable that overrides it, for a slower host or a longer run.
LOCK_TIMEOUT_ENV_VAR = "LOCALMAIL_TEST_DB_LOCK_TIMEOUT_S"

#: How long to wait for the TCP connect itself. Without it libpq waits out
#: the OS TCP timeout (~130 s on Linux) against a host that drops SYNs — and
#: it does so inside session-fixture setup, where output is captured, which
#: is the "silence reads as a hung run" symptom `on_wait` exists to prevent.
#: `on_wait` cannot help here: it only fires after a failed lock *attempt*.
CONNECT_TIMEOUT_S = 10


class DatabaseSessionBusy(RuntimeError):
    """Another pytest session holds this test database.

    A named class rather than a bare ``TimeoutError`` so a caller can tell
    "someone else is running the suite" — which is a wait, not a fault —
    from a Postgres that is genuinely unreachable.

    Deliberately **not** named ``Test…``. Nothing collects *this* module —
    it matches no ``python_files`` pattern — but ``test_db_session_lock.py``
    imports the class, and pytest collects ``Test*`` classes it finds in a
    test module's namespace, imported ones included. It would then warn that
    it cannot collect this one, because it inherits ``BaseException.__init__``
    (pytest's check is ``__init__ is not object.__init__``). Same call as
    ``probe_connection``.
    """


class SessionLockLost(RuntimeError):
    """The session lock was taken but is no longer held.

    Distinct from :class:`DatabaseSessionBusy`, which means we never got it.
    This one means the guard was in force and has silently lapsed, so every
    ``TRUNCATE`` from here on may be racing another session.
    """


def database_name(dsn: str) -> str:
    """Return the database ``dsn`` actually connects to.

    Resolved by :func:`psycopg.conninfo.conninfo_to_dict`, i.e. by the same
    rule libpq itself applies — **not** by hand. `urlsplit` understands only
    the URI form, while `psycopg.connect` also takes the keyword/value form
    (``host=… dbname=…``) that ``LOCALMAIL_TEST_DSN`` may legitimately carry.
    Against that, a hand parser returns the *whole connection string* as the
    name, with no error: two sessions spelling one database differently then
    derive two keys, both acquire, and the guard excludes nothing. That is
    the silent failure :func:`advisory_lock_key` is written to prevent,
    arriving one function earlier. It also put ``password=…`` into
    :func:`busy_message`, which is printed to the terminal and into CI logs.

    A DSN naming no database is refused rather than keyed as the empty
    string: every such DSN would otherwise share one key and serialise
    unrelated databases against each other, which is worse than not locking.
    """
    name = conninfo_to_dict(dsn).get("dbname")
    if not isinstance(name, str) or not name:
        raise ValueError(f"DSN names no database, so no per-database lock key exists: {dsn!r}")
    return name


def resolve_lock_timeout_s(env: Mapping[str, str] | None = None) -> float:
    """Return the wait budget, honouring the documented override.

    Read per call rather than at import so a malformed value fails the one
    fixture that needs it, not collection of the whole suite — the import
    form turned a typo into a bare ``ValueError`` that took ~2000 tests
    which never touch a database down with it.
    """
    raw = (os.environ if env is None else env).get(LOCK_TIMEOUT_ENV_VAR)
    if raw is None:
        return DEFAULT_LOCK_TIMEOUT_S
    try:
        return float(raw)
    except ValueError:
        raise ValueError(
            f"{LOCK_TIMEOUT_ENV_VAR} must be a number of seconds, not {raw!r}"
        ) from None


def advisory_lock_key(database: str) -> int:
    """Return the stable advisory-lock key for ``database``.

    Derived by digest rather than by hash() — the latter is salted per
    process, so two pytest sessions would compute different keys and the
    guard would exclude nothing, with every unit test still green. That is
    the failure mode this whole module is shaped around; note it has a
    second door, which is why :func:`database_name` defers to libpq instead
    of parsing the DSN by hand.
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
    """Return the notice for a session that has to wait.

    Emitting it — once, past pytest's output capture — belongs to
    :func:`acquire_exclusive` and to conftest's ``_announce``, not here.
    """
    return (
        f"waiting for another pytest session to release the test database "
        f"{database!r}; set LOCALMAIL_TEST_DSN to run against your own."
    )


def acquire_exclusive(
    dsn: str,
    *,
    timeout_s: float | None = None,
    poll_interval_s: float = POLL_INTERVAL_S,
    on_wait: Callable[[str], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> psycopg.Connection:
    """Take the session lock for ``dsn`` and return the connection holding it.

    The caller owns the returned connection: the lock lives for exactly as
    long as it stays open, which is why it is returned rather than closed
    here. Closing it releases the lock — and so does anything that kills the
    backend, which is why long-lived callers should re-check with
    :func:`verify_still_held` rather than assume.

    ``on_wait`` is called at most once, with :func:`waiting_message`, the
    first time an attempt fails **with budget still on the clock**. A call
    that gives up on its first attempt (``timeout_s=0``) never waited, so it
    announces nothing and only raises. A session that blocks for minutes in
    silence is indistinguishable from a hung fixture, hence the notice.
    """
    if timeout_s is None:
        timeout_s = resolve_lock_timeout_s()
    database = database_name(dsn)
    key = advisory_lock_key(database)
    conn = psycopg.connect(dsn, autocommit=True, connect_timeout=CONNECT_TIMEOUT_S)
    announced = False
    deadline = clock() + timeout_s
    try:
        while True:
            row = conn.execute("SELECT pg_try_advisory_lock(%s)", (key,)).fetchone()
            if row is None:
                # Unreachable: a scalar SELECT with no FROM always returns one
                # row. Named rather than folded into the "contended" branch
                # below, which would hand the operator a confident and false
                # diagnosis ("another pytest session") plus two remedies that
                # cannot apply — after waiting out the full timeout for it.
                # Same call as `upsert_message`'s no-match RuntimeError.
                raise RuntimeError(
                    "pg_try_advisory_lock returned no row; the lock query has "
                    "been changed and this branch needs revisiting"
                )
            if row[0]:
                return conn
            remaining = deadline - clock()
            if remaining <= 0:
                raise DatabaseSessionBusy(busy_message(database, timeout_s=timeout_s))
            if not announced:
                announced = True
                if on_wait is not None:
                    on_wait(waiting_message(database))
            # Clamped, so the wait cannot overrun `timeout_s` by up to a poll
            # interval and make `busy_message`'s "waited Ns" an understatement.
            sleep(min(poll_interval_s, remaining))
    except BaseException:
        conn.close()
        raise


def verify_still_held(conn: psycopg.Connection) -> None:
    """Raise :class:`SessionLockLost` unless ``conn`` still holds its lock.

    The lock is acquired once and then rides the most idle connection in the
    suite — open for the whole run with no traffic — which is exactly the
    connection a Postgres restart, an ``idle_session_timeout``, a failover or
    a reaped TCP flow takes out first. When that happens the advisory lock
    dies with the backend (the same property that makes a SIGKILLed run safe
    rather than wedging the next one), a second session acquires freely, and
    both resume truncating each other's tables. Nothing reports it: psycopg
    does not notice a dead backend until the connection is used, so
    ``conn.closed`` still reads ``False`` and even ``close()`` returns clean.

    So the guard has to be re-checked at the point it protects — before each
    ``TRUNCATE`` — rather than trusted for the length of the run.

    Asked of ``pg_locks`` rather than by pinging the connection, because a
    live connection is not the same claim as a held lock: a pooler issuing
    ``DISCARD ALL`` releases every advisory lock while the socket survives,
    and a liveness check would call that healthy. We take exactly one
    advisory lock per holder, so "this backend holds one" is unambiguous.
    """
    try:
        row = conn.execute(
            "SELECT count(*) > 0 FROM pg_locks "
            "WHERE locktype = 'advisory' AND pid = pg_backend_pid() AND granted"
        ).fetchone()
    except psycopg.Error as exc:
        raise SessionLockLost(
            "the test-database session lock is no longer held: the connection "
            f"holding it has failed ({exc}). Another pytest session may have "
            "been running against this database unguarded; treat this run's "
            "results as untrustworthy and re-run it."
        ) from exc
    if row is None or not row[0]:
        raise SessionLockLost(
            "the test-database session lock is no longer held, although its "
            "connection is still open. Another pytest session can now truncate "
            "this database mid-run; treat this run's results as untrustworthy "
            "and re-run it."
        )
