# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""UID numbering: synthetic allocation for archive imports + resume-watermark
arithmetic for IMAP sync (#215, #222A).

`message_labels` carries ``UNIQUE (mailbox_id, uid)``. For IMAP-sourced mail the
UID is the server's truth; for archive imports it is invented here. The stored
uid has exactly one reader -- `sync.backfill_internal_date`, which uses it as an
IMAP FETCH key; every read surface (search, browse, account listing, message
fetch) keys on ``mailbox_id`` alone. Re-allocating a synthetic UID is therefore
safe repair only because the re-allocation and the reader can never meet:
`should_reallocate_uid` is gated on the archive auth method, archive accounts
carry no `imap_host`, and `backfill-internal-date` requires a live connection.
Widening that gate to a live account would make `backfill_internal_date` FETCH a
synthetic UID against the real server and write another message's INTERNALDATE
onto this row -- re-check both claims before touching it.

Everything is pure except `max_label_uid`, which is the one thin DB read.
"""
from __future__ import annotations

import psycopg

#: `accounts.auth_method` for a non-IMAP archive account. Its UIDs are synthetic.
ARCHIVE_AUTH_METHOD = "archive"


def next_uid_after(max_uid: int | None) -> int:
    """First free UID in a mailbox whose highest stored UID is `max_uid`.

    `None` is treated as an empty mailbox -- `max_label_uid` COALESCEs, so this
    only guards a caller that passes a raw NULL.
    """
    return (max_uid or 0) + 1


def should_reallocate_uid(auth_method: str) -> bool:
    """Whether a failed message's stored UID may be replaced on retry.

    Synthetic UIDs (archive imports) are re-allocated: replaying one that a
    later import already consumed collides on ``UNIQUE (mailbox_id, uid)``
    forever. Real IMAP UIDs are preserved verbatim -- they identify the message
    on the server and a collision there would signal a genuine invariant
    violation worth surfacing rather than papering over.
    """
    return auth_method == ARCHIVE_AUTH_METHOD


def checkpoint_uidnext(highest_seen: int, hold_at: int | None) -> int:
    """Resume point to checkpoint into ``mailboxes.uidnext``.

    Normally one past the highest UID processed. When a UID was held back (an
    empty ``BODY[]`` that is still present on the server -- transient, #222A),
    the resume point is clamped to that UID so the next run re-fetches it.
    The clamp is required because `highest_seen` is a running max: a later UID
    in the same run would otherwise carry the watermark past the stuck one.
    """
    resume = highest_seen + 1
    if hold_at is None:
        return resume
    return min(resume, hold_at)


def max_label_uid(conn: psycopg.Connection, mailbox_id: int) -> int:
    """Highest UID stored for `mailbox_id`, or 0 when the mailbox is empty."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(MAX(uid), 0) FROM message_labels WHERE mailbox_id = %s",
            (mailbox_id,),
        )
        row = cur.fetchone()
        assert row is not None
        return int(row[0])
