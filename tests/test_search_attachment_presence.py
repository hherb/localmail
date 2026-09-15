# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The one "has attachments" rule, and its guard against malformed rows (#364)."""
from __future__ import annotations

from datetime import datetime, timezone

import psycopg
import pytest

from localmail.search.arms import _filter_sql
from localmail.search.attachment_presence import HAS_ATTACHMENT_SQL
from localmail.search.query import SearchFilters

_PDF = '[{"filename": "ticket.pdf", "sha256": "' + "ab" * 32 + '"}]'


def _seed(conn: psycopg.Connection) -> dict[str, int]:
    """One message per shape the rule must decide: with, without, malformed.

    ``malformed`` is an object where an array belongs. No writer produces it,
    but a restore or a hand UPDATE can, and the column has no CHECK.
    """
    ids: dict[str, int] = {}
    with conn.cursor() as cur:
        cur.execute("INSERT INTO accounts (name, email_address, imap_host, auth_method)"
                    " VALUES ('a', 'a@x', 'h', 'password') RETURNING id")
        row = cur.fetchone()
        assert row is not None
        acct = row[0]
        for i, (key, attachments) in enumerate(
            [("with", _PDF), ("without", "[]"), ("malformed", "{}")]
        ):
            cur.execute(
                "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
                " body_text, headers, raw_bytes, size_bytes, attachments, internal_date)"
                " VALUES (%s, %s, %s, %s, 'body', '{}'::jsonb, 'r', 1, %s::jsonb, %s)"
                " RETURNING id",
                (acct, f"<{key}>", bytes([i + 1]) * 32, f"Subject {key}", attachments,
                 datetime(2026, 3, i + 1, tzinfo=timezone.utc)),
            )
            row = cur.fetchone()
            assert row is not None
            ids[key] = row[0]
    conn.commit()
    return ids


def test_the_rule_decides_each_shape(db_conn) -> None:
    ids = _seed(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(f"SELECT m.id, {HAS_ATTACHMENT_SQL} FROM messages m")
        got = dict(cur.fetchall())
    assert got == {ids["with"]: True, ids["without"]: False, ids["malformed"]: False}


@pytest.mark.parametrize("wanted, expected_keys", [
    (True, {"with"}),
    (False, {"without", "malformed"}),
])
def test_the_filter_is_the_rule_and_its_exact_complement(
    db_conn, wanted, expected_keys,
) -> None:
    ids = _seed(db_conn)
    fragment, params = _filter_sql(SearchFilters(has_attachment=wanted))
    with db_conn.cursor() as cur:
        cur.execute(f"SELECT m.id FROM messages m WHERE TRUE{fragment}", params)
        got = {row[0] for row in cur.fetchall()}
    assert got == {ids[k] for k in expected_keys}


def test_the_unguarded_expression_fails_on_the_malformed_row(db_conn) -> None:
    """Negative control: the fixture really reaches the failure the guard exists for.

    Without it, a guard that did nothing would pass both tests above on a
    fixture that never put a non-array in front of ``jsonb_array_length``.
    """
    _seed(db_conn)
    with db_conn.cursor() as cur, pytest.raises(psycopg.errors.InvalidParameterValue):
        cur.execute("SELECT jsonb_array_length(m.attachments) > 0 FROM messages m")
    db_conn.rollback()
