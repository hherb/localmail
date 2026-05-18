from datetime import datetime, timezone

import psycopg

from localmail.api.accounts import list_accounts, list_folders


def _seed_account(conn: psycopg.Connection, name: str, address: str | None = None) -> int:
    """Insert an account row. Provides all NOT NULL fields."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (name, address or f"{name}@example.com", "imap.example.com", "password"),
        )
        row = cur.fetchone()
        assert row is not None
        return row[0]


def _seed_mailbox(conn: psycopg.Connection, account_id: int, name: str, *, flags: str | None = None) -> int:
    # flags column is a text array; pass as list or None
    flags_arr = [flags] if flags is not None else None
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO mailboxes (account_id, name, flags) VALUES (%s, %s, %s) RETURNING id",
            (account_id, name, flags_arr),
        )
        row = cur.fetchone()
        assert row is not None
        return row[0]


def _seed_message(conn: psycopg.Connection, account_id: int, mailbox_id: int) -> int:
    """Minimal message insert satisfying all NOT NULL constraints."""
    now = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO messages (account_id, message_id, raw_bytes, raw_sha256, size_bytes, "
            "                       headers, attachments, date_sent, date_received) "
            "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s) RETURNING id",
            (account_id, f"<{mailbox_id}-{now.timestamp()}@test>", b"raw", b"\x00" * 32, 3,
             "{}", "[]", now, now),
        )
        msg_row = cur.fetchone()
        assert msg_row is not None
        # uid is NOT NULL on message_labels; use mailbox_id as a stable fake UID
        cur.execute(
            "INSERT INTO message_labels (message_id, mailbox_id, uid) VALUES (%s, %s, %s)",
            (msg_row[0], mailbox_id, msg_row[0]),
        )
        return msg_row[0]


_ANY_ACCOUNT = list(range(1, 1000))


def test_list_accounts_empty(db_conn: psycopg.Connection) -> None:
    assert list_accounts(db_conn, allowed_account_ids=_ANY_ACCOUNT) == []


def test_list_accounts_returns_basic_fields(db_conn: psycopg.Connection) -> None:
    aid = _seed_account(db_conn, "gmail-primary", "horst@gmail.com")
    db_conn.commit()
    accounts = list_accounts(db_conn, allowed_account_ids=[aid])
    assert len(accounts) == 1
    a = accounts[0]
    assert a["id"] == str(aid)
    assert a["name"] == "gmail-primary"
    assert a["address"] == "horst@gmail.com"  # JSON field is "address", maps from email_address column
    assert a["message_count"] == 0
    assert a["capabilities"]["is_archive_only"] in (True, False)


def test_list_accounts_message_count(db_conn: psycopg.Connection) -> None:
    aid = _seed_account(db_conn, "acct")
    mid = _seed_mailbox(db_conn, aid, "INBOX")
    _seed_message(db_conn, aid, mid)
    _seed_message(db_conn, aid, mid)
    db_conn.commit()
    a = list_accounts(db_conn, allowed_account_ids=[aid])[0]
    assert a["message_count"] == 2


def test_list_folders_returns_per_mailbox_counts(db_conn: psycopg.Connection) -> None:
    aid = _seed_account(db_conn, "acct")
    inbox = _seed_mailbox(db_conn, aid, "INBOX")
    sent = _seed_mailbox(db_conn, aid, "Sent", flags=r"\Sent")
    _seed_message(db_conn, aid, inbox)
    db_conn.commit()
    folders = list_folders(db_conn, aid, allowed_account_ids=[aid])
    by_name = {f["name"]: f for f in folders}
    assert by_name["INBOX"]["message_count"] == 1
    assert by_name["Sent"]["message_count"] == 0
    assert by_name["Sent"]["flags"] == r"\Sent"


def test_list_folders_unknown_account_returns_empty(db_conn: psycopg.Connection) -> None:
    assert list_folders(db_conn, 99999, allowed_account_ids=_ANY_ACCOUNT) == []
