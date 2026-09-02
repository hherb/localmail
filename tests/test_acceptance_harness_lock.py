# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The acceptance harnesses take the test-database session lock (#337).

#336 serialised pytest sessions against one test database. It covered
*pytest*, not the *database*: the standalone harnesses under
``tests/acceptance/`` truncate the same tables against the same
``LOCALMAIL_TEST_DSN`` and took no lock, so running one beside a suite
reproduced #329's corruption in both directions and with the same silence.

Two halves are tested here, because either alone has a hole. The context
manager is what takes the lock; the AST rule is what keeps a harness added
next year from forgetting to call it — the harnesses are not collected by
pytest (they match no ``python_files`` pattern), so no conftest fixture can
arm them and nothing else would notice.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests._db_session_lock import (
    DatabaseSessionBusy,
    SessionLockLost,
    verify_still_held,
)
from tests.acceptance._harness_lock import (
    BUSY_EXIT_CODE,
    COVERED_LIBRARIES,
    DB_ENTRY_CALLS,
    HARNESS_LOCK_MODULE,
    LOCK_HELPER_NAME,
    acceptance_coverage_error,
    checkpoint,
    harness_db_lock,
    harness_entry_points,
    harness_lock_error,
)

ACCEPTANCE_DIR = Path(__file__).parent / "acceptance"


def _noop_verify(conn: object) -> None:
    """Stand in for `verify_still_held`, which needs a real backend.

    Passed explicitly at every call site rather than defaulted, so the
    production default stays pinned by
    `test_the_exit_check_defaults_to_the_shared_verify`.
    """


class _FakeConn:
    """Stands in for the connection `acquire_exclusive` hands back."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _acquire_returning(conn: _FakeConn):
    def acquire(dsn: str, **kwargs):
        return conn

    return acquire


# --------------------------------------------------------------------------
# The context manager: it must hold the lock for the run and always release.
# --------------------------------------------------------------------------


def test_the_lock_connection_is_yielded_to_the_harness() -> None:
    conn = _FakeConn()
    with harness_db_lock(
        "postgresql:///x", acquire=_acquire_returning(conn), verify=_noop_verify
    ) as held:
        assert held is conn


def test_the_lock_is_released_when_the_harness_finishes() -> None:
    conn = _FakeConn()
    with harness_db_lock(
        "postgresql:///x", acquire=_acquire_returning(conn), verify=_noop_verify
    ):
        assert not conn.closed
    assert conn.closed


def test_the_lock_is_released_when_the_harness_raises() -> None:
    """A harness that dies mid-run must not wedge the next one.

    The lock dies with its backend eventually, but "eventually" is whenever
    the OS reaps the socket; a harness raising is an ordinary outcome (a
    missing embedding model, an unreachable DSN) and must release promptly.
    """
    conn = _FakeConn()
    with pytest.raises(ZeroDivisionError):
        with harness_db_lock(
            "postgresql:///x", acquire=_acquire_returning(conn), verify=_noop_verify
        ):
            raise ZeroDivisionError("harness blew up")
    assert conn.closed


def test_a_contended_database_exits_with_the_message_and_no_traceback() -> None:
    """`DatabaseSessionBusy` is a wait, not a fault — it gets one line.

    The conftest call, one layer over: a traceback out of a harness reads as
    a crash in the harness, when what happened is that someone else is
    running the suite.
    """

    said: list[str] = []

    def acquire(dsn: str, **kwargs):
        raise DatabaseSessionBusy("another test run is using 'localmail_test'")

    with pytest.raises(SystemExit) as caught:
        with harness_db_lock(
            "postgresql:///x", acquire=acquire, announce=said.append, verify=_noop_verify
        ):
            pytest.fail("the body must not run when the lock was refused")

    # The code, not `!= 0`: `SystemExit("some message")` exits **1** and puts
    # the string on stderr, so a `!= 0` assertion is satisfied by a payload
    # that is not an exit code at all. That is how the first version of this
    # shipped, and the end-to-end test below is what caught it.
    assert caught.value.code == BUSY_EXIT_CODE
    assert said == ["another test run is using 'localmail_test'"]


def test_the_wait_notice_is_announced_to_the_caller() -> None:
    """A harness blocked for minutes in silence reads as a hang."""
    said: list[str] = []
    conn = _FakeConn()

    def acquire(dsn: str, *, on_wait=None, **kwargs):
        assert on_wait is not None, "the harness must be able to report a wait"
        on_wait("waiting for another test run")
        return conn

    with harness_db_lock(
        "postgresql:///x", acquire=acquire, announce=said.append, verify=_noop_verify
    ):
        pass

    assert said == ["waiting for another test run"]


# --------------------------------------------------------------------------
# The AST rule: every harness entry point must actually take the lock.
# --------------------------------------------------------------------------


_COMPLIANT = """
from tests.acceptance._harness_lock import harness_db_lock
from localmail.db import apply_migrations

