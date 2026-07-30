# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Polling endpoint: messages inserted since a cursor."""
from __future__ import annotations

import re
from typing import Any

import psycopg
from fastapi import APIRouter, Depends, Query, Request, Response, status
from pydantic import BaseModel

from localmail.api.acl import allowed_account_ids
from localmail.api.errors import ValidationFailed
from localmail.api.ids import parse_int_id
from localmail.config import ServeConfig
from localmail.serve.middleware import get_authenticated_user

router = APIRouter()

_DEFAULT_LIMIT = 200


class AckRequest(BaseModel):
    subscription: str
    cursor: str


_MAX_SUBSCRIPTION_NAME = 64
_SUBSCRIPTION_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _validate_subscription_name(name: str) -> str:
    """Validate a wire-format subscription name.

    Non-empty, at most `_MAX_SUBSCRIPTION_NAME` chars, `[A-Za-z0-9_-]+` only.
    The charset restriction is about keeping the name a clean log/URL token and
    bounding the row -- every query below passes it as a bound parameter, so it
    carries no SQL-injection burden.
    """
    if (
        not name
        or len(name) > _MAX_SUBSCRIPTION_NAME
        or not _SUBSCRIPTION_NAME_RE.match(name)
    ):
        raise ValidationFailed(
            f"subscription must be 1-{_MAX_SUBSCRIPTION_NAME} chars of "
            f"[A-Za-z0-9_-], got {name!r}"
        )
    return name


def _subscription_cursor(conn: psycopg.Connection, user_id: int, name: str) -> int | None:
    """Stored cursor for (user, name); None when the subscription is new."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT cursor FROM channel_subscriptions WHERE user_id = %s AND name = %s",
            (user_id, name),
        )
        row = cur.fetchone()
    return None if row is None else int(row[0])


def _current_tip(conn: psycopg.Connection, allowed: list[int], horizon_s: float) -> int:
    """Highest visible message id -- where a brand-new subscription starts.

    Applies the same safe-horizon predicate as the `since_id` branch below
    (`date_received < now() - horizon`). Without it, a message whose insert
    transaction hasn't committed/cleared the horizon yet could get a lower
    id than one that already has (commit order != id allocation order); if
    the tip were the raw MAX(id), a fresh subscription's starting cursor
    could land *past* that not-yet-visible message, and no later poll would
    ever return it -- silent, permanent loss, not just delay.

    Runs once per subscription lifetime (first poll only). The ACL + horizon
    predicates make it an index scan over the caller's accounts rather than a
    one-row backward walk, which is why `_max_message_id` -- on the per-ack
    path -- deliberately does not reuse it.
    """
    with conn.cursor() as cur:
        cur.execute(
            """SELECT COALESCE(MAX(id), 0) FROM messages
                WHERE account_id = ANY(%s)
                  AND date_received < now() - make_interval(secs => %s)""",
            (allowed, horizon_s),
        )
        row = cur.fetchone()
    assert row is not None
    return int(row[0])


def _max_message_id(conn: psycopg.Connection) -> int:
    """Upper bound for an acceptable ack cursor.

    Deliberately global -- no ACL and no horizon predicate -- because Postgres
    rewrites `MAX(id)` on the primary key into a one-row `Index Only Scan
    Backward`, so this stays O(1) on a path that runs on *every* ack. The
    ACL-scoped, horizon-filtered `_current_tip` costs an index scan over all of
    the caller's rows, which is fine once per subscription but not per poll.

    A loose bound is all that's needed: it exists to reject a cursor that could
    not have come from any response (a timestamp, a Message-Id, an overflowing
    BIGINT). That case matters because acks are monotonic, so a cursor set past
    the archive would silence the subscription permanently.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT COALESCE(MAX(id), 0) FROM messages")
        row = cur.fetchone()
    assert row is not None
    return int(row[0])


def _claim_subscription(
    conn: psycopg.Connection, user_id: int, name: str, cursor: int
) -> bool:
    """Create the subscription row at `cursor`; True when this call created it.

    `ON CONFLICT DO NOTHING RETURNING` makes the create atomic. Two
    simultaneous first polls of the same new name both read no stored cursor,
    so both reach here; exactly one inserts and the loser gets no row back
    (rather than a `UniqueViolation` escaping as a 500) and re-reads the
    winner's cursor.
    """
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO channel_subscriptions (user_id, name, cursor)
                    VALUES (%s, %s, %s)
               ON CONFLICT (user_id, name) DO NOTHING
               RETURNING cursor""",
            (user_id, name, cursor),
        )
        return cur.fetchone() is not None


def _enforce_subscription_cap(conn: psycopg.Connection, user_id: int, cap: int) -> None:
    """Bound how many subscription rows one api-user can create.

    Both `GET ?subscription=` and `POST /ack` create a row on first use, so a
    client that derives the name from a UUID or timestamp would otherwise grow
    the table without limit. Advisory, not a security boundary: two concurrent
    creates at `cap - 1` can both pass and leave `cap + 1` rows, which is
    harmless for a resource guard and not worth a lock.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM channel_subscriptions WHERE user_id = %s",
            (user_id,),
        )
        row = cur.fetchone()
    assert row is not None
    if int(row[0]) >= cap:
        raise ValidationFailed(
            f"subscription limit reached ({cap} per user); ack or reuse an "
            "existing subscription instead of creating a new name"
        )


