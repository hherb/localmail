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

import ast
import os
import pathlib
import subprocess
import sys
from collections.abc import Iterator
from unittest import mock
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest

from tests._db_session_lock import (
    DEFAULT_LOCK_TIMEOUT_S,
    DatabaseSessionBusy,
    SessionLockLost,
    acquire_exclusive,
    advisory_lock_key,
    busy_message,
    database_name,
    resolve_lock_timeout_s,
    verify_still_held,
    waiting_message,
)
from tests.conftest import _announce


@pytest.fixture(scope="module")
def _conftest_source() -> ast.Module:
    """conftest's AST, for the two ordering pins below."""
    path = pathlib.Path(__file__).resolve().parent / "conftest.py"
    return ast.parse(path.read_text(encoding="utf-8"))


def _function_named(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"conftest has no function named {name!r}")


def _with_database(dsn: str, database: str) -> str:
    """Return `dsn` repointed at `database`, keeping every other parameter."""
    return urlunsplit(urlsplit(dsn)._replace(path=f"/{database}"))


def _callee_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


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
        # libpq takes everything after the first `/` as the name, so a
        # trailing slash is part of it. The old hand parser stripped it,
        # which keyed a (legal) database named `db/` as `db` — a quiet
        # disagreement with the database psycopg would actually open.
        ("postgresql://u:p@h:5432/db/", "db/"),
        ("postgresql://h/a/b", "a/b"),
    ],
)
def test_database_name_is_read_off_the_dsn(dsn: str, expected: str) -> None:
    """The key is per-database, so the name has to come out of the DSN."""
    assert database_name(dsn) == expected


def test_a_dsn_naming_no_database_is_refused_rather_than_keyed_as_blank() -> None:
    """A blank name would key every such DSN to one lock, serialising
    unrelated databases against each other — worse than not locking."""
    with pytest.raises(ValueError, match="names no database"):
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
    def one_run() -> str:
        # Not `check=True`: that raises CalledProcessError with the child's
        # stderr swallowed, so an import failure on CI is opaque.
        proc = subprocess.run(
            [sys.executable, "-c", source],
            cwd=pathlib.Path(__file__).resolve().parent.parent,
            capture_output=True, text=True,
        )
        assert proc.returncode == 0, f"subprocess failed:\n{proc.stderr}"
        return proc.stdout.strip()

    runs = {one_run() for _ in range(2)}
    assert len(runs) == 1, f"key differs between processes: {runs}"
    assert runs != {""}
    assert next(iter(runs)) == str(advisory_lock_key("localmail_test"))


def test_different_databases_get_different_keys() -> None:
    """Otherwise a run against a scratch database blocks on the shared one."""
    assert advisory_lock_key("localmail_test") != advisory_lock_key("localmail_other")


# --------------------------------------------------------------------------
# Pure: busy_message
# --------------------------------------------------------------------------


def test_the_busy_message_names_the_database_and_the_remedy() -> None:
    """The whole point is that the operator can act on it: the failure is
    another session, not a broken test, and waiting is the fix."""
    msg = busy_message("localmail_test", timeout_s=30.0)
    assert "localmail_test" in msg
    assert "waited 30s" in msg  # not a bare "30", which any incidental digit pair satisfies
    assert "LOCALMAIL_TEST_DSN" in msg


# --------------------------------------------------------------------------
# Behavioural: the lock actually excludes
# --------------------------------------------------------------------------


@pytest.fixture(scope="session")
def lock_probe_dsn(db_dsn: str) -> Iterator[str]:
    """A DSN naming a scratch database this session owns exclusively.

    The live pytest session holds the lock on its own test database for the
    whole run — that is the fix — so a test cannot acquire that key to prove
    anything about it. It needs a second database.

    That second database must be **unique to this session**, not a shared
    `postgres`. Postgres scopes advisory locks per database (`pg_locks` keys
    them by database OID), so a fixed probe database means every concurrent
    suite contends on one key — breaking, in the test file for the
    concurrency guard, the very escape hatch the guard documents ("point
    LOCALMAIL_TEST_DSN at your own database"). With a shared probe, two
    suites on two databases fail six tests apiece.

    `CREATE DATABASE` needs only `CREATEDB`, which the test role has; it
    notably does *not* need the superuser that `CREATE EXTENSION vector`
    would, because nothing here runs migrations against it.
    """
    admin = _with_database(db_dsn, "postgres")
    name = f"localmail_locktest_{os.getpid()}"
    try:
        conn = psycopg.connect(admin, autocommit=True, connect_timeout=5)
    except psycopg.OperationalError as exc:
        pytest.skip(f"no maintenance database to create a lock probe in: {exc}")
    with conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
        conn.execute(f'CREATE DATABASE "{name}"')
    try:
        yield _with_database(db_dsn, name)
    finally:
        with psycopg.connect(admin, autocommit=True, connect_timeout=5) as cleanup:
            cleanup.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


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
    """Closing releases, so the next session is not wedged.

    This is the *graceful* half of that property; the backend-death half —
    which is what actually makes a SIGKILLed run safe, and is also how the
    guard can be lost mid-run — is pinned by
    `test_a_lock_lost_to_a_dead_backend_is_detected`.
    """
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


