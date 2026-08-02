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
