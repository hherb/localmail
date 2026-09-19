# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""An attachment is addressed by its position in the message's array."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

import psycopg
import pytest

from localmail.api.attachments import (
    MAX_JSONB_INDEX,
    MessageAttachment,
    resolve_message_attachment,
)
from localmail.api.errors import NotFound, ValidationFailed

_A = "a1" * 32
_B = "b2" * 32

_ENTRIES: list[dict[str, Any]] = [
    {"filename": "note.txt", "sha256": _A},
    {"filename": "note.txt", "sha256": _B},
    {"sha256": _A},  # same bytes as entry 0, no name of its own
]


def _seed(conn: psycopg.Connection, entries: list[dict[str, Any]]) -> tuple[int, int]:
    """One account carrying one message with this `attachments` array."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method) "
            "VALUES ('acct', 'x@y.test', 'imap.x', 'password') RETURNING id",
        )
        row = cur.fetchone(); assert row is not None
        aid = int(row[0])
        raw = b"x"
        cur.execute(
            "INSERT INTO messages (account_id, message_id, raw_bytes, raw_sha256, "
            "size_bytes, headers, attachments, date_sent) "
            "VALUES (%s, '<m@x>', %s, %s, 1, '{}'::jsonb, %s, %s) RETURNING id",
            (aid, raw, hashlib.sha256(raw).digest(),
             psycopg.types.json.Jsonb(entries), datetime.now(timezone.utc)),
        )
        row = cur.fetchone(); assert row is not None
        mid = int(row[0])
    conn.commit()
    return aid, mid


def test_each_index_resolves_to_its_own_entry(db_conn: psycopg.Connection) -> None:
    aid, mid = _seed(db_conn, _ENTRIES)
    got = [
        resolve_message_attachment(db_conn, mid, i, allowed_account_ids=[aid])
        for i in range(3)
    ]
    assert got == [
        MessageAttachment(sha256=_A, filename="note.txt"),
        MessageAttachment(sha256=_B, filename="note.txt"),
        MessageAttachment(sha256=_A, filename=None),
    ]


def test_an_index_past_the_end_is_the_shared_404(db_conn: psycopg.Connection) -> None:
    aid, mid = _seed(db_conn, _ENTRIES)
    with pytest.raises(NotFound, match=f"attachment 3 of message {mid} not found"):
        resolve_message_attachment(db_conn, mid, 3, allowed_account_ids=[aid])


def test_ungranted_is_indistinguishable_from_missing(db_conn: psycopg.Connection) -> None:
    aid, mid = _seed(db_conn, _ENTRIES)
    with pytest.raises(NotFound) as ungranted:
        resolve_message_attachment(db_conn, mid, 0, allowed_account_ids=[aid + 1])
    with pytest.raises(NotFound) as missing:
        resolve_message_attachment(db_conn, mid + 1, 0, allowed_account_ids=[aid])
    assert str(ungranted.value) == f"attachment 0 of message {mid} not found"
    assert str(missing.value) == f"attachment 0 of message {mid + 1} not found"


def test_a_negative_index_is_refused_not_answered_with_the_last_entry(
    db_conn: psycopg.Connection,
) -> None:
    # Postgres `->` indexes from the end: `attachments -> -1` IS the last
    # entry. The accessor is public API, so it must refuse -1 itself rather
    # than rely on parse_int_id having refused it on the wire.
    aid, mid = _seed(db_conn, _ENTRIES)
    with pytest.raises(ValidationFailed, match="attachment index must be >= 0, got -1"):
        resolve_message_attachment(db_conn, mid, -1, allowed_account_ids=[aid])


def test_a_negative_index_is_refused_before_the_empty_acl_short_circuit(
    db_conn: psycopg.Connection,
) -> None:
    aid, mid = _seed(db_conn, _ENTRIES)
    with pytest.raises(ValidationFailed):
        resolve_message_attachment(db_conn, mid, -1, allowed_account_ids=[])


class _Untouchable:
    """A connection that fails the test if anything asks it for a cursor."""

    def cursor(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("the resolver queried when it should not have")


def test_an_index_past_int4_is_a_404_without_a_query() -> None:
    with pytest.raises(NotFound):
        resolve_message_attachment(
            _Untouchable(), 1, MAX_JSONB_INDEX + 1,  # type: ignore[arg-type]
            allowed_account_ids=[1],
        )


def test_the_largest_int4_index_is_answered_by_the_query_not_a_500(
    db_conn: psycopg.Connection,
) -> None:
    aid, mid = _seed(db_conn, _ENTRIES)
    with pytest.raises(NotFound):
        resolve_message_attachment(
            db_conn, mid, MAX_JSONB_INDEX, allowed_account_ids=[aid],
        )


def test_the_ceiling_is_int4_max() -> None:
    assert MAX_JSONB_INDEX == 2**31 - 1


def test_an_entry_without_a_hash_is_a_404(db_conn: psycopg.Connection) -> None:
    aid, mid = _seed(db_conn, [{"filename": "orphan.bin"}])
    with pytest.raises(NotFound):
        resolve_message_attachment(db_conn, mid, 0, allowed_account_ids=[aid])


def test_an_empty_acl_is_a_404(db_conn: psycopg.Connection) -> None:
    _aid, mid = _seed(db_conn, _ENTRIES)
    with pytest.raises(NotFound):
        resolve_message_attachment(db_conn, mid, 0, allowed_account_ids=[])