def main() -> int:
    dsn = "postgresql:///x"
    with harness_db_lock(dsn):
        apply_migrations(dsn)
        return 0
"""

_NO_LOCK_AT_ALL = """
from localmail.db import apply_migrations

def main() -> int:
    dsn = "postgresql:///x"
    apply_migrations(dsn)
    return 0
"""

_MIGRATES_BEFORE_TAKING_THE_LOCK = """
from tests.acceptance._harness_lock import harness_db_lock
from localmail.db import apply_migrations

def main() -> int:
    dsn = "postgresql:///x"
    apply_migrations(dsn)
    with harness_db_lock(dsn):
        return 0
"""

#: The rationale for this rule necessarily names the helper, in prose, in
#: every harness that follows it. A text scan would read that as compliance —
#: the `_mentions_version_option` lesson (#291), which this module inherits
#: rather than rediscovers.
_ONLY_TALKS_ABOUT_THE_LOCK = '''
"""This harness should call harness_db_lock before apply_migrations."""
from localmail.db import apply_migrations

def main() -> int:
    # harness_db_lock(dsn) belongs here
    apply_migrations("postgresql:///x")
    return 0
'''


_LOCKS_WITH_THE_WRONG_CONTEXT_MANAGER = """
import psycopg
from localmail.db import apply_migrations

def main() -> int:
    dsn = "postgresql:///x"
    with psycopg.connect(dsn) as conn:
        apply_migrations(dsn)
        return 0
"""


_LOCKS_WITH_A_BARE_NAME_CONTEXT_MANAGER = """
from contextlib import ExitStack
from localmail.db import apply_migrations

def main() -> int:
    dsn = "postgresql:///x"
    with ExitStack() as stack:
        apply_migrations(dsn)
        return 0