# --------------------------------------------------------------------------
# Pure: database_name against the forms libpq actually accepts
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("dsn", "expected"),
    [
        # libpq keyword/value form. `psycopg.connect` takes it, so
        # LOCALMAIL_TEST_DSN can carry it; `urlsplit` returns the whole
        # string as the "path" and reports no scheme, so a hand-rolled
        # parser digests the connection string instead of the database.
        ("host=localhost port=5532 dbname=localmail_test user=u", "localmail_test"),
        # Same database, different spelling. These MUST agree or two
        # sessions derive different keys, both acquire, and the guard
        # excludes nothing — silently.
        ("dbname=localmail_test host=localhost port=5532 user=u", "localmail_test"),
        # libpq honours a `dbname` query parameter over the URI path.
        ("postgresql://h:5432/ignored?dbname=localmail_test", "localmail_test"),
        # Percent-encoding is the URI's, not the database's.
        ("postgresql://h/localmail%5Ftest", "localmail_test"),
    ],
)
def test_database_name_matches_what_libpq_would_connect_to(dsn: str, expected: str) -> None:
    """The key must name the database psycopg will actually open.

    Any divergence between this and libpq's own resolution is the failure
    `advisory_lock_key`'s docstring exists to prevent, arriving one function
    earlier: two DSNs naming one database yield two keys, and the guard
    excludes nothing without erroring.
    """
    assert database_name(dsn) == expected


def test_a_password_in_the_dsn_never_reaches_the_lock_key_or_the_messages() -> None:
    """`busy_message`/`waiting_message` interpolate the parsed name, and a
    contended run prints them to the terminal and into CI logs. Returning an
    unparsed connection string would put the password in both."""
    dsn = "host=localhost port=5532 dbname=localmail_test user=u password=s3cr3t"
    name = database_name(dsn)
    assert name == "localmail_test"
    assert "s3cr3t" not in name
    assert "s3cr3t" not in busy_message(name, timeout_s=1.0)
    assert "s3cr3t" not in waiting_message(name)


# --------------------------------------------------------------------------
# The key width is name-dependent, so the pin must be too
# --------------------------------------------------------------------------


def test_the_key_is_signed_so_high_digests_do_not_overflow_bigint() -> None:
    """`signed=False` survives every name this file otherwise uses: all of
    them happen to digest to a clear top bit. A name on the other side of
    that coin is what makes the `signed=True` argument load-bearing, and
    `pg_advisory_lock(bigint)` rejects the unsigned value at session start.
    """
    negatives = [n for n in _KEY_WIDTH_PROBE_NAMES if advisory_lock_key(n) < 0]
    assert negatives, (
        "no probe name digests with the top bit set, so this test cannot "
        "distinguish signed from unsigned; add one"
    )
    for name in _KEY_WIDTH_PROBE_NAMES:
        assert -(2**63) <= advisory_lock_key(name) < 2**63, name


#: Names spanning both signs of the digest, so the `signed=True` argument is
#: actually exercised. Verified to contain at least one of each by the test
#: above, which fails rather than silently degrading if that stops holding.
_KEY_WIDTH_PROBE_NAMES = [
    "localmail_test", "localmail_test_2", "localmail_test_3",
    "postgres", "a", "x" * 63, "UPPER_case",
]


# --------------------------------------------------------------------------
# waiting_message content, pinned directly rather than via the probe's name
# --------------------------------------------------------------------------


def test_the_waiting_message_names_the_database_and_the_remedy() -> None:
    """The behavioural test asserts `database_name(probe) in seen[0]`, and the
    probe database used to be literally named `postgres` — so the whole body
    could be replaced by a literal and stay green. Pin the content here."""
    msg = waiting_message("localmail_test")
    assert "localmail_test" in msg
    assert "LOCALMAIL_TEST_DSN" in msg
    assert "wait" in msg.lower()


