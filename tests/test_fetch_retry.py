# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Bounded-hold expiry predicate + bookkeeping IO (#222A)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from localmail.fetch_retry import (
    clear_attempts,
    clear_mailbox,
    hold_expired,
    load_attempts,
    reclaim_below,
    record_attempt,
)

_ABSENT_MAILBOX_ID = -1
_T0 = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)


class TestHoldExpired:
    """A *duration*, not an attempt count — the IDLE thread re-syncs INBOX on
    every notification, so a count would be spent at the mailbox's traffic
    rate rather than at any predictable pace."""

    def test_a_hold_that_just_started_has_not_expired(self):
        assert hold_expired(_T0, _T0, 1800.0) is False

    def test_a_hold_inside_the_window_has_not_expired(self):
        assert hold_expired(_T0, _T0 + timedelta(seconds=1799), 1800.0) is False

    def test_a_hold_at_the_window_has_expired(self):
        assert hold_expired(_T0, _T0 + timedelta(seconds=1800), 1800.0) is True

    def test_a_hold_past_the_window_has_expired(self):
        assert hold_expired(_T0, _T0 + timedelta(hours=9), 1800.0) is True

    @pytest.mark.parametrize("max_hold_s", [0.0, -1.0])
    def test_a_non_positive_window_disables_holding(self, max_hold_s):
        """Opt out entirely: every empty body advances at once."""
        assert hold_expired(_T0, _T0, max_hold_s) is True


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


def test_attempts_accumulate_per_uid_and_keep_the_first_sighting(db_conn):
    mb = _mailbox(db_conn)

    first = record_attempt(db_conn, mailbox_id=mb, uid=7)
    second = record_attempt(db_conn, mailbox_id=mb, uid=7)
    other = record_attempt(db_conn, mailbox_id=mb, uid=8)

    assert (first.attempt_count, second.attempt_count, other.attempt_count) == (1, 2, 1)
    # first_seen_at anchors the expiry window, so it must NOT move on re-sighting.
    assert second.first_seen_at == first.first_seen_at

    loaded = load_attempts(db_conn, mb)
    assert {uid: h.attempt_count for uid, h in loaded.items()} == {7: 2, 8: 1}
    assert loaded[7].first_seen_at == first.first_seen_at


def test_clearing_restarts_the_window(db_conn):
    """The window measures a continuous outage, so recovery must start over."""
    mb = _mailbox(db_conn)
    record_attempt(db_conn, mailbox_id=mb, uid=7)
    record_attempt(db_conn, mailbox_id=mb, uid=7)

    clear_attempts(db_conn, mailbox_id=mb, uid=7)

    assert load_attempts(db_conn, mb) == {}
    assert record_attempt(db_conn, mailbox_id=mb, uid=7).attempt_count == 1


def test_reclaim_below_drops_only_rows_the_watermark_has_passed(db_conn):
    """Rows under the resume point are dead — sync never revisits those UIDs,
    so nothing else would ever clear them and they would accumulate."""
    mb = _mailbox(db_conn)
    for uid in (3, 7, 9):
        record_attempt(db_conn, mailbox_id=mb, uid=uid)

    reclaim_below(db_conn, mailbox_id=mb, uid=7)

    assert sorted(load_attempts(db_conn, mb)) == [7, 9]


def test_clear_mailbox_drops_every_row_for_that_mailbox(db_conn):
    """UIDVALIDITY reset: stale history must not attach to renumbered UIDs."""
    mb = _mailbox(db_conn)
    for uid in (1, 2, 3):
        record_attempt(db_conn, mailbox_id=mb, uid=uid)

    clear_mailbox(db_conn, mb)

    assert load_attempts(db_conn, mb) == {}


def test_a_bookkeeping_failure_does_not_abort_the_surrounding_batch(db_conn):
    """The nested SAVEPOINT's whole purpose.

    Without it the failed INSERT would poison the transaction and take down the
    batch of real messages it was only trying to report on.
    """
    mb = _mailbox(db_conn)
    record_attempt(db_conn, mailbox_id=mb, uid=1)

    # FK violation: no such mailbox.
    assert record_attempt(db_conn, mailbox_id=_ABSENT_MAILBOX_ID, uid=1).attempt_count == 1

    # The transaction must still be usable.
    assert {uid: h.attempt_count for uid, h in load_attempts(db_conn, mb).items()} == {1: 1}
    assert record_attempt(db_conn, mailbox_id=mb, uid=2).attempt_count == 1