"""


def test_a_harness_that_wraps_its_db_work_in_the_lock_passes() -> None:
    assert harness_lock_error(_COMPLIANT, "run_ok.py") is None


def test_a_harness_that_never_takes_the_lock_is_reported() -> None:
    problem = harness_lock_error(_NO_LOCK_AT_ALL, "run_bad.py")
    assert problem is not None
    assert "run_bad.py" in problem
    assert LOCK_HELPER_NAME in problem


def test_a_harness_that_migrates_before_locking_is_reported() -> None:
    """Taking the lock late is taking no lock: the truncate has already run."""
    problem = harness_lock_error(_MIGRATES_BEFORE_TAKING_THE_LOCK, "run_late.py")
    assert problem is not None
    # Anchored on the list segment, not a bare substring: the remedy sentence
    # says "from apply_migrations onward", so `"apply_migrations" in problem`
    # is satisfied by boilerplate whatever the rule decided. A mutation proved
    # it — see the sibling test below.
    assert "session lock: apply_migrations" in problem


def test_prose_naming_the_helper_does_not_count_as_taking_the_lock() -> None:
    problem = harness_lock_error(_ONLY_TALKS_ABOUT_THE_LOCK, "run_prose.py")
    assert problem is not None


def test_some_other_context_manager_does_not_count_as_taking_the_lock() -> None:
    """The `with` must be the lock's, not merely *a* `with`.

    Every harness already wraps its work in `with psycopg.connect(dsn)`, so
    a rule that accepted any context manager would pass all five while
    locking none of them. `test_prose_naming_the_helper…` above cannot
    catch that: its fixture has no `with` at all, so `_is_lock_call` is
    never reached and it passes whatever that function returns — which a
    mutation proved (forcing `_is_lock_call` to True left the whole file
    green until this test existed).
    """
    problem = harness_lock_error(_LOCKS_WITH_THE_WRONG_CONTEXT_MANAGER, "run_wrong.py")
    assert problem is not None
    # `connect` is uncovered here under ANY rule — it is the `with`'s own
    # context expression, never inside its body — so the mutation still
    # produced a message, and the boilerplate remedy still contained the
    # word. Only the list segment separates the two verdicts.
    assert "session lock: apply_migrations" in problem


def test_a_bare_name_context_manager_does_not_count_as_taking_the_lock() -> None:
    """The `ast.Name` branch needs its own case.

    The sibling above wraps in `psycopg.connect(...)`, which is an
    `ast.Attribute`, so it exercises only half of `_is_lock_call`. A
    mutation forcing the Name branch to True left every other test green —
    and a harness reaching for `ExitStack`, or for a local `connection(dsn)`
    helper, is entirely plausible, so this is not a contrived half.
    """
    problem = harness_lock_error(_LOCKS_WITH_A_BARE_NAME_CONTEXT_MANAGER, "run_stack.py")
    assert problem is not None
    assert "session lock: apply_migrations" in problem


def test_a_source_that_defines_no_main_is_reported_rather_than_passed() -> None:
    """Silence on an unrecognised shape would be the guard going inert."""
    problem = harness_lock_error("x = 1\n", "run_shapeless.py")
    assert problem is not None
    assert "main" in problem


# --------------------------------------------------------------------------
# Coverage: derived from the filesystem, so a sixth harness is in scope
# without anyone remembering to update a list.
# --------------------------------------------------------------------------


def test_every_harness_entry_point_takes_the_lock() -> None:
    entry_points = harness_entry_points(ACCEPTANCE_DIR)
    assert entry_points, "no harness entry points found — the glob is wrong"
    problems = [
        problem
        for path in entry_points
        if (problem := harness_lock_error(path.read_text(), path.name)) is not None
    ]
    assert problems == []


def test_the_entry_point_glob_finds_the_harnesses_and_not_their_library() -> None:
    """`browse_explain_lib.py` touches the DB but is imported, never run.

    Its work happens inside `run_browse_explain.main`, so it is covered by
    that harness's lock; holding the rule to entry points is what keeps the
    message honest about who must call the helper.
    """
    names = {p.name for p in harness_entry_points(ACCEPTANCE_DIR)}
    assert "run_browse_explain.py" in names
    assert "browse_explain_lib.py" not in names
    assert "__init__.py" not in names


def test_the_db_entry_calls_are_the_ones_the_harnesses_actually_use() -> None:
    """Every entry point is a DB harness, so the rule belongs on it.

    Note what this does **not** prove: it asserts a non-empty intersection,
    which `apply_migrations` alone satisfies for all five, so it cannot
    detect a name dropped from the set. It used to claim otherwise, and the
    comment on `DB_ENTRY_CALLS` cited it for that. The dropped-name property
    is `test_every_db_entry_call_name_is_reached_by_some_harness`.
    """
    for path in harness_entry_points(ACCEPTANCE_DIR):
        called = {
            node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
            for node in ast.walk(ast.parse(path.read_text()))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute | ast.Name)
        }
        assert called & DB_ENTRY_CALLS, f"{path.name} names no DB entry call"


# --------------------------------------------------------------------------
# End to end: a real harness, started the way an operator starts one, against
# a database this very pytest session is holding.
# --------------------------------------------------------------------------

#: Needs only `--dsn`. argparse runs *before* the lock, so a harness with a
#: required argument (`run_recall_eval.py --queries`) exits 2 without ever
#: reaching `harness_db_lock`, and the test would be exercising argparse
#: rather than the wiring it is here to prove. (An earlier note claimed such
#: a harness would *pass* the test for the wrong reason; it fails it — the
#: assertion below demands BUSY_EXIT_CODE, not merely non-zero. The wording
#: was left over from a draft that asserted `!= 0`.)
_NO_REQUIRED_ARGS_HARNESS = "run_browse_explain.py"

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_a_harness_started_beside_this_suite_refuses_the_database(
    db_session_lock, db_dsn: str
) -> None:
    """#337 itself, reproduced: this session holds the lock, the harness waits.

    Requesting `db_session_lock` is what makes the precondition real rather
    than assumed — it is the fixture holding `localmail_test` for the length
    of this run, i.e. exactly the "suite already in flight" the issue is
    about.

    The DSN comes from `db_dsn`, **not** from `db_session_lock.info.dsn`.
    libpq's `ConnectionInfo.dsn` is a *report*, not a round-trippable
    connection string: it omits the password. Locally that is invisible
    wherever `pg_hba.conf` does not demand one, so this test passed on macOS
    while failing on both CI legs with `fe_sendauth: no password supplied` —
    the harness died in a traceback instead of being refused, which is a
    different outcome that happens to be non-zero. Never reconstruct a DSN
    from a live connection.

    The subprocess is given a one-second budget so the test costs a second
    rather than the ten-minute default; the point being proved is that it
    refuses at all, not how long it is willing to wait.

    `--total-rows 1` bounds the counterfactual. This spawns a fully
    destructive harness against the database the suite is using, and its
    safety rests entirely on the lock still being held — the one property
    `_db_session_lock` says must be re-checked rather than assumed. So it is
    re-checked, immediately before the spawn; and if the check should ever
    be wrong, the harness seeds one row instead of the 100,000 its default
    would.

    Every other test here injects a fake `acquire`, so this is the only one
    that proves the wiring: that the import resolves on the harness's own
    sys.path and that the refusal survives as a process exit code. It does
    **not** pin that `--dsn` is read — the env passes `LOCALMAIL_TEST_DSN`
    through, so the harness resolves the same database from its own default
    either way.
    """
    verify_still_held(db_session_lock)
    env = {
        **os.environ,
        "PYTHONPATH": f"src{os.pathsep}.",
        "LOCALMAIL_TEST_DB_LOCK_TIMEOUT_S": "1",
    }
    proc = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "tests" / "acceptance" / _NO_REQUIRED_ARGS_HARNESS),
            "--dsn",
            db_dsn,
            "--total-rows",
            "1",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert proc.returncode == BUSY_EXIT_CODE, (
        f"expected the busy exit code; got {proc.returncode}\n"
        f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    )
    assert "another test run is using" in proc.stderr
    # Not cosmetic: a harness that cannot *connect* also exits non-zero, so
    # without this the test would pass on a broken DSN. That is precisely how
    # it failed on CI while passing locally.
    assert "Traceback" not in proc.stderr


# --------------------------------------------------------------------------
# The exit status is a value, not a self-comparison.
# --------------------------------------------------------------------------


def test_the_busy_exit_code_is_a_status_no_other_outcome_uses() -> None:
    """The two behavioural assertions above cannot pin this, and did not.

    Both of them compare the observed status against `BUSY_EXIT_CODE`
    itself, so both sides move together and the constant could be set to
    anything — including `0`, at which point a harness refused the database
    reports success and a shell loop carries on. Mutation-proven: with
    `BUSY_EXIT_CODE = 0` the whole file stayed green while the real harness
    subprocess exited 0 on a contended database.

    That is the `!= 0` trap this module already paid for once, wearing a
    self-referential hat, so the value is asserted against literals here —
    the `DEFAULT_LOCK_TIMEOUT_S` arrangement in `_db_session_lock.py`, and
    for the same reason.

    The excluded values are the ones a harness reaches for other reasons:
    `0` success, `1` an eval failing its own acceptance gates, `2` argparse.
    """
    assert BUSY_EXIT_CODE == 3
    assert BUSY_EXIT_CODE != 0, "a refused database must not report success"
    assert BUSY_EXIT_CODE != 1, "1 is an eval failing its gates"
    assert BUSY_EXIT_CODE != 2, "2 is argparse rejecting the command line"


# --------------------------------------------------------------------------
# The lock can lapse mid-run, so it is re-checked rather than trusted.
# --------------------------------------------------------------------------


def test_a_lock_lost_during_the_run_is_reported_at_exit() -> None:
    """A harness holds this lock across the longest work in the tree.

    `_db_session_lock` documents that the lock rides the most idle
    connection in the run and dies silently with its backend — a restart, an
    `idle_session_timeout`, a reaped TCP flow — while `conn.closed` still
    reads False. pytest re-checks before every TRUNCATE; a harness has no
    per-test seam to hang that on, so the check runs on the way out. It
    cannot undo a truncate that already raced, but it turns "the numbers are
    quietly wrong" into a failed run, which is the whole difference.
    """
    conn = _FakeConn()

    def verify(_conn: object) -> None:
        raise SessionLockLost("the lock lapsed mid-run")

    with pytest.raises(SessionLockLost):
        with harness_db_lock(
            "postgresql:///x", acquire=_acquire_returning(conn), verify=verify
        ):
            pass
    assert conn.closed, "the connection is released even when the check fails"


def test_the_exit_check_does_not_mask_the_harness_s_own_failure() -> None:
    """A harness that raises must surface *its* exception, not the check's.

    The check runs after the body returns normally, so a body that raised
    never reaches it — otherwise a lapsed lock would rewrite every harness
    crash into `SessionLockLost` and hide the real cause.
    """
    conn = _FakeConn()

    def verify(_conn: object) -> None:
        raise SessionLockLost("must not be raised")

    with pytest.raises(ZeroDivisionError):
        with harness_db_lock(
            "postgresql:///x", acquire=_acquire_returning(conn), verify=verify
        ):
            raise ZeroDivisionError("the real failure")
    assert conn.closed


def test_the_exit_check_defaults_to_the_shared_verify() -> None:
    """Injected everywhere in these tests, so the default needs its own pin.

    Without it, `verify` could default to a no-op and every test above would
    still pass — the guard present in the signature and absent in effect.
    """
    import inspect

    default = inspect.signature(harness_db_lock).parameters["verify"].default
    assert default is verify_still_held


def test_checkpoint_delegates_to_the_shared_verify() -> None:
    """`checkpoint` is what a harness calls before a *late* truncate.

    The exit check bounds the report; this bounds the damage. It must be the
    same rule `db_conn` applies, not a second one that could drift.
    """
    seen: list[object] = []
    conn = _FakeConn()
    checkpoint(conn, verify=seen.append)
    assert seen == [conn]

    import inspect

    default = inspect.signature(checkpoint).parameters["verify"].default
    assert default is verify_still_held


# --------------------------------------------------------------------------
# The rule follows `main` into the helpers it calls.
# --------------------------------------------------------------------------


#: The shape `run_chunk_insert_bench.py` already has: `main` takes the lock
#: and the DB work lives in a helper. The rule used to walk `main` only, so
#: hoisting that helper's call out of the `with` left every TRUNCATE
#: unlocked and the rule silent — #337 admitted by the guard written to end
#: it. Verified against the real file before the fix.
_HELPER_DOES_THE_DB_WORK_OUTSIDE_THE_LOCK = """
import psycopg
from tests.acceptance._harness_lock import harness_db_lock
from localmail.db import apply_migrations

