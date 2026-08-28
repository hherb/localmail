# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Two pytest sessions must not share one test database (#335, #329).

`db_conn` truncates every data table before each test, so a second pytest
process running against the same `localmail_test` silently deletes the rows
the first one just seeded — and seeds rows of its own into the first one's
queries. The symptoms read as product bugs: impossible archive states
("48 rows where 9 were seeded"), mid-insert reads, and tests that pass alone
and fail in company.

Measured, because the issue attributed it to lock contention instead: with a
`lock_timeout` armed on the TRUNCATE, three full-suite runs and seven
targeted runs recorded **zero** blocked truncates, while a single concurrent
pytest process reproduced the named failures on the first attempt. The
mechanism is a TRUNCATE that *succeeds*, not one that blocks.

The guard is a Postgres session-level advisory lock keyed on the database
name. Uncontended acquisition costs one round trip; a second session waits
and then fails with a message naming what holds it.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys
import time

import psycopg
import pytest

from tests._db_session_lock import (
    DEFAULT_LOCK_TIMEOUT_S,
    DatabaseSessionBusy,
    acquire_exclusive,
    advisory_lock_key,
    busy_message,
    database_name,
)


# --------------------------------------------------------------------------
# Pure: database_name
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("dsn", "expected"),
    [
        ("postgresql://localmail:local%40%40mail@localhost:5532/localmail_test",
         "localmail_test"),
        ("postgresql://u:p@h:5432/db_with_underscores", "db_with_underscores"),
        ("postgresql://h/plain", "plain"),
        ("postgresql://u:p@h:5432/db?sslmode=require", "db"),
        ("postgresql://u:p@h:5432/db/", "db"),
    ],
)
def test_database_name_is_read_off_the_dsn(dsn: str, expected: str) -> None:
    """The key is per-database, so the name has to come out of the DSN."""
    assert database_name(dsn) == expected


def test_a_dsn_naming_no_database_is_refused_rather_than_keyed_as_blank() -> None:
    """A blank name would key every such DSN to one lock, serialising
    unrelated databases against each other — worse than not locking."""
    with pytest.raises(ValueError, match="database"):
        database_name("postgresql://user:pw@host:5432/")


# --------------------------------------------------------------------------
# Pure: advisory_lock_key
# --------------------------------------------------------------------------


def test_the_key_is_stable_for_one_database() -> None:
    """Two sessions must derive the SAME key or the guard excludes nothing."""
    assert advisory_lock_key("localmail_test") == advisory_lock_key("localmail_test")


