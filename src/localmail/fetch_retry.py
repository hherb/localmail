# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Bounded retry bookkeeping for an empty ``BODY[]`` on FETCH (#222A).

When a FETCH returns no body for a UID that is still on the server, sync holds
its resume watermark so the next run re-fetches it. "Still present" is not
"will ever be fetchable", though -- a zero-length message reads as no-body, and
a corrupt store entry can omit the body indefinitely -- so the hold is bounded
by a per-(mailbox, uid) attempt counter, mirroring `transient_extractions`
(#153). A successful fetch clears the row, making the cap count *consecutive*
failures.

`fetch_budget_exhausted` is the pure boundary; the rest is thin IO.
"""
from __future__ import annotations

import logging

import psycopg

log = logging.getLogger(__name__)

#: Consecutive held attempts before sync gives up on a UID and advances past
#: it. Larger than the poison-pill cap because an unfetchable body is usually
#: genuinely recoverable -- but now bounded. Overridable via
#: `[daemon] max_body_fetch_retries`.
DEFAULT_MAX_BODY_FETCH_RETRIES = 5


def fetch_budget_exhausted(attempt_count: int, cap: int) -> bool:
    """Whether `attempt_count` consecutive holds have used up the budget.

    Matches the shape of `transient_budget_exhausted`. A `cap` of 0 disables
    holding entirely -- every empty body advances immediately, i.e. the
    pre-#222A behaviour but with the WARNING that names it.
    """
    return attempt_count >= cap


def load_attempts(conn: psycopg.Connection, mailbox_id: int) -> dict[int, int]:
    """Every held UID in this mailbox and its consecutive-failure count.

    Read once per mailbox per run so the common path -- a message that fetches
    normally and has no history -- costs no per-message query to clear.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT uid, attempt_count FROM transient_fetches WHERE mailbox_id = %s",
            (mailbox_id,),
        )
        return {int(uid): int(count) for uid, count in cur.fetchall()}


def record_attempt(conn: psycopg.Connection, *, mailbox_id: int, uid: int) -> int:
    """Count one held attempt for `uid`; return its new consecutive total.

    Runs in a nested SAVEPOINT so a bookkeeping failure cannot abort the batch
    it is reporting on -- the same guarantee `record_failed_message` gives, and
    deliberately the same shape: the SAVEPOINT is established *outside* the try,
    so `ROLLBACK TO` in the handler is always valid.

    On failure it reports 1 (this attempt), which keeps sync holding rather than
    giving up on a UID whose real count it could not read.
    """
    with conn.cursor() as cur:
        cur.execute("SAVEPOINT transient_fetch")
        try:
            cur.execute(
                """
                INSERT INTO transient_fetches (mailbox_id, uid, attempt_count)
                VALUES (%s, %s, 1)
                ON CONFLICT (mailbox_id, uid) DO UPDATE SET
                    attempt_count = transient_fetches.attempt_count + 1,
                    last_seen_at  = now()
                RETURNING attempt_count
                """,
                (mailbox_id, uid),
            )
            row = cur.fetchone()
            assert row is not None
            cur.execute("RELEASE SAVEPOINT transient_fetch")
            return int(row[0])
        except Exception:
            cur.execute("ROLLBACK TO SAVEPOINT transient_fetch")
            cur.execute("RELEASE SAVEPOINT transient_fetch")
            log.exception(
                "could not count a held fetch for UID %s in mailbox %s; "
                "treating it as the first attempt",
                uid, mailbox_id,
            )
            return 1


def clear_attempts(conn: psycopg.Connection, *, mailbox_id: int, uid: int) -> None:
    """Drop `uid`'s hold history, so the cap measures consecutive failures."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM transient_fetches WHERE mailbox_id = %s AND uid = %s",
            (mailbox_id, uid),
        )
