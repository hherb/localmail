import json
from datetime import datetime, timezone

import psycopg
import pytest

from localmail.api.errors import NotFound
from localmail.api.messages import get_message, get_message_raw


def _seed_msg(conn: psycopg.Connection, **overrides) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            ("acct", "horst@example.com", "imap.example.com", "password"),
        )
        row = cur.fetchone()
        assert row is not None
        aid = row[0]
        cur.execute(
            "INSERT INTO mailboxes (account_id, name) VALUES (%s, %s) RETURNING id",
            (aid, "INBOX"),
        )
        row = cur.fetchone()
        assert row is not None
        mb_id = row[0]
        now = datetime.now(timezone.utc)
        defaults: dict = dict(
            account_id=aid,
            message_id="<m1@example>",
            subject="hello",
            from_addr="anna@example.com",
            from_name="Anna",
            to_addrs=["horst@example.com"],
            cc_addrs=None,
            bcc_addrs=None,
            body_text="hi there",
            body_html="<p>hi <b>there</b></p>",
            attachments=[],
            raw_bytes=b"From: anna\r\nSubject: hello\r\n\r\nhi",
            headers={"From": "anna@example.com", "Subject": "hello", "Date": "Mon, 4 Mar 2026 10:00:00 +0000"},
            date_sent=datetime(2026, 3, 4, 10, 0, tzinfo=timezone.utc),
            date_received=now,
        )
        defaults.update(overrides)
        defaults["raw_sha256"] = b"\x00" * 32
        defaults["size_bytes"] = len(defaults["raw_bytes"])
        cur.execute(
            """INSERT INTO messages
               (account_id, message_id, subject, from_addr, from_name, to_addrs,
                cc_addrs, bcc_addrs, body_text, body_html, attachments,
                raw_bytes, raw_sha256, size_bytes, headers, date_sent, date_received)
               VALUES (%(account_id)s, %(message_id)s, %(subject)s, %(from_addr)s,
                       %(from_name)s, %(to_addrs)s, %(cc_addrs)s, %(bcc_addrs)s,
                       %(body_text)s, %(body_html)s, %(attachments)s::jsonb,
                       %(raw_bytes)s, %(raw_sha256)s, %(size_bytes)s,
                       %(headers)s::jsonb, %(date_sent)s, %(date_received)s)
               RETURNING id""",
            {**defaults,
             "attachments": json.dumps(defaults["attachments"]),
             "headers": json.dumps(defaults["headers"])},
        )
        row = cur.fetchone()
        assert row is not None
        msg_id = row[0]
        cur.execute(
            "INSERT INTO message_labels (message_id, mailbox_id, uid) VALUES (%s, %s, %s)",
            (msg_id, mb_id, msg_id),  # use msg_id as a fake UID
        )
        return msg_id


def test_get_message_returns_compact_headers(db_conn: psycopg.Connection) -> None:
    mid = _seed_msg(db_conn)
    db_conn.commit()
    msg = get_message(db_conn, mid, full_headers=False)
    assert msg["id"] == str(mid)
    assert msg["subject"] == "hello"
    assert msg["from"]["address"] == "anna@example.com"
    assert msg["from"]["name"] == "Anna"
    assert msg["to"][0]["address"] == "horst@example.com"
    assert "<p>hi" in msg["body_html"]
    assert msg["body_text"] == "hi there"
    assert msg["account"]["name"] == "acct"
    assert msg["folders"][0]["name"] == "INBOX"
    assert "headers" not in msg or msg.get("headers") in (None, {})


def test_get_message_full_headers_includes_all(db_conn: psycopg.Connection) -> None:
    mid = _seed_msg(db_conn)
    db_conn.commit()
    msg = get_message(db_conn, mid, full_headers=True)
    assert msg["headers"]["From"] == "anna@example.com"
    assert msg["headers"]["Date"].startswith("Mon, 4 Mar")


def test_get_message_not_found_raises(db_conn: psycopg.Connection) -> None:
    with pytest.raises(NotFound):
        get_message(db_conn, 999999)


def test_get_message_raw_returns_bytes(db_conn: psycopg.Connection) -> None:
    mid = _seed_msg(db_conn)
    db_conn.commit()
    raw = get_message_raw(db_conn, mid)
    assert raw.startswith(b"From: anna")


def test_get_message_raw_not_found_raises(db_conn: psycopg.Connection) -> None:
    with pytest.raises(NotFound):
        get_message_raw(db_conn, 999999)