def _run_mode(dsn):
    with psycopg.connect(dsn) as conn:
        conn.execute("TRUNCATE messages")

def main() -> int:
    dsn = "postgresql:///x"
    results = _run_mode(dsn)
    with harness_db_lock(dsn):
        apply_migrations(dsn)
    return 0
"""

_HELPER_DOES_THE_DB_WORK_INSIDE_THE_LOCK = """
import psycopg
from tests.acceptance._harness_lock import harness_db_lock
from localmail.db import apply_migrations

def _run_mode(dsn):
    with psycopg.connect(dsn) as conn:
        conn.execute("TRUNCATE messages")

def main() -> int:
    dsn = "postgresql:///x"
    with harness_db_lock(dsn):
        apply_migrations(dsn)
        _run_mode(dsn)
    return 0
"""

#: Two hops, because one is an arbitrary depth to stop at and the harnesses
#: already nest (`_seed_and_embed_multilingual` calls further helpers).
_HELPER_CALLS_ANOTHER_HELPER = """
from tests.acceptance._harness_lock import harness_db_lock
from localmail.db import apply_migrations, open_pool

def _inner(dsn):
    return open_pool(dsn)

def _outer(dsn):
    return _inner(dsn)

def main() -> int:
    dsn = "postgresql:///x"
    pool = _outer(dsn)
    with harness_db_lock(dsn):
        apply_migrations(dsn)
    return 0