# --------------------------------------------------------------------------
# The timeout knob is documented as overridable, so overriding it must work
# --------------------------------------------------------------------------


def test_the_literal_default_timeout_is_long_enough_for_a_full_suite() -> None:
    """Asserted against the literal, not the resolved value: the module
    documents LOCALMAIL_TEST_DB_LOCK_TIMEOUT_S as overridable, and an
    operator lowering it to fail fast must not turn this test red."""
    assert DEFAULT_LOCK_TIMEOUT_S >= 300


def test_the_documented_override_is_honoured() -> None:
    assert resolve_lock_timeout_s({"LOCALMAIL_TEST_DB_LOCK_TIMEOUT_S": "42"}) == 42.0
    assert resolve_lock_timeout_s({}) == DEFAULT_LOCK_TIMEOUT_S


def test_a_malformed_override_names_the_variable_instead_of_killing_collection() -> None:
    """`float(os.environ[...])` at import raises a bare ValueError from
    conftest's import, which is a collection error for the whole suite —
    including the ~2000 tests that never touch a database."""
    with pytest.raises(ValueError, match="LOCALMAIL_TEST_DB_LOCK_TIMEOUT_S"):
        resolve_lock_timeout_s({"LOCALMAIL_TEST_DB_LOCK_TIMEOUT_S": "abc"})


# --------------------------------------------------------------------------
# The lock can be lost mid-run, and losing it must not be silent
# --------------------------------------------------------------------------


def test_a_held_lock_verifies_as_held(lock_probe_dsn) -> None:
    """Positive control: a guard that always raised would pass the two below."""
    holder = acquire_exclusive(lock_probe_dsn, timeout_s=5.0)
    try:
        verify_still_held(holder)
    finally:
        holder.close()


def test_a_lock_lost_to_a_dead_backend_is_detected(lock_probe_dsn) -> None:
    """The holder is the most idle connection in the suite — held open for the
    whole run with no traffic — so it is the one most likely to be reaped by a
    Postgres restart, an `idle_session_timeout`, or a dropped TCP flow. The
    advisory lock dies with its backend (the property that makes a SIGKILLed
    run safe), a second session then acquires freely, and `holder.closed`
    still reads False, so nothing anywhere notices.
    """
    holder = acquire_exclusive(lock_probe_dsn, timeout_s=5.0)
    try:
        with psycopg.connect(lock_probe_dsn, autocommit=True) as killer:
            pid = holder.execute("SELECT pg_backend_pid()").fetchone()
            assert pid is not None
            killer.execute("SELECT pg_terminate_backend(%s)", (pid[0],))
        assert not holder.closed, "psycopg does not notice; that is the point"
        with pytest.raises(SessionLockLost, match="no longer held"):
            verify_still_held(holder)
    finally:
        holder.close()


def test_a_lock_released_under_a_live_connection_is_detected(lock_probe_dsn) -> None:
    """A connection can survive while the lock does not — a pooler issuing
    DISCARD ALL / pg_advisory_unlock_all on reset does exactly this, and a
    liveness-only check would report the guard healthy."""
    holder = acquire_exclusive(lock_probe_dsn, timeout_s=5.0)
    try:
        holder.execute("SELECT pg_advisory_unlock_all()")
        with pytest.raises(SessionLockLost, match="no longer held"):
            verify_still_held(holder)
    finally:
        holder.close()


def test_the_truncate_is_guarded_by_the_lock_check(_conftest_source) -> None:
    """`db_conn`'s TRUNCATE is the destructive act, so the check belongs
    ahead of it — not merely somewhere in the fixture. Read structurally,
    because the reason is written in comments beside it and prose is not
    code (the lesson #291 already paid for).
    """
    fn = _function_named(_conftest_source, "db_conn")
    assert "db_session_lock" in [a.arg for a in fn.args.args], (
        "db_conn cannot check a lock it does not receive"
    )
    calls = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Call) and _callee_name(n) == "verify_still_held"
    ]
    assert calls, "db_conn does not verify the session lock before truncating"
    truncates = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and "TRUNCATE" in n.value
    ]
    assert truncates, "db_conn no longer truncates; this pin needs rethinking"
    assert min(c.lineno for c in calls) < min(t.lineno for t in truncates), (
        "the lock check must run BEFORE the truncate, not after it"
    )


