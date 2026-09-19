# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Slice D's acceptance, through the real ingestion path: two attachments of
the same filename are addressable by index, the text pages, and the sha route
is untouched (its golden is `test_serve_attachments_golden.py`)."""
from __future__ import annotations

from pathlib import Path

import psycopg
from fastapi.testclient import TestClient

from localmail.serve.app import create_app
from localmail.sync import process_one_message
from . import _eml


def _ingest(conn: psycopg.Connection, tmp_path: Path) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method) "
            "VALUES ('acc', 'x@y.test', 'imap.x', 'password') RETURNING id",
        )
        row = cur.fetchone(); assert row is not None
        aid = int(row[0])
        cur.execute(
            "INSERT INTO mailboxes (account_id, name) VALUES (%s, 'INBOX') RETURNING id",
            (aid,),
        )
        row = cur.fetchone(); assert row is not None
        mb = int(row[0])
    mid, inserted = process_one_message(
        conn, account_id=aid, mailbox_id=mb, uid=1,
        raw=_eml.two_attachments_same_name(), flags=[],
        attachments_root=tmp_path,
    )
    assert inserted
    conn.commit()
    return mid


def test_two_attachments_of_one_name_are_addressable_by_index(
    db_dsn, api_token, db_conn, tmp_path, grant_alice_all_accounts,
) -> None:
    mid = _ingest(db_conn, tmp_path)
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    auth = {"Authorization": f"Bearer {api_token}"}

    listed = c.get(f"/v1/messages/{mid}", headers=auth).json()["attachments"]
    assert [a["filename"] for a in listed] == ["note.txt", "note.txt"]

    first = c.get(f"/v1/messages/{mid}/attachments/0", headers=auth)
    second = c.get(f"/v1/messages/{mid}/attachments/1", headers=auth)
    assert (first.content, second.content) == (b"file-one", b"file-two")
    # The index route agrees with the array get_message returns.
    assert first.headers["etag"] == f'"{listed[0]["sha256"]}"'
    assert second.headers["etag"] == f'"{listed[1]["sha256"]}"'
    # Both entries share a name, so this shows only that a name is served;
    # per-entry naming is test_disposition_carries_the_entrys_own_name.
    assert 'filename="note.txt"' in second.headers["content-disposition"]


def test_the_text_of_an_indexed_attachment_pages(
    db_dsn, api_token, db_conn, tmp_path, grant_alice_all_accounts,
) -> None:
    mid = _ingest(db_conn, tmp_path)
    row = db_conn.execute(
        "SELECT attachments -> 1 ->> 'sha256' FROM messages WHERE id = %s", (mid,),
    ).fetchone()
    assert row is not None
    sha = row[0]
    db_conn.execute(
        "INSERT INTO attachment_text (sha256, extractor, extracted_text) "
        "VALUES (%s, 'lightweight', 'file-two')",
        (bytes.fromhex(sha),),
    )
    db_conn.commit()
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    auth = {"Authorization": f"Bearer {api_token}"}
    pages: list[str] = []
    offset: int | None = 0
    while offset is not None:
        body = c.get(
            f"/v1/messages/{mid}/attachments/1/text?offset={offset}&limit=3",
            headers=auth,
        ).json()
        pages.append(body["text"])
        offset = body["next_offset"]
    assert pages == ["fil", "e-t", "wo"]