@router.get("")
def changes(
    request: Request,
    since: str | None = Query(default=None),
    subscription: str | None = Query(default=None),
    user=Depends(get_authenticated_user),
) -> dict[str, Any]:
    """Return messages whose id > since cursor (or recent if no cursor).

    **Tail-subscription endpoint, not a backfill walk (#38).** Capped at
    ``_DEFAULT_LIMIT`` (200) rows per call. With no ``since`` cursor the
    response is the 200 most recent messages across the caller's allowed
    accounts; with ``since=N`` it is up to 200 messages newer than ``id=N``.
    Clients use this for live polling — initial backfill and "load older"
    pagination go through ``GET /v1/messages`` (keyset cursor on the same
    sort order, no row cap). The spec at
    ``docs/superpowers/specs/2026-05-17-localmail-gui-design.md`` codifies
    the split; do NOT add a ``min_id`` / ``before`` parameter here.

    Filtered to accounts the caller can read (`user_accounts` join). Users
    with no grants get an empty result set and a `next_cursor` of "0" — the
    same shape the v1 client already handles for fresh-account polling.

    Cursor is the highest `messages.id` from the previous response.

    Excludes messages newer than ``now() - serve.changes_safe_horizon_s`` so
    that concurrent sync transactions whose commit order differs from their
    `id` allocation order cannot make the client skip rows. A tx that
    allocates id=N may commit AFTER a later tx that allocated id=N+1 — if we
    returned id=N+1 immediately, the client would advance past N and never
    see it. The horizon trades a few seconds of latency for monotonic
    delivery.

    ``subscription=<name>`` is a server-side alternative to ``since``, so a
    polling client (e.g. kastellan's email channel) can be stateless: poll,
    process, ``POST /v1/changes/ack``. Mutually exclusive with ``since`` --
    mixing a server-side and a client-side cursor is rejected with 400. A
    subscription that has never been acked starts at the *current tip*
    (this call's response is empty) rather than the backlog, so a client
    that subscribes for the first time never replays old mail as new. This
    endpoint never advances a subscription's cursor itself; only
    ``POST /v1/changes/ack`` does.

    First use of a name creates the row, capped at
    ``serve.max_subscriptions_per_user`` names per api-user (400 past the cap).
    """
    if subscription is not None and since is not None:
        raise ValidationFailed("subscription and since are mutually exclusive")

    sub_name = _validate_subscription_name(subscription) if subscription is not None else None
    since_id = None if since is None else parse_int_id(since, field="since cursor")

    serve_cfg: ServeConfig = getattr(request.app.state, "serve_config", None) or ServeConfig()
    horizon_s = serve_cfg.changes_safe_horizon_s
    pool = request.app.state.pool
    new_messages: list[dict[str, Any]] = []
    # Bound before the connection block so the tail of this function can never
    # read it unassigned, whatever early returns get added inside.
    fallback_cursor = since or "0"
    with pool.connection() as conn:
        allowed = allowed_account_ids(conn, user.id)
        if not allowed:
            return {"new_messages": [], "next_cursor": fallback_cursor}

        if sub_name is not None:
            stored_cursor = _subscription_cursor(conn, user.id, sub_name)
            if stored_cursor is None:
                # Brand-new subscription: start at the tip, not the
                # backlog -- see the docstring note above.
                _enforce_subscription_cap(
                    conn, user.id, serve_cfg.max_subscriptions_per_user
                )
                tip = _current_tip(conn, allowed, horizon_s)
                created = _claim_subscription(conn, user.id, sub_name, tip)
                conn.commit()
                if created:
                    return {"new_messages": [], "next_cursor": str(tip)}
                # A concurrent first poll won the insert; adopt its cursor.
                stored_cursor = _subscription_cursor(conn, user.id, sub_name)
                if stored_cursor is None:
                    stored_cursor = tip
            since_id = stored_cursor

        if since_id is not None:
            fallback_cursor = str(since_id)

        with conn.cursor() as cur:
            if since_id is None:
                # Initial-load order:
                # `COALESCE(internal_date, date_sent) DESC NULLS LAST, id DESC`.
                # `internal_date` holds the IMAP server's INTERNALDATE — when
                # the email actually arrived at the mailbox — populated by
                # sync.py going forward and backfillable for legacy rows via
                # `localmail backfill-internal-date`. `date_sent` (header
                # `Date:`) is the fallback for rows not yet backfilled. The
                # empty-query search uses the same ordering so "All Mail" and
                # a cleared search agree. The expression matches the
                # `messages_recent_idx` index (migration 0018) so the planner
                # can use it instead of sorting the whole table.
                cur.execute(
                    """SELECT m.id, m.subject, m.from_addr, m.from_name, m.date_sent,
                              m.internal_date, m.account_id, a.name
                         FROM messages m JOIN accounts a ON a.id = m.account_id
                        WHERE m.date_received < now() - make_interval(secs => %s)
                          AND m.account_id = ANY(%s)
                        ORDER BY COALESCE(m.internal_date, m.date_sent) DESC NULLS LAST, m.id DESC
                        LIMIT %s""",
                    (horizon_s, allowed, _DEFAULT_LIMIT),
                )
            else:
                cur.execute(
                    """SELECT m.id, m.subject, m.from_addr, m.from_name, m.date_sent,
                              m.internal_date, m.account_id, a.name
                         FROM messages m JOIN accounts a ON a.id = m.account_id
                        WHERE m.id > %s
                          AND m.date_received < now() - make_interval(secs => %s)
                          AND m.account_id = ANY(%s)
                        ORDER BY m.id ASC
                        LIMIT %s""",
                    (since_id, horizon_s, allowed, _DEFAULT_LIMIT),
                )
            rows = cur.fetchall()

    max_id = 0
    for row in rows:
        mid, subject, from_addr, from_name, date_sent, internal_date, account_id, account_name = row
        max_id = max(max_id, int(mid))
        # Wire `date` is COALESCE(internal_date, date_sent) — the same
        # expression the initial-load ORDER BY uses. Returning only
        # `date_sent` while sorting by INTERNALDATE made the displayed
        # dates look out of order whenever the two differ.
        received = internal_date or date_sent
        new_messages.append({
            "message_id": str(mid),
            "subject": subject,
            "from": {"address": from_addr, "name": from_name},
            "date": received.isoformat() if received else None,
            "account": {"id": str(account_id), "name": account_name},
        })

    next_cursor = str(max_id) if max_id else fallback_cursor
    return {"new_messages": new_messages, "next_cursor": next_cursor}


