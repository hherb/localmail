# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The acceptance stated for slice B: every occurrence, in order, and `full`
unchanged against a golden response (#379)."""
import json
from datetime import datetime, timezone

import psycopg
from fastapi.testclient import TestClient

from localmail.serve.app import create_app

_RAW = (
    b"Received: from a.example (a.example [10.0.0.1]) by mx.example; Tue, 15 Sep 2026 19:18:28 -0700\r\n"
    b"Authentication-Results: mx.example; dkim=pass header.d=a.example\r\n"
    b"received: from b.example (b.example [10.0.0.2]) by a.example; Tue, 15 Sep 2026 19:18:20 -0700\r\n"
    b"From: Anna <anna@example.com>\r\n"
    b"Subject: two hops and a case variant\r\n"
    b"\r\n"
    b"body\r\n"
)

_GOLDEN_FULL = {
    "Received": [
        "from a.example (a.example [10.0.0.1]) by mx.example; Tue, 15 Sep 2026 19:18:28 -0700",
    ],
    "Authentication-Results": ["mx.example; dkim=pass header.d=a.example"],
    "received": [
        "from b.example (b.example [10.0.0.2]) by a.example; Tue, 15 Sep 2026 19:18:20 -0700",
    ],
    "From": ["Anna <anna@example.com>"],
    "Subject": ["two hops and a case variant"],
}


def _seed(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method) "
            "VALUES ('acc','x@y.test','imap.x','password') RETURNING id"
        )
        row = cur.fetchone(); assert row is not None
        aid = row[0]
        cur.execute(
            "INSERT INTO mailboxes (account_id, name) VALUES (%s,'INBOX') RETURNING id",
            (aid,),
        )
        row = cur.fetchone(); assert row is not None
        mb = row[0]
        now = datetime.now(timezone.utc)
        cur.execute(
            """INSERT INTO messages (account_id, message_id, subject, from_addr,
                                     body_text, attachments, raw_bytes, raw_sha256,
                                     size_bytes, headers, date_sent, date_received)
               VALUES (%s,'<m@x>','two hops and a case variant','anna@example.com',
                       'body','[]'::jsonb,%s,%s,%s,%s::jsonb,%s,%s) RETURNING id""",
            (aid, _RAW, b"\x01" * 32, len(_RAW), json.dumps({}),
             datetime(2026, 9, 15, tzinfo=timezone.utc), now),
        )
        row = cur.fetchone(); assert row is not None
        mid = row[0]
        cur.execute(
            "INSERT INTO message_labels (message_id, mailbox_id, uid) VALUES (%s,%s,%s)",
            (mid, mb, mid),
        )
    conn.commit()
    return mid


def test_two_received_headers_and_a_case_variant_come_back_in_order(
    db_dsn: str, api_token: str, db_conn, grant_alice_all_accounts,
) -> None:
    mid = _seed(db_conn)
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(
        f"/v1/messages/{mid}?headers=list",
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 200
    assert [e["name"] for e in r.json()["headers"]] == [
        "Received", "Authentication-Results", "received", "From", "Subject",
    ]
    assert r.json()["headers"][0]["value"].startswith("from a.example")


def test_full_is_unchanged_against_the_golden_response(
    db_dsn: str, api_token: str, db_conn, grant_alice_all_accounts,
) -> None:
    mid = _seed(db_conn)
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(
        f"/v1/messages/{mid}?headers=full",
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 200
    assert r.json()["headers"] == _GOLDEN_FULL
