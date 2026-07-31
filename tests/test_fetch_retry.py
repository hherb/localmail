# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Bounded-hold bookkeeping IO (#222A). The pure predicate lives in test_uids.py."""
from __future__ import annotations

from localmail.fetch_retry import clear_attempts, load_attempts, record_attempt

_ABSENT_MAILBOX_ID = -1


def _mailbox(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, auth_method, imap_host, "
            "imap_port, config) VALUES ('a', 'a@b.test', 'password', 'h', 993, '{}') "
            "RETURNING id"
        )
        row = cur.fetchone()
        assert row is not None
        cur.execute(
            "INSERT INTO mailboxes (account_id, name) VALUES (%s, 'INBOX') RETURNING id",
            (row[0],),
        )
        mb = cur.fetchone()
        assert mb is not None
    conn.commit()
    return int(mb[0])


def test_attempts_accumulate_per_uid(db_conn):
    mb = _mailbox(db_conn)

    assert record_attempt(db_conn, mailbox_id=mb, uid=7) == 1
    assert record_attempt(db_conn, mailbox_id=mb, uid=7) == 2
    assert record_attempt(db_conn, mailbox_id=mb, uid=8) == 1

    assert load_attempts(db_conn, mb) == {7: 2, 8: 1}


def test_clearing_resets_the_count(db_conn):
    """The cap measures consecutive failures, so recovery must start over."""
    mb = _mailbox(db_conn)
    record_attempt(db_conn, mailbox_id=mb, uid=7)
    record_attempt(db_conn, mailbox_id=mb, uid=7)

    clear_attempts(db_conn, mailbox_id=mb, uid=7)

    assert load_attempts(db_conn, mb) == {}
    assert record_attempt(db_conn, mailbox_id=mb, uid=7) == 1


def test_a_bookkeeping_failure_does_not_abort_the_surrounding_batch(db_conn):
    """The nested SAVEPOINT's whole purpose.

    Without it the failed INSERT would poison the transaction and take down the
    batch of real messages it was only trying to report on.
    """
    mb = _mailbox(db_conn)
    record_attempt(db_conn, mailbox_id=mb, uid=1)

    # FK violation: no such mailbox.
    assert record_attempt(db_conn, mailbox_id=_ABSENT_MAILBOX_ID, uid=1) == 1

    # The transaction must still be usable.
    assert load_attempts(db_conn, mb) == {1: 1}
    assert record_attempt(db_conn, mailbox_id=mb, uid=2) == 1
