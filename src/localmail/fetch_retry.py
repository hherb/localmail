# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Bounded retry bookkeeping for an empty ``BODY[]`` on FETCH (#222A).

When a FETCH returns no body for a UID that is still on the server, sync holds
its resume watermark so the next run re-fetches it. "Still present" is not
"will ever be fetchable", though -- a zero-length message reads as no-body, and
a corrupt store entry can omit the body indefinitely -- so a hold expires after
`[daemon] max_body_fetch_hold_s`, tracked per `(mailbox_id, uid)`. A successful
fetch clears the row, so the window measures one *continuous* outage.

`hold_expired` is the pure boundary; the rest is thin IO.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import NamedTuple

import psycopg

log = logging.getLogger(__name__)

#: How long sync keeps re-trying a UID whose body it cannot fetch before giving
#: up and advancing past it. Overridable via `[daemon] max_body_fetch_hold_s`.
DEFAULT_MAX_BODY_FETCH_HOLD_S = 1800.0


class HoldState(NamedTuple):
    """A UID's hold history: how many times, and since when."""

    attempt_count: int
    first_seen_at: datetime


def hold_expired(first_seen_at: datetime, now: datetime, max_hold_s: float) -> bool:
    """Whether a hold that began at `first_seen_at` has run out of time.

    **A duration, deliberately, not an attempt count.** The obvious analogue
    would be `transient_extractions`' consecutive-failure cap (#153), but that
    counter is driven by a timer-paced sweep, so there a count *is* a duration.
    Here the pace is event-driven: `idle.py::_sync_inbox` runs on *every* IDLE
    notification -- a new message, an EXPUNGE, another client toggling a flag --
    so a count would be spent at the mailbox's traffic rate. Five unrelated
    notifications in ten seconds would exhaust a 5-attempt budget and drop a
    message over a blip that resolved a minute later, while the poll plane
    (`poll_seconds` apart) got twenty-five minutes from the same number. A
    duration behaves identically on both planes and means to an operator what it
    says.

    Nor would a count bound the re-fetch traffic: that comes from holding the
    watermark, which happens per sync pass regardless of whether the pass is
    counted. Only the elapsed hold bounds it.

    `max_hold_s <= 0` disables holding entirely -- every empty body advances at
    once, i.e. the pre-#222A behaviour but with the WARNING that names it.
    """
    if max_hold_s <= 0:
        return True
    return (now - first_seen_at).total_seconds() >= max_hold_s


def load_attempts(conn: psycopg.Connection, mailbox_id: int) -> dict[int, HoldState]:
    """Every held UID in this mailbox with its count and first-sighting time.

    Read once per mailbox per run so the common path -- a message that fetches
    normally and has no history -- costs no per-message query to clear.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT uid, attempt_count, first_seen_at FROM transient_fetches "
            "WHERE mailbox_id = %s",
            (mailbox_id,),
        )
        return {
            int(uid): HoldState(attempt_count=int(count), first_seen_at=first_seen)
            for uid, count, first_seen in cur.fetchall()
        }


def record_attempt(
    conn: psycopg.Connection, *, mailbox_id: int, uid: int
) -> HoldState:
    """Count one held attempt for `uid`; return its new consecutive total.

    Runs in a nested SAVEPOINT so a bookkeeping failure cannot abort the batch
    it is reporting on -- the same guarantee `record_failed_message` gives, and
    deliberately the same shape: the SAVEPOINT is established *outside* the try,
    so `ROLLBACK TO` in the handler is always valid.

    On failure it reports this sighting as a fresh hold starting now, which
    keeps sync holding rather than giving up on a history it could not read.
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
                RETURNING attempt_count, first_seen_at
                """,
                (mailbox_id, uid),
            )
            row = cur.fetchone()
            assert row is not None
            cur.execute("RELEASE SAVEPOINT transient_fetch")
            return HoldState(attempt_count=int(row[0]), first_seen_at=row[1])
        except Exception:
            cur.execute("ROLLBACK TO SAVEPOINT transient_fetch")
            cur.execute("RELEASE SAVEPOINT transient_fetch")
            log.exception(
                "could not record a held fetch for UID %s in mailbox %s; "
                "treating it as a hold starting now",
                uid, mailbox_id,
            )
            return HoldState(attempt_count=1, first_seen_at=db_now(conn))


def clear_attempts(conn: psycopg.Connection, *, mailbox_id: int, uid: int) -> None:
    """Drop `uid`'s hold history, so the window measures a *continuous* outage."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM transient_fetches WHERE mailbox_id = %s AND uid = %s",
            (mailbox_id, uid),
        )


def reclaim_below(conn: psycopg.Connection, *, mailbox_id: int, uid: int) -> None:
    """Drop hold history for UIDs the resume watermark has already passed.

    Such a row is dead by construction: sync will never look at that UID again,
    so nothing would ever clear or expire it. Without this they accumulate
    without bound. The population that lands here is UIDs which really were
    expunged but got recorded as held anyway, because the probe is skipped once
    the run knows the server is emptying bodies.

    **Tombstoned rows are exempt (#239).** The watermark passing a given-up UID
    is precisely the moment its record has to survive -- that record is the only
    trace of a permanently lost message, and `list-failed-fetches` /
    `retry-failed-fetches` are the commands that act on it. Purging is manual
    (`purge_gave_up`). Expiry stays sticky for re-sightings because `mark_gave_up`
    never restamps `gave_up_at`, not because the row gets collected.
    """
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM transient_fetches "
            "WHERE mailbox_id = %s AND uid < %s AND gave_up_at IS NULL",
            (mailbox_id, uid),
        )


