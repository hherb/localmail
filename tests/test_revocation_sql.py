# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The shared "is this credential still honoured?" SQL fragment.

The value of centralising it is that every credential kind applies the *same*
predicate; the tests therefore check that the emitted fragment is valid SQL
under real aliases and that it decides the four cases identically no matter
which table it is pointed at.
"""
from __future__ import annotations

import psycopg
import pytest

from localmail.api.revocation_sql import credential_valid_sql


def test_fragment_is_parameter_free() -> None:
    """It is composed into larger statements, so it must not consume a
    placeholder slot — callers bind their own parameters positionally."""
    assert "%s" not in credential_valid_sql(user="u", credential="c")


def test_fragment_uses_the_aliases_it_is_given() -> None:
    sql = credential_valid_sql(user="usr", credential="cred")
    assert "usr.disabled_at" in sql
    assert "usr.sessions_invalidated_at" in sql
    assert "cred.created_at" in sql


def test_fragment_survives_being_spliced_after_or(db_conn: psycopg.Connection) -> None:
    """It is self-parenthesised, so an ``OR`` in front cannot regroup it.

    Every current call site splices it into an ``AND`` chain, where the inner
    ``OR``'s own parens suffice. This pins the general contract instead: an
    unwrapped ``A AND (B OR C)`` placed after ``FALSE OR`` would bind as
    ``(FALSE OR A) AND (B OR C)`` and start honouring revoked credentials.
    """
    sql = credential_valid_sql(user="u", credential="c")
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT FALSE OR " + sql + " FROM "
            "(VALUES (NULL::timestamptz, %s::timestamptz)) AS u"
            "  (disabled_at, sessions_invalidated_at), "
            "(VALUES (%s::timestamptz)) AS c (created_at)",
            (LATE, EARLY),
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] is False, "a credential predating the cutoff must stay invalid"


def test_fragment_alone_reads_a_missing_user_as_valid(
    db_conn: psycopg.Connection,
) -> None:
    """The fragment does **not** fail closed on an absent user row, and callers
    that outer-join the user have to know it.

    Against a LEFT JOIN miss every column of `u` is NULL, so both `IS NULL`
    tests are TRUE and the predicate returns TRUE — not NULL, so a surrounding
    `COALESCE(..., FALSE)` catches nothing. `codes.consume_code` is the only
    caller that outer-joins, and it carries its own `u.id IS NOT NULL`; this
    test documents why that line is not redundant.
    """
    sql = credential_valid_sql(user="u", credential="c")
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(" + sql + ", FALSE) FROM "
            "(VALUES (%s::timestamptz)) AS c (created_at) "
            "LEFT JOIN (SELECT NULL::bigint AS id, NULL::timestamptz AS disabled_at, "
            "                  NULL::timestamptz AS sessions_invalidated_at "
            "           WHERE FALSE) AS u ON TRUE",
            (EARLY,),
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] is True, (
        "if this ever starts returning False the guard in consume_code may be "
        "dropped as redundant — until then it is load-bearing"
    )


def _decide(conn: psycopg.Connection, *, disabled, invalidated, created) -> bool:
    """Evaluate the fragment against literal timestamps via a two-row VALUES
    join, so the test exercises the real SQL rather than a Python re-model."""
    sql = credential_valid_sql(user="u", credential="c")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT " + sql + " FROM "
            "(VALUES (%s::timestamptz, %s::timestamptz)) AS u"
            "  (disabled_at, sessions_invalidated_at), "
            "(VALUES (%s::timestamptz)) AS c (created_at)",
            (disabled, invalidated, created),
        )
        row = cur.fetchone()
    assert row is not None
    return row[0]


EARLY = "2026-01-01T00:00:00+00:00"
LATE = "2026-06-01T00:00:00+00:00"


@pytest.mark.parametrize(
    "disabled,invalidated,created,expected",
    [
        (None, None, EARLY, True),          # never disabled, never revoked
        (EARLY, None, LATE, False),         # disabled user
        (None, LATE, EARLY, False),         # credential predates the cutoff
        (None, EARLY, LATE, True),          # credential issued after the cutoff
        (None, EARLY, EARLY, True),         # cutoff is inclusive of its own moment
    ],
)
def test_fragment_decides_each_case(
    db_conn: psycopg.Connection, disabled, invalidated, created, expected
) -> None:
    assert _decide(
        db_conn, disabled=disabled, invalidated=invalidated, created=created
    ) is expected
