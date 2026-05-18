from datetime import datetime, timezone
import json

import psycopg
from fastapi.testclient import TestClient

from localmail.serve.app import create_app


def _seed_msg(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method) "
            "VALUES ('a','x@y.test','imap.x','password') RETURNING id"
        )
        row = cur.fetchone(); assert row is not None
        aid = row[0]
        cur.execute("INSERT INTO mailboxes (account_id, name) VALUES (%s, 'INBOX') RETURNING id", (aid,))
        row = cur.fetchone(); assert row is not None
        mb = row[0]
        now = datetime.now(timezone.utc)
        cur.execute(
            """INSERT INTO messages (account_id, message_id, subject, from_addr, from_name,
                                     body_text, body_html, attachments, raw_bytes, raw_sha256,
                                     size_bytes, headers, date_sent, date_received)
               VALUES (%s, '<m@x>', 'hello', 'a@x', 'Anna', 'hi', '<p>hi</p>', '[]'::jsonb,
                       'RAW', %s, 3, %s::jsonb, %s, %s) RETURNING id""",
            (aid, b"\x00" * 32, json.dumps({"From": "a@x"}),
             datetime(2026, 3, 4, tzinfo=timezone.utc), now),
        )
        row = cur.fetchone(); assert row is not None
        mid = row[0]
        cur.execute(
            "INSERT INTO message_labels (message_id, mailbox_id, uid) VALUES (%s, %s, %s)",
            (mid, mb, mid),  # fake uid
        )
    conn.commit()
    return mid


def test_get_message(
    db_dsn: str, api_token: str, db_conn, grant_alice_all_accounts,
) -> None:
    mid = _seed_msg(db_conn)
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(f"/v1/messages/{mid}", headers={"Authorization": f"Bearer {api_token}"})
    assert r.status_code == 200
    body = r.json()
    assert body["subject"] == "hello"
    assert "<p>hi" in body["body_html"]


def test_get_message_full_headers(
    db_dsn: str, api_token: str, db_conn, grant_alice_all_accounts,
) -> None:
    mid = _seed_msg(db_conn)
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(
        f"/v1/messages/{mid}?headers=full",
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 200
    assert r.json()["headers"]["From"] == "a@x"


def test_get_message_not_found(db_dsn: str, api_token: str) -> None:
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get("/v1/messages/999999", headers={"Authorization": f"Bearer {api_token}"})
    assert r.status_code == 404


def test_get_raw(
    db_dsn: str, api_token: str, db_conn, grant_alice_all_accounts,
) -> None:
    mid = _seed_msg(db_conn)
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(f"/v1/messages/{mid}/raw", headers={"Authorization": f"Bearer {api_token}"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("message/rfc822")
    assert r.content == b"RAW"
