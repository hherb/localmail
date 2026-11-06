# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The harness lock rule compares the database, not just the position (#340).

`harness_lock_error` used to prove only that a database call sat inside a
``with harness_db_lock(...)`` block. Locking one database while working
against another passed — the #329 corruption with the guard reporting
nothing. These pin the check added for it, which compares the DSN
*expression* of each database call against the lock's, within the frame
that takes the lock (option 1 of #340). Kept out of
``test_acceptance_harness_lock.py``, which is already past 800 lines.
"""
from __future__ import annotations

import pytest

from tests.acceptance._harness_lock import DB_ENTRY_CALLS, harness_lock_error

_IMPORTS = """
import os
import psycopg
from tests.acceptance._harness_lock import harness_db_lock
from localmail.db import apply_migrations, open_pool
"""

#: The issue's own reproduction, verbatim in shape.
_ISSUE_340 = _IMPORTS + """
def main() -> int:
    with harness_db_lock(os.environ["LOCALMAIL_TEST_DSN"]):
        apply_migrations(args.dsn)
    return 0
"""

_SAME_EXPRESSION = _IMPORTS + """
def main() -> int:
    with harness_db_lock(args.dsn) as lock:
        apply_migrations(args.dsn)
        with psycopg.connect(args.dsn) as conn:
            pass
        pool = open_pool(args.dsn)
    return 0
"""

#: `run_chunk_insert_bench.py`'s shape: the lock is on `args.dsn` in main,
#: the connect is in a helper where the same database is the parameter
#: `dsn`. Comparing spellings across the call would report this; the rule
#: must not (the false positive #340 says the obvious fix produces).
_HELPER_TAKES_THE_DSN_AS_A_PARAMETER = _IMPORTS + """
def _run_mode(dsn):
    with psycopg.connect(dsn) as conn:
        conn.execute("TRUNCATE messages")

def main() -> int:
    with harness_db_lock(args.dsn):
        apply_migrations(args.dsn)
        _run_mode(args.dsn)
    return 0
"""

_KEYWORD_DSN_MATCHES = _IMPORTS + """
def main() -> int:
    with harness_db_lock(dsn):
        apply_migrations(dsn=dsn)
        with psycopg.connect(conninfo=dsn) as conn:
            pass
    return 0
"""

#: No DSN argument the rule can read: it cannot tell which database this
#: reaches, so it fails closed, like the rest of the module.
_UNREADABLE_DSN = _IMPORTS + """
def main() -> int:
    with harness_db_lock(dsn):
        apply_migrations(dsn)
        with psycopg.connect(**params) as conn:
            pass
    return 0
"""


def _mismatch_source(call: str) -> str:
    # A bare call: `_called_name` reads a `Name` as readily as an attribute,
    # so the case is built from the member itself and a fourth needs no edit.
    return _IMPORTS + f"""
def main() -> int:
    with harness_db_lock(dsn):
        apply_migrations(dsn)
        {call}(other_dsn)
    return 0
"""


def _locked_body(body: str, lock: str = "harness_db_lock(dsn)") -> str:
    indented = "\n".join(f"        {line}" for line in body.strip().splitlines())
    return _IMPORTS + f"""
def main() -> int:
    with {lock}:
{indented}
    return 0
"""


def test_the_issue_s_reproduction_is_reported_naming_both_databases() -> None:
    problem = harness_lock_error(_ISSUE_340, "run_x.py")
    assert problem is not None
    assert "session lock on `os.environ['LOCALMAIL_TEST_DSN']`" in problem
    assert "apply_migrations(`args.dsn`)" in problem


def test_one_expression_for_the_lock_and_the_work_passes() -> None:
    assert harness_lock_error(_SAME_EXPRESSION, "run_x.py") is None


@pytest.mark.parametrize("call", sorted(DB_ENTRY_CALLS))
def test_every_db_entry_call_is_compared(call: str) -> None:
    """Derived from the set, so a fourth entry call is compared too."""
    problem = harness_lock_error(_mismatch_source(call), "run_x.py")
    assert problem is not None
    assert f"{call}(`other_dsn`)" in problem


def test_a_helper_reached_under_the_lock_is_not_compared_by_spelling() -> None:
    assert harness_lock_error(_HELPER_TAKES_THE_DSN_AS_A_PARAMETER, "run_x.py") is None


def test_a_keyword_dsn_is_read_like_a_positional_one() -> None:
    assert harness_lock_error(_KEYWORD_DSN_MATCHES, "run_x.py") is None


def test_a_call_whose_dsn_cannot_be_read_is_reported() -> None:
    problem = harness_lock_error(_UNREADABLE_DSN, "run_x.py")
    assert problem is not None
    assert "connect(`<unreadable>`)" in problem


def test_two_unreadable_dsns_are_not_a_match() -> None:
    """Both sides render as the sentinel; equal sentinels prove nothing."""
    source = _locked_body("psycopg.connect(**other)", lock="harness_db_lock(**kw)")
    problem = harness_lock_error(source, "run_x.py")
    assert problem is not None
    assert "connect(`<unreadable>`)" in problem


@pytest.mark.parametrize(
    "call",
    [
        "psycopg.connect(dsn, dbname='localmail')",
        "psycopg.connect(conninfo=dsn, host='elsewhere')",
        "psycopg.connect(dsn, **live)",
        "open_pool(dsn, connect_kwargs={'dbname': 'localmail'})",
    ],
)
def test_keywords_that_may_redirect_the_same_dsn_are_reported(call: str) -> None:
    """libpq keywords override the conninfo string, so the spelling alone
    does not say which database the call reaches."""
    problem = harness_lock_error(_locked_body(call), "run_x.py")
    assert problem is not None
    assert "(`<unreadable>`)" in problem


def test_a_keyword_dsn_naming_another_database_is_reported() -> None:
    problem = harness_lock_error(
        _locked_body("psycopg.connect(conninfo=other_dsn)"), "run_x.py"
    )
    assert problem is not None
    assert "connect(`other_dsn`)" in problem


def test_a_non_dsn_keyword_is_not_read_as_the_dsn() -> None:
    problem = harness_lock_error(
        _locked_body("psycopg.connect(autocommit=True)"), "run_x.py"
    )
    assert problem is not None
    assert "connect(`<unreadable>`)" in problem


def test_a_helper_that_takes_the_lock_itself_is_compared() -> None:
    source = _IMPORTS + """
def _work(d):
    with harness_db_lock(d):
        apply_migrations(other_dsn)

def main() -> int:
    _work(args.dsn)
    return 0
"""
    problem = harness_lock_error(source, "run_x.py")
    assert problem is not None
    assert "session lock on `d` but apply_migrations(`other_dsn`)" in problem


def test_the_lock_s_dsn_is_read_even_when_it_is_not_the_first_item() -> None:
    source = _locked_body(
        "apply_migrations(dsn)",
        lock="contextlib.ExitStack(), harness_db_lock(dsn)",
    )
    assert harness_lock_error(source, "run_x.py") is None


def test_every_mismatch_is_named_not_just_the_first() -> None:
    problem = harness_lock_error(
        _locked_body("apply_migrations(a)\napply_migrations(b)"), "run_x.py"
    )
    assert problem is not None
    assert "apply_migrations(`a`)" in problem
    assert "apply_migrations(`b`)" in problem