def test_the_lock_is_taken_before_migrations_run(_conftest_source) -> None:
    """CLAUDE.md and the fixture docstring both make this load-bearing — two
    sessions must not race the migration runner.

    Asserted on the fixture *graph*, not on statement order: `db_dsn`
    requesting `db_session_lock` is what makes "lock first" true by
    construction, and pytest cannot satisfy it any other way. A pin on line
    order inside one function would be undone by any refactor that split
    them, while still reading as though it covered this.
    """
    lock_fn = _function_named(_conftest_source, "db_session_lock")
    assert any(
        isinstance(n, ast.Call) and _callee_name(n) == "acquire_exclusive"
        for n in ast.walk(lock_fn)
    ), "db_session_lock does not acquire the lock"

    dsn_fn = _function_named(_conftest_source, "db_dsn")
    assert "db_session_lock" in [a.arg for a in dsn_fn.args.args], (
        "db_dsn does not depend on db_session_lock, so migrations can run "
        "before the lock is held"
    )
    assert any(
        isinstance(n, ast.Call) and _callee_name(n) == "apply_migrations"
        for n in ast.walk(dsn_fn)
    ), "db_dsn no longer applies migrations; this pin needs rethinking"


# --------------------------------------------------------------------------
# The announcement is what makes a long wait legible; pin its delivery
# --------------------------------------------------------------------------


def test_the_waiting_notice_is_emitted_once_not_once_per_poll(lock_probe_dsn) -> None:
    """Without the `announced` guard this is one yellow line per second for
    up to the full timeout. `assert seen` alone is satisfied by both."""
    seen: list[str] = []
    holder = acquire_exclusive(lock_probe_dsn, timeout_s=5.0)
    try:
        with pytest.raises(DatabaseSessionBusy):
            acquire_exclusive(
                lock_probe_dsn, timeout_s=0.5, poll_interval_s=0.01, on_wait=seen.append,
            )
    finally:
        holder.close()
    assert len(seen) == 1, f"announced {len(seen)} times"


def test_the_connection_is_released_when_the_wait_gives_up(lock_probe_dsn) -> None:
    """The timeout path leaks a backend for the rest of the run without the
    `except BaseException: conn.close()` cleanup, and nothing else notices."""
    holder = acquire_exclusive(lock_probe_dsn, timeout_s=5.0)
    opened: list[psycopg.Connection] = []
    real_connect = psycopg.connect

    def spy(*a, **kw):
        conn = real_connect(*a, **kw)
        opened.append(conn)
        return conn

    try:
        with mock.patch.object(psycopg, "connect", spy):
            with pytest.raises(DatabaseSessionBusy):
                acquire_exclusive(lock_probe_dsn, timeout_s=0.2, poll_interval_s=0.05)
    finally:
        holder.close()
    assert opened, "the spy saw no connection"
    assert all(c.closed for c in opened), "the give-up path leaked a connection"


def test_the_uncontended_path_never_sleeps(lock_probe_dsn) -> None:
    """Replaces a wall-clock bound with the injected seam, so the property is
    asserted directly instead of inferred from elapsed time."""
    def fail_on_sleep(_seconds: float) -> None:
        pytest.fail("slept on an uncontended acquire")

    conn = acquire_exclusive(lock_probe_dsn, timeout_s=5.0, sleep=fail_on_sleep)
    conn.close()


def test_announce_reaches_the_operator_past_pytest_s_output_capture() -> None:
    """Fixture-setup output is captured, so the notice has to go through the
    terminal reporter. A `print` here is invisible for exactly as long as the
    wait lasts — the window where silence reads as a hung run."""
    written: list[tuple[str, dict]] = []
    reporter = mock.Mock()
    reporter.write_line.side_effect = lambda msg, **kw: written.append((msg, kw))
    request = mock.Mock()
    request.config.pluginmanager.get_plugin.return_value = reporter

    _announce(request, "held by someone else")

    request.config.pluginmanager.get_plugin.assert_called_once_with("terminalreporter")
    assert written and "held by someone else" in written[0][0]


def test_announce_falls_back_to_stderr_when_there_is_no_reporter(capsys) -> None:
    """`-p no:terminal` removes the plugin; the message must still be tried."""
    request = mock.Mock()
    request.config.pluginmanager.get_plugin.return_value = None

    _announce(request, "held by someone else")

    assert "held by someone else" in capsys.readouterr().err
