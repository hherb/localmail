"""Server-side narrowing by account_ids / folder_ids works at every arm.

These tests assert that _filter_sql injects the right SQL predicates so
arm_bm25_messages returns only matching rows. Other arms inherit the same
filter clause via _filter_sql so we don't repeat the seeding for each."""
from __future__ import annotations

import pytest

from localmail.config import SearchConfig
from localmail.search.arms import arm_bm25_messages
from localmail.search.query import ParsedQuery, SearchFilters


def _seed(conn) -> dict[str, int]:
    """Insert 2 accounts × 3 mailboxes × 2 messages each, return name → PK map."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method)"
            " VALUES ('a1', 'a1@x', 'h', 'password') RETURNING id"
        )
        row = cur.fetchone(); assert row is not None
        a1 = row[0]
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method)"
            " VALUES ('a2', 'a2@x', 'h', 'password') RETURNING id"
        )
        row = cur.fetchone(); assert row is not None
        a2 = row[0]

        cur.execute(
            "INSERT INTO mailboxes (account_id, name) VALUES (%s, 'INBOX') RETURNING id",
            (a1,),
        )
        row = cur.fetchone(); assert row is not None
        mb1 = row[0]
        cur.execute(
            "INSERT INTO mailboxes (account_id, name) VALUES (%s, 'Sent') RETURNING id",
            (a1,),
        )
        row = cur.fetchone(); assert row is not None
        mb2 = row[0]
        cur.execute(
            "INSERT INTO mailboxes (account_id, name) VALUES (%s, 'INBOX') RETURNING id",
            (a2,),
        )
        row = cur.fetchone(); assert row is not None
        mb3 = row[0]

        uid_counter = 1
        for mb_id, acct_id in ((mb1, a1), (mb2, a1), (mb3, a2)):
            for n in (1, 2):
                sha = bytes([mb_id, n, 0]) + bytes(29)
                cur.execute(
                    "INSERT INTO messages"
                    " (account_id, message_id, raw_sha256, raw_bytes, size_bytes,"
                    "  headers, attachments, subject, body_text)"
                    " VALUES (%s, %s, %s, %s, %s, '{}'::jsonb, '[]'::jsonb, %s, %s)"
                    " RETURNING id",
                    (acct_id, f"mb{mb_id}-msg{n}@x", sha, b"raw", 3,
                     "hello world", "hello world"),
                )
                row = cur.fetchone(); assert row is not None
                msg_id = row[0]
                cur.execute(
                    "INSERT INTO message_labels (message_id, mailbox_id, uid)"
                    " VALUES (%s, %s, %s)",
                    (msg_id, mb_id, uid_counter),
                )
                uid_counter += 1
        conn.commit()
    return {"a1": a1, "a2": a2, "mb1": mb1, "mb2": mb2, "mb3": mb3}


def _cfg() -> SearchConfig:
    return SearchConfig()


def _parsed(filters: SearchFilters) -> ParsedQuery:
    return ParsedQuery(free_text="hello", filters=filters)


def test_account_ids_narrows_arm_results(db_conn):
    ids = _seed(db_conn)
    hits = arm_bm25_messages(
        db_conn, _parsed(SearchFilters(account_ids=[ids["a1"]])), _cfg(), limit=10
    )
    assert len({h.message_id for h in hits}) == 4
    for h in hits:
        with db_conn.cursor() as cur:
            cur.execute("SELECT account_id FROM messages WHERE id = %s", (h.message_id,))
            row = cur.fetchone(); assert row is not None
            assert row[0] == ids["a1"]


def test_folder_ids_narrows_arm_results(db_conn):
    ids = _seed(db_conn)
    hits = arm_bm25_messages(
        db_conn, _parsed(SearchFilters(folder_ids=[ids["mb2"]])), _cfg(), limit=10
    )
    assert len({h.message_id for h in hits}) == 2
    for h in hits:
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT EXISTS(SELECT 1 FROM message_labels"
                " WHERE message_id = %s AND mailbox_id = %s)",
                (h.message_id, ids["mb2"]),
            )
            row = cur.fetchone(); assert row is not None and row[0] is True


def test_account_ids_and_folder_ids_combine_with_AND(db_conn):
    ids = _seed(db_conn)
    hits = arm_bm25_messages(
        db_conn,
        _parsed(SearchFilters(account_ids=[ids["a1"]], folder_ids=[ids["mb3"]])),
        _cfg(), limit=10,
    )
    # mb3 belongs to a2; intersection with a1 is empty.
    assert hits == []


def test_no_filter_returns_all(db_conn):
    _seed(db_conn)
    hits = arm_bm25_messages(db_conn, _parsed(SearchFilters()), _cfg(), limit=10)
    assert len({h.message_id for h in hits}) == 6