def clear_mailbox(conn: psycopg.Connection, mailbox_id: int) -> None:
    """Drop all hold history for a mailbox (UIDVALIDITY reset).

    The UID space is being renumbered, so a surviving row would attach a stale
    history to an unrelated new message -- one already near its expiry could
    make sync give up on that message immediately.
    """
    with conn.cursor() as cur:
        cur.execute("DELETE FROM transient_fetches WHERE mailbox_id = %s", (mailbox_id,))


def db_now(conn: psycopg.Connection) -> datetime:
    """Server clock, so held timestamps are all comparable to `first_seen_at`."""
    with conn.cursor() as cur:
        cur.execute("SELECT now()")
        row = cur.fetchone()
        assert row is not None
        return row[0]


# --- give-up tombstones (#239) ---------------------------------------------


class GaveUpFetch(NamedTuple):
    """One permanently-unfetchable UID, as `list-failed-fetches` reports it."""

    account_name: str
    mailbox_name: str
    mailbox_id: int
    uid: int
    attempt_count: int
    first_seen_at: datetime
    gave_up_at: datetime


def plan_uidnext_rewind(
    tombstones: Iterable[tuple[int, int]],
    current_uidnext: Mapping[int, int],
) -> dict[int, int]:
    """Map each mailbox to the resume point that re-reaches its given-up UIDs.

    Pure. `tombstones` is `(mailbox_id, uid)` pairs; `current_uidnext` is each
    mailbox's stored watermark. A mailbox appears in the result only when the
    rewind actually moves the watermark *backwards* -- rewinding to a UID sync
    has not reached yet would skip everything in between.

    Re-scanning from the lowest given-up UID upward is safe rather than merely
    tolerable: `upsert_message`'s existing-id check plus `ON CONFLICT DO NOTHING`
    make every already-archived message in that range a no-op.
    """
    lowest: dict[int, int] = {}
    for mailbox_id, uid in tombstones:
        if mailbox_id not in current_uidnext:
            continue
        lowest[mailbox_id] = min(uid, lowest.get(mailbox_id, uid))
    return {
        mailbox_id: uid
        for mailbox_id, uid in lowest.items()
        if uid < current_uidnext[mailbox_id]
    }


def mark_gave_up(conn: psycopg.Connection, *, mailbox_id: int, uid: int) -> None:
    """Tombstone `uid`: sync will never fetch it, and the operator should know.

    Idempotent on the *moment*. An expired UID stays reachable while any lower
    UID holds the watermark, so it is re-seen on every pass; restamping would
    make `gave_up_at` read as "just now" forever and hide how long the message
    has been missing.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE transient_fetches SET gave_up_at = COALESCE(gave_up_at, now()) "
            "WHERE mailbox_id = %s AND uid = %s",
            (mailbox_id, uid),
        )


_GAVE_UP_SELECT = """
    SELECT a.name, m.name, t.mailbox_id, t.uid, t.attempt_count,
           t.first_seen_at, t.gave_up_at
    FROM transient_fetches t
    JOIN mailboxes m ON m.id = t.mailbox_id
    JOIN accounts  a ON a.id = m.account_id
    WHERE t.gave_up_at IS NOT NULL
"""


def list_gave_up(
    conn: psycopg.Connection,
    *,
    account_name: str | None = None,
    limit: int | None = None,
) -> list[GaveUpFetch]:
    """Every tombstoned UID, newest give-up first."""
    sql = _GAVE_UP_SELECT
    params: list[object] = []
    if account_name is not None:
        sql += " AND a.name = %s"
        params.append(account_name)
    sql += " ORDER BY t.gave_up_at DESC, t.mailbox_id, t.uid"
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [GaveUpFetch(*row) for row in cur.fetchall()]


def purge_gave_up(
    conn: psycopg.Connection,
    *,
    account_name: str | None = None,
    older_than_s: float | None = None,
) -> int:
    """Delete tombstones; return how many. Live holds are never touched.

    Retention is this call, not a background sweep. A tombstone is written once
    per distinct unfetchable UID and upserted after that, so the table's growth
    is bounded by the number of genuinely broken messages. Expiring rows
    automatically would trade that for silently discarding the only record of
    permanently lost mail -- exactly the gap #239 closed. `failed_messages` and
    `failed_extractions` make the same call.
    """
    sql = (
        "DELETE FROM transient_fetches t"
        " USING mailboxes m, accounts a"
        " WHERE m.id = t.mailbox_id AND a.id = m.account_id"
        " AND t.gave_up_at IS NOT NULL"
    )
    params: list[object] = []
    if account_name is not None:
        sql += " AND a.name = %s"
        params.append(account_name)
    if older_than_s is not None:
        sql += " AND t.gave_up_at < now() - make_interval(secs => %s)"
        params.append(older_than_s)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.rowcount
