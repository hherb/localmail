# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Polling endpoint: messages inserted since a cursor."""
from __future__ import annotations

import re
from typing import Any

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


def _validate_subscription_name(name: object) -> str:
    """Validate a wire-format subscription name.

    Non-empty, at most `_MAX_SUBSCRIPTION_NAME` chars, `[A-Za-z0-9_-]+` only
    -- keeps it safe to use as a plain SQL parameter and a log/URL token.
    """
    if not isinstance(name, str) or not name or len(name) > _MAX_SUBSCRIPTION_NAME \
            or not _SUBSCRIPTION_NAME_RE.match(name):
        raise ValidationFailed(
            f"subscription must be 1-{_MAX_SUBSCRIPTION_NAME} chars of "
            f"[A-Za-z0-9_-], got {name!r}"
        )
    return name


def _subscription_cursor(conn, user_id: int, name: str) -> int | None:
    """Stored cursor for (user, name); None when the subscription is new."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT cursor FROM channel_subscriptions WHERE user_id = %s AND name = %s",
            (user_id, name),
        )
        row = cur.fetchone()
    return None if row is None else int(row[0])


def _current_tip(conn, allowed: list[int], horizon_s: float) -> int:
    """Highest visible message id -- where a brand-new subscription starts.

    Applies the same safe-horizon predicate as the `since_id` branch below
    (`date_received < now() - horizon`). Without it, a message whose insert
    transaction hasn't committed/cleared the horizon yet could get a lower
    id than one that already has (commit order != id allocation order); if
    the tip were the raw MAX(id), a fresh subscription's starting cursor
    could land *past* that not-yet-visible message, and no later poll would
    ever return it -- silent, permanent loss, not just delay.
    """
    with conn.cursor() as cur:
        cur.execute(
            """SELECT COALESCE(MAX(id), 0) FROM messages
                WHERE account_id = ANY(%s)
                  AND date_received < now() - make_interval(secs => %s)""",
            (allowed, horizon_s),
        )
        return int(cur.fetchone()[0])


def _create_subscription(conn, user_id: int, name: str, cursor: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO channel_subscriptions (user_id, name, cursor) VALUES (%s, %s, %s)",
            (user_id, name, cursor),
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
    """
    if subscription is not None and since is not None:
        raise ValidationFailed("subscription and since are mutually exclusive")

    sub_name = _validate_subscription_name(subscription) if subscription is not None else None
    since_id = None if since is None else parse_int_id(since, field="since cursor")

    serve_cfg: ServeConfig = getattr(request.app.state, "serve_config", None) or ServeConfig()
    horizon_s = serve_cfg.changes_safe_horizon_s
    pool = request.app.state.pool
    new_messages: list[dict[str, Any]] = []
    with pool.connection() as conn:
        allowed = allowed_account_ids(conn, user.id)
        if not allowed:
            return {"new_messages": [], "next_cursor": since or "0"}

        if sub_name is not None:
            stored_cursor = _subscription_cursor(conn, user.id, sub_name)
            if stored_cursor is None:
                # Brand-new subscription: start at the tip, not the
                # backlog -- see the docstring note above.
                tip = _current_tip(conn, allowed, horizon_s)
                _create_subscription(conn, user.id, sub_name, tip)
                conn.commit()
                return {"new_messages": [], "next_cursor": str(tip)}
            since_id = stored_cursor

        fallback_cursor = since if since is not None else (
            str(since_id) if since_id is not None else "0"
        )

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
    """
    name = _validate_subscription_name(body.subscription)
    cursor = parse_int_id(body.cursor, field="cursor")
    pool = request.app.state.pool
    with pool.connection() as conn:
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