@router.post("/ack", status_code=status.HTTP_204_NO_CONTENT)
def ack(
    body: AckRequest,
    request: Request,
    user=Depends(get_authenticated_user),
) -> Response:
    """Advance a subscription's cursor. Monotonic: never rewinds.

    Body: ``{"subscription": "<name>", "cursor": "<id>"}``. Creates the
    subscription row if it doesn't exist yet (a caller may ack before ever
    polling). ``GREATEST`` keeps the update monotonic, so a stale or
    replayed ack can never resurface already-processed messages.

    A cursor above the archive's highest ``messages.id`` is rejected with 400.
    Because the update is monotonic there is no way to walk such a cursor back
    over the API, so an out-of-range ack -- a client sending a timestamp, or a
    value that overflows ``BIGINT`` -- would otherwise silence the
    subscription permanently.
    """
    name = _validate_subscription_name(body.subscription)
    cursor = parse_int_id(body.cursor, field="cursor")
    serve_cfg: ServeConfig = getattr(request.app.state, "serve_config", None) or ServeConfig()
    pool = request.app.state.pool
    with pool.connection() as conn:
        max_id = _max_message_id(conn)
        if cursor > max_id:
            raise ValidationFailed(
                f"cursor {cursor} is past the highest message id ({max_id}); "
                "ack only a cursor returned by GET /v1/changes"
            )
        if _subscription_cursor(conn, user.id, name) is None:
            _enforce_subscription_cap(
                conn, user.id, serve_cfg.max_subscriptions_per_user
            )
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO channel_subscriptions (user_id, name, cursor)
                        VALUES (%s, %s, %s)
                   ON CONFLICT (user_id, name) DO UPDATE
                        SET cursor = GREATEST(channel_subscriptions.cursor, EXCLUDED.cursor),
                            updated_at = now()""",
                (user.id, name, cursor),
            )
        conn.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