"""

#: A helper that takes the lock itself is compliant — the rule is about
#: position, not about which function the `with` is written in.
_HELPER_TAKES_THE_LOCK_ITSELF = """
from tests.acceptance._harness_lock import harness_db_lock
from localmail.db import apply_migrations

def _run(dsn):
    with harness_db_lock(dsn):
        apply_migrations(dsn)

def main() -> int:
    return _run("postgresql:///x")
"""

#: Mutual recursion: the walk must terminate rather than hang the suite.
_HELPERS_CALL_EACH_OTHER = """
from tests.acceptance._harness_lock import harness_db_lock
from localmail.db import apply_migrations

def _a(dsn):
    return _b(dsn)

def _b(dsn):
    apply_migrations(dsn)
    return _a(dsn)

def main() -> int:
    return _a("postgresql:///x")
"""

#: Module-level DB work runs at *import*, so no `with` inside `main` can
#: ever cover it — it is unlocked by construction.
_DB_WORK_AT_MODULE_LEVEL = """
from tests.acceptance._harness_lock import harness_db_lock
from localmail.db import apply_migrations

apply_migrations("postgresql:///x")

def main() -> int:
    with harness_db_lock("postgresql:///x"):
        return 0
"""

#: Python binds the *last* definition, so a rule reading the first inspects
#: a `main` that will never run.
_TWO_MAINS_THE_LAST_UNLOCKED = """
from tests.acceptance._harness_lock import harness_db_lock
from localmail.db import apply_migrations