def test_the_key_is_stable_ACROSS_processes() -> None:
    """The assertion above is satisfied by a salted `hash()`, which is the
    one implementation that fails silently: two pytest *processes* would
    derive different keys, each acquire happily, and the guard would exclude
    nothing while every test here still passed. The guard's whole purpose is
    cross-process, so the pin has to be too.
    """
    source = (
        "from tests._db_session_lock import advisory_lock_key;"
        "print(advisory_lock_key('localmail_test'))"
    )
    runs = {
        subprocess.run(
            [sys.executable, "-c", source],
            cwd=pathlib.Path(__file__).resolve().parent.parent,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        for _ in range(2)
    }
    assert len(runs) == 1, f"key differs between processes: {runs}"
    assert runs != {""}
    assert next(iter(runs)) == str(advisory_lock_key("localmail_test"))


def test_different_databases_get_different_keys() -> None:
    """Otherwise a run against a scratch database blocks on the shared one."""
    assert advisory_lock_key("localmail_test") != advisory_lock_key("localmail_other")


@pytest.mark.parametrize(
    "database",
    ["localmail_test", "a", "x" * 63, "localmail_test_2", "UPPER_case"],
)
def test_the_key_fits_a_signed_64_bit_integer(database: str) -> None:
    """`pg_advisory_lock(bigint)` rejects anything wider, and it would do so
    at session start on some *other* developer's database name, not ours."""
    key = advisory_lock_key(database)
    assert -(2**63) <= key < 2**63, key


# --------------------------------------------------------------------------
# Pure: busy_message
# --------------------------------------------------------------------------


def test_the_busy_message_names_the_database_and_the_remedy() -> None:
    """The whole point is that the operator can act on it: the failure is
    another session, not a broken test, and waiting is the fix."""
    msg = busy_message("localmail_test", timeout_s=30.0)
    assert "localmail_test" in msg
    assert "30" in msg
    assert "LOCALMAIL_TEST_DSN" in msg


def test_the_default_timeout_is_long_enough_for_a_full_suite() -> None:
    """A full suite is ~3 minutes on this hardware; a timeout under that
    turns a correct wait into a spurious failure for the second session."""
    assert DEFAULT_LOCK_TIMEOUT_S >= 300


# --------------------------------------------------------------------------
# Behavioural: the lock actually excludes
# --------------------------------------------------------------------------


@pytest.fixture
def lock_probe_dsn(db_dsn: str) -> str:
    """A DSN on the same server naming a *different* database.

    The live pytest session holds the lock on its own test database for the
    whole run — that is the fix — so a test cannot acquire that key to prove
    anything about it. Exercising a second key both sidesteps that and
    doubles as the practical proof of the per-database property: if the key
    were global rather than per-database, every acquisition here would fail
    against the session's own lock.
    """
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(db_dsn)
    probe = urlunsplit(parts._replace(path="/postgres"))
    try:
        psycopg.connect(probe, connect_timeout=2).close()
    except Exception as exc:  # a cluster without the maintenance database
        pytest.skip(f"no second database to probe the lock against: {exc}")
    return probe


def test_a_second_session_cannot_hold_the_lock_at_the_same_time(lock_probe_dsn) -> None:
    """The property the whole guard rests on."""
    holder = acquire_exclusive(lock_probe_dsn, timeout_s=5.0)
    try:
        with pytest.raises(DatabaseSessionBusy):
            acquire_exclusive(lock_probe_dsn, timeout_s=0.5, poll_interval_s=0.05)
    finally:
        holder.close()


def test_the_refusal_names_the_database(lock_probe_dsn) -> None:
    """A bare timeout would read as a hung fixture."""
    holder = acquire_exclusive(lock_probe_dsn, timeout_s=5.0)
    try:
        with pytest.raises(DatabaseSessionBusy, match=database_name(lock_probe_dsn)):
            acquire_exclusive(lock_probe_dsn, timeout_s=0.5, poll_interval_s=0.05)
    finally:
        holder.close()


def test_the_lock_is_released_when_the_holding_connection_closes(lock_probe_dsn) -> None:
    """Session-level advisory locks die with their backend, so a pytest run
    killed with SIGKILL cannot wedge every later run — which is why this is
    an advisory lock and not a row in a table."""
    holder = acquire_exclusive(lock_probe_dsn, timeout_s=5.0)
    holder.close()
    second = acquire_exclusive(lock_probe_dsn, timeout_s=5.0)
    second.close()


def test_a_waiting_session_reports_that_it_is_waiting(lock_probe_dsn) -> None:
    """Silence for minutes is indistinguishable from a hang."""
    seen: list[str] = []
    holder = acquire_exclusive(lock_probe_dsn, timeout_s=5.0)
    try:
        with pytest.raises(DatabaseSessionBusy):
            acquire_exclusive(
                lock_probe_dsn, timeout_s=0.4, poll_interval_s=0.05, on_wait=seen.append,
            )
    finally:
        holder.close()
    assert seen, "a session that had to wait said nothing"
    assert database_name(lock_probe_dsn) in seen[0]


def test_an_uncontended_acquisition_does_not_wait(lock_probe_dsn) -> None:
    """The common case is one session; it must not pay the poll interval."""
    started = time.monotonic()
    conn = acquire_exclusive(lock_probe_dsn, timeout_s=5.0, poll_interval_s=10.0)
    conn.close()
    assert time.monotonic() - started < 1.0


def test_the_lock_is_dropped_when_the_caller_closes_and_not_before(lock_probe_dsn) -> None:
    """Pins that the lock rides the returned connection: if `acquire_exclusive`
    leaked it onto a throwaway connection instead, this second acquisition
    would succeed while the first is still held."""
    holder = acquire_exclusive(lock_probe_dsn, timeout_s=5.0)
    try:
        with psycopg.connect(lock_probe_dsn, autocommit=True) as probe:
            got = probe.execute(
                "SELECT pg_try_advisory_lock(%s)", (advisory_lock_key(database_name(lock_probe_dsn)),)
            ).fetchone()
            assert got is not None
            assert got[0] is False
    finally:
        holder.close()


# --------------------------------------------------------------------------
# Integration: conftest actually takes the lock
# --------------------------------------------------------------------------


def test_the_running_pytest_session_holds_the_lock_on_its_test_database(db_dsn) -> None:
    """The module above is inert unless `conftest` takes the lock for real.

    Asserted from inside a live session against a *separate* connection, so
    it pins the wiring rather than the helper: with the `db_dsn` fixture not
    acquiring, this probe succeeds and the guard protects nothing.
    """
    with psycopg.connect(db_dsn, autocommit=True) as probe:
        got = probe.execute(
            "SELECT pg_try_advisory_lock(%s)",
            (advisory_lock_key(database_name(db_dsn)),),
        ).fetchone()
        assert got is not None
        assert got[0] is False, (
            "this pytest session does not hold the session lock on its test "
            "database, so a second session can run against it concurrently"
        )
