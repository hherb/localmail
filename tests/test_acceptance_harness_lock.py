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

from tests._db_session_lock import DatabaseSessionBusy
from tests.acceptance._harness_lock import (
    BUSY_EXIT_CODE,
    DB_ENTRY_CALLS,
    LOCK_HELPER_NAME,
    harness_db_lock,
    harness_entry_points,
    harness_lock_error,
)

ACCEPTANCE_DIR = Path(__file__).parent / "acceptance"


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
    with harness_db_lock("postgresql:///x", acquire=_acquire_returning(conn)) as held:
        assert held is conn


def test_the_lock_is_released_when_the_harness_finishes() -> None:
    conn = _FakeConn()
    with harness_db_lock("postgresql:///x", acquire=_acquire_returning(conn)):
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
        with harness_db_lock("postgresql:///x", acquire=_acquire_returning(conn)):
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
        with harness_db_lock("postgresql:///x", acquire=acquire, announce=said.append):
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

    with harness_db_lock("postgresql:///x", acquire=acquire, announce=said.append):
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
    """The rule pivots on this set, so a name dropped from it goes quiet.

    Every entry point must contain at least one of them — otherwise it is
    not a DB harness and the rule is being applied to the wrong file.
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

#: Needs only `--dsn`, so argparse — which runs *before* the lock — cannot
#: exit first and make the assertion below pass for the wrong reason.
_NO_REQUIRED_ARGS_HARNESS = "run_browse_explain.py"

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_a_harness_started_beside_this_suite_refuses_the_database(db_session_lock) -> None:
    """#337 itself, reproduced: this session holds the lock, the harness waits.

    Requesting `db_session_lock` is what makes the precondition real rather
    than assumed — it is the fixture holding `localmail_test` for the length
    of this run, i.e. exactly the "suite already in flight" the issue is
    about.

    The subprocess is given a one-second budget so the test costs a second
    rather than the ten-minute default; the point being proved is that it
    refuses at all, not how long it is willing to wait.

    Every other test here injects a fake `acquire`, so this is the only one
    that proves the wiring: that the import resolves on the harness's own
    sys.path, that `--dsn` reaches the helper, and that the refusal survives
    as a process exit code.
    """
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
            db_session_lock.info.dsn,
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
    assert "Traceback" not in proc.stderr
