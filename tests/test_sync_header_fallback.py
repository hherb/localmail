# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""A header the stdlib cannot parse, carrying 8-bit bytes, still syncs (#380).

`parser._headers_dict` goes through `header_block.entries_from_message`, so
the fallback's decoding decides what `messages.headers` stores. Kept as its
raw surrogate-escaped text it failed the JSONB insert, a permanent poison pill.
"""

from __future__ import annotations

import email.policy
from pathlib import Path

import psycopg

from localmail import sync as sync_mod
from localmail.sync import process_one_message

from .test_header_block import _XAParseRaises
from .test_sync import _ensure_account, make_account


def test_an_unparsable_raw_utf8_header_is_stored_decoded(
    db_conn: psycopg.Connection, tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(email.policy, "default", _XAParseRaises())
    account_id = _ensure_account(db_conn, make_account())
    mailbox = sync_mod.upsert_mailbox(
        db_conn, account_id=account_id, name="INBOX", delimiter="/", flags=[],
    )
    raw = (
        b"Message-Id: <fallback@x>\r\nFrom: a@x\r\nX-A: caf\xc3\xa9 \xff\r\n"
        b"Subject: ok\r\n\r\nbody\r\n"
    )

    msg_id, inserted = process_one_message(
        db_conn, account_id=account_id, mailbox_id=mailbox.id, uid=1, raw=raw,
        flags=[], attachments_root=tmp_path,
    )

    assert inserted
    with db_conn.cursor() as cur:
        cur.execute("SELECT headers -> 'X-A' FROM messages WHERE id = %s", (msg_id,))
        row = cur.fetchone()
    assert row is not None
    assert row[0] == ["café �"]