def main() -> int:
    with harness_db_lock("postgresql:///x"):
        apply_migrations("postgresql:///x")
        return 0

def main() -> int:
    apply_migrations("postgresql:///x")
    return 0
"""

#: A locally defined `harness_db_lock` satisfies a rule that matches the
#: *name*. Nothing about it takes a lock.
_SHADOWS_THE_LOCK_HELPER = """
from contextlib import contextmanager
from localmail.db import apply_migrations

@contextmanager
def harness_db_lock(dsn):
    yield None

def main() -> int:
    with harness_db_lock("postgresql:///x"):
        apply_migrations("postgresql:///x")
        return 0
"""


def test_db_work_in_a_helper_outside_the_lock_is_reported() -> None:
    problem = harness_lock_error(
        _HELPER_DOES_THE_DB_WORK_OUTSIDE_THE_LOCK, "run_helper.py"
    )
    assert problem is not None
    assert "session lock: connect" in problem


def test_db_work_in_a_helper_inside_the_lock_passes() -> None:
    """The positive control: without it, a rule that reports every helper
    call would pass the test above and fail all five real harnesses."""
    assert (
        harness_lock_error(_HELPER_DOES_THE_DB_WORK_INSIDE_THE_LOCK, "run_ok.py")
        is None
    )


def test_the_walk_follows_more_than_one_hop() -> None:
    problem = harness_lock_error(_HELPER_CALLS_ANOTHER_HELPER, "run_deep.py")
    assert problem is not None
    assert "session lock: open_pool" in problem


def test_a_helper_that_takes_the_lock_itself_passes() -> None:
    assert harness_lock_error(_HELPER_TAKES_THE_LOCK_ITSELF, "run_inner.py") is None


def test_mutually_recursive_helpers_terminate_and_are_reported() -> None:
    problem = harness_lock_error(_HELPERS_CALL_EACH_OTHER, "run_cycle.py")
    assert problem is not None
    assert "session lock: apply_migrations" in problem


def test_module_level_db_work_is_reported() -> None:
    problem = harness_lock_error(_DB_WORK_AT_MODULE_LEVEL, "run_import.py")
    assert problem is not None
    assert "session lock: apply_migrations" in problem
    assert "import" in problem, "the message must say why no `with` can cover it"


def test_the_rule_reads_the_main_python_will_actually_run() -> None:
    problem = harness_lock_error(_TWO_MAINS_THE_LAST_UNLOCKED, "run_twice.py")
    assert problem is not None
    assert "session lock: apply_migrations" in problem


def test_a_locally_shadowed_lock_helper_does_not_count() -> None:
    """Matching a bare name accepts any function spelled that way.

    Only reachable once the position rule passes — which is exactly when it
    matters, because the shadowed helper made everything look covered.
    """
    problem = harness_lock_error(_SHADOWS_THE_LOCK_HELPER, "run_shadow.py")
    assert problem is not None
    assert HARNESS_LOCK_MODULE in problem


# --------------------------------------------------------------------------
# Coverage cannot shrink silently: every DB-touching module is accounted for.
# --------------------------------------------------------------------------


def test_the_acceptance_directory_has_no_unaccounted_db_module() -> None:
    """The glob is `run_*.py`; nothing made that a rule rather than a habit.

    A harness named `bench_*.py`, or dropped in a subdirectory, took no lock
    and was never asked to — coverage shrinking with every test still green.
    This is the `pool_constructor_calls` arrangement in `tests/_pool_leaks.py`,
    which exists because `missing_seam_error` asks only whether a name is
    present.
    """
    assert acceptance_coverage_error(ACCEPTANCE_DIR) is None


def test_a_db_module_that_is_not_an_entry_point_is_reported(tmp_path: Path) -> None:
    (tmp_path / "run_real.py").write_text(_COMPLIANT)
    (tmp_path / "bench_new_thing.py").write_text(_NO_LOCK_AT_ALL)
    problem = acceptance_coverage_error(tmp_path)
    assert problem is not None
    assert "bench_new_thing.py" in problem


def test_an_allowlisted_library_may_not_touch_the_db_at_import(tmp_path: Path) -> None:
    """`browse_explain_lib.py` is exempt because its work runs inside
    `run_browse_explain.main`'s lock. That is true only while it does
    nothing at import — and nothing checked it.
    """
    (tmp_path / "run_real.py").write_text(_COMPLIANT)
    name = sorted(COVERED_LIBRARIES)[0]
    (tmp_path / name).write_text(_DB_WORK_AT_MODULE_LEVEL)
    problem = acceptance_coverage_error(tmp_path)
    assert problem is not None
    assert name in problem


def test_every_db_entry_call_name_is_reached_by_some_harness() -> None:
    """The set is the rule's pivot, and the per-file test cannot pin it.

    That one asserts a non-empty *intersection*, which every harness
    satisfies through `apply_migrations` alone — so dropping `open_pool` or
    `connect` left the suite green while the rule stopped seeing a pool
    opened before the lock. Mutation-proven in both directions.

    Direction matters: this asserts every member is *used*, which is what
    fails when a name is dropped and re-added to the set as dead weight.
    """
    used: set[str] = set()
    for path in harness_entry_points(ACCEPTANCE_DIR):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and isinstance(
                node.func, ast.Attribute | ast.Name
            ):
                used.add(
                    node.func.attr
                    if isinstance(node.func, ast.Attribute)
                    else node.func.id
                )
    missing = DB_ENTRY_CALLS - used
    assert not missing, (
        f"{sorted(missing)} is in DB_ENTRY_CALLS but no harness calls it, so "
        f"nothing would fail if the rule stopped recognising it"
    )
