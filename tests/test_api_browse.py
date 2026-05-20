"""Tests for localmail.api.browse.list_messages."""
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from localmail.api.browse import list_messages
from localmail.api.browse_cursor import decode_browse_cursor
from localmail.api.errors import ValidationFailed


def _ensure_account(conn: psycopg.Connection, name: str = "a") -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method) "
            "VALUES (%s, %s, 'imap.x', 'password') RETURNING id",
            (name, f"{name}@y.test"),
        )
        row = cur.fetchone(); assert row is not None
        return int(row[0])


def _seed(
    conn: psycopg.Connection, *,
    account_id: int,
    suffix: str,
    internal_date: datetime | None = None,
    date_sent: datetime | None = None,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO messages (account_id, message_id, subject, raw_bytes,
                                     raw_sha256, size_bytes, headers, attachments,
                                     date_sent, internal_date, date_received)
               VALUES (%s, %s, 's', 'r', %s, 1, '{}'::jsonb, '[]'::jsonb,
                       %s, %s, now()) RETURNING id""",
            (account_id, f"<{suffix}@x>", bytes.fromhex(suffix * 32),
             date_sent, internal_date),
        )
        row = cur.fetchone(); assert row is not None
        return int(row[0])


def test_initial_page_returns_messages_in_recent_first_order(db_conn) -> None:
    aid = _ensure_account(db_conn)
    now = datetime.now(timezone.utc)
    m_old = _seed(db_conn, account_id=aid, suffix="aa",
                  internal_date=now - timedelta(days=2))
    m_mid = _seed(db_conn, account_id=aid, suffix="bb",
                  internal_date=now - timedelta(days=1))
    m_new = _seed(db_conn, account_id=aid, suffix="cc",
                  internal_date=now)
    db_conn.commit()

    out = list_messages(db_conn, allowed_account_ids=[aid], limit=10)
    ids = [int(m["message_id"]) for m in out["messages"]]
    assert ids == [m_new, m_mid, m_old]
    assert out["next_cursor"] is None  # pool exhausted, only 3 rows


def test_cursor_round_trip_paginates_strictly_older(db_conn) -> None:
    aid = _ensure_account(db_conn)
    now = datetime.now(timezone.utc)
    ids = [
        _seed(db_conn, account_id=aid, suffix=f"{i:02x}" * 1,
              internal_date=now - timedelta(hours=i))
        for i in range(5)
    ]
    # ids[0] is the newest (i=0), ids[4] is the oldest (i=4).
    db_conn.commit()

    page1 = list_messages(db_conn, allowed_account_ids=[aid], limit=2)
    assert [int(m["message_id"]) for m in page1["messages"]] == [ids[0], ids[1]]
    assert page1["next_cursor"] is not None

    page2 = list_messages(db_conn, allowed_account_ids=[aid], limit=2,
                          cursor=page1["next_cursor"])
    assert [int(m["message_id"]) for m in page2["messages"]] == [ids[2], ids[3]]
    assert page2["next_cursor"] is not None

    page3 = list_messages(db_conn, allowed_account_ids=[aid], limit=2,
                          cursor=page2["next_cursor"])
    assert [int(m["message_id"]) for m in page3["messages"]] == [ids[4]]
    assert page3["next_cursor"] is None


def test_tied_internal_date_paginates_by_id_desc(db_conn) -> None:
    aid = _ensure_account(db_conn)
    ts = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
    a = _seed(db_conn, account_id=aid, suffix="aa", internal_date=ts)
    b = _seed(db_conn, account_id=aid, suffix="bb", internal_date=ts)
    c = _seed(db_conn, account_id=aid, suffix="cc", internal_date=ts)
    db_conn.commit()

    p1 = list_messages(db_conn, allowed_account_ids=[aid], limit=2)
    assert [int(m["message_id"]) for m in p1["messages"]] == [c, b]
    p2 = list_messages(db_conn, allowed_account_ids=[aid], limit=2,
                       cursor=p1["next_cursor"])
    assert [int(m["message_id"]) for m in p2["messages"]] == [a]


def test_empty_allowed_account_ids_returns_empty_page(db_conn) -> None:
    out = list_messages(db_conn, allowed_account_ids=[], limit=10)
    assert out == {"messages": [], "next_cursor": None}


def test_malformed_cursor_raises_validation_failed(db_conn) -> None:
    with pytest.raises(ValidationFailed):
        list_messages(db_conn, allowed_account_ids=[1], limit=10,
                      cursor="not-a-cursor")


def test_null_date_rows_paginate_after_dated_rows(db_conn) -> None:
    aid = _ensure_account(db_conn)
    now = datetime.now(timezone.utc)
    dated = _seed(db_conn, account_id=aid, suffix="aa",
                  internal_date=now - timedelta(hours=1))
    nul_a = _seed(db_conn, account_id=aid, suffix="bb")  # both dates NULL
    nul_b = _seed(db_conn, account_id=aid, suffix="cc")
    db_conn.commit()

    # Dated row first; NULL rows tail in id DESC (so nul_b before nul_a).
    p1 = list_messages(db_conn, allowed_account_ids=[aid], limit=1)
    assert [int(m["message_id"]) for m in p1["messages"]] == [dated]

    p2 = list_messages(db_conn, allowed_account_ids=[aid], limit=1,
                       cursor=p1["next_cursor"])
    assert [int(m["message_id"]) for m in p2["messages"]] == [nul_b]

    p3 = list_messages(db_conn, allowed_account_ids=[aid], limit=1,
                       cursor=p2["next_cursor"])
    assert [int(m["message_id"]) for m in p3["messages"]] == [nul_a]
    assert p3["next_cursor"] is None


def test_account_ids_filter_is_intersected_with_acl(db_conn) -> None:
    aid1 = _ensure_account(db_conn, name="alpha")
    aid2 = _ensure_account(db_conn, name="beta")
    now = datetime.now(timezone.utc)
    m1 = _seed(db_conn, account_id=aid1, suffix="aa", internal_date=now)
    _seed(db_conn, account_id=aid2, suffix="bb", internal_date=now)
    db_conn.commit()

    # Caller asks for both accounts but is only granted aid1.
    out = list_messages(db_conn, allowed_account_ids=[aid1],
                        account_ids=[aid1, aid2], limit=10)
    ids = [int(m["message_id"]) for m in out["messages"]]
    assert ids == [m1]


def test_account_ids_intersection_empty_short_circuits(db_conn) -> None:
    aid_granted = _ensure_account(db_conn, name="alpha")
    aid_other = _ensure_account(db_conn, name="beta")
    now = datetime.now(timezone.utc)
    _seed(db_conn, account_id=aid_granted, suffix="aa", internal_date=now)
    _seed(db_conn, account_id=aid_other, suffix="bb", internal_date=now)
    db_conn.commit()

    out = list_messages(db_conn, allowed_account_ids=[aid_granted],
                        account_ids=[aid_other], limit=10)
    assert out == {"messages": [], "next_cursor": None}


def test_folder_ids_filter_restricts_to_labelled_messages(db_conn) -> None:
    aid = _ensure_account(db_conn)
    now = datetime.now(timezone.utc)
    m_in = _seed(db_conn, account_id=aid, suffix="aa", internal_date=now)
    m_out = _seed(db_conn, account_id=aid, suffix="bb", internal_date=now)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO mailboxes (account_id, name, uidvalidity) "
            "VALUES (%s, 'INBOX', 1) RETURNING id", (aid,),
        )
        row = cur.fetchone(); assert row is not None
        mb_id = int(row[0])
        cur.execute(
            "INSERT INTO message_labels (message_id, mailbox_id, uid) "
            "VALUES (%s, %s, 1)", (m_in, mb_id),
        )
    db_conn.commit()

    out = list_messages(db_conn, allowed_account_ids=[aid],
                        folder_ids=[mb_id], limit=10)
    ids = [int(m["message_id"]) for m in out["messages"]]
    assert ids == [m_in]
    assert m_out not in ids
