# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""A `Date:` whose year overflows a C long still syncs, undated (#430).

`email.utils.parsedate_to_datetime` raises `OverflowError` for it, and the
stdlib's `DateHeader.parse` catches only `ValueError`, so the raise escaped
`msg.get("Date")` itself and the message landed in `failed_messages` for good
(`retry-failed` re-parses the same bytes). A real header, not a seam: every
interpreter we run raises on this one.
"""

from __future__ import annotations

from pathlib import Path

import psycopg

from localmail import sync as sync_mod
from localmail.parser import parse_message
from localmail.sync import process_one_message

from .test_sync import _ensure_account, make_account

_RAW = (
    b"Message-Id: <overflow@x>\r\nFrom: a@x\r\n"
    b"Date: Mon, 1 Jan 99999999999999999999 00:00:00 +0000\r\n"
    b"Subject: giant year\r\n\r\nbody\r\n"
)


def test_an_overflowing_date_parses_as_undated() -> None:
    parsed = parse_message(_RAW)

    assert parsed.date_sent is None
    assert parsed.subject == "giant year"


def test_an_overflowing_date_is_stored_not_poison_pilled(
    db_conn: psycopg.Connection, tmp_path: Path,
) -> None:
    account_id = _ensure_account(db_conn, make_account())
    mailbox = sync_mod.upsert_mailbox(
        db_conn, account_id=account_id, name="INBOX", delimiter="/", flags=[],
    )

    msg_id, inserted = process_one_message(
        db_conn, account_id=account_id, mailbox_id=mailbox.id, uid=1, raw=_RAW,
        flags=[], attachments_root=tmp_path,
    )

    assert inserted
    with db_conn.cursor() as cur:
        cur.execute("SELECT date_sent FROM messages WHERE id = %s", (msg_id,))
        row = cur.fetchone()
        assert row is not None
        assert row[0] is None
        cur.execute("SELECT count(*) FROM failed_messages")
        failed = cur.fetchone()
    assert failed is not None
    assert failed[0] == 0
