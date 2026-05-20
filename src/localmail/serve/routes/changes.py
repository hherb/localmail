"""Polling endpoint: messages inserted since a cursor."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from localmail.api.acl import allowed_account_ids
from localmail.api.ids import parse_int_id
from localmail.config import ServeConfig
from localmail.serve.middleware import get_authenticated_user

router = APIRouter()

_DEFAULT_LIMIT = 200


@router.get("")
def changes(
    request: Request,
    since: str | None = Query(default=None),
    user=Depends(get_authenticated_user),
) -> dict[str, Any]:
    """Return messages whose id > since cursor (or recent if no cursor).

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
    """
    since_id = None if since is None else parse_int_id(since, field="since cursor")

    serve_cfg: ServeConfig = getattr(request.app.state, "serve_config", None) or ServeConfig()
    horizon_s = serve_cfg.changes_safe_horizon_s
    pool = request.app.state.pool
    new_messages: list[dict[str, Any]] = []
    with pool.connection() as conn:
        allowed = allowed_account_ids(conn, user.id)
        if not allowed:
            return {"new_messages": [], "next_cursor": since or "0"}
        with conn.cursor() as cur:
            if since_id is None:
                # Initial-load order: `date_received DESC, m.id DESC`. `m.id`
                # alone reflects insertion order — a sync that processes an
                # archive folder after the inbox would surface decade-old
                # messages at the top. This matches the canonical default
                # used by the empty-query search fallback so the UI shows the
                # same ordering whether the user is in "All Mail" or in a
                # cleared search.
                cur.execute(
                    """SELECT m.id, m.subject, m.from_addr, m.from_name, m.date_sent,
                              m.account_id, a.name
                         FROM messages m JOIN accounts a ON a.id = m.account_id
                        WHERE m.date_received < now() - make_interval(secs => %s)
                          AND m.account_id = ANY(%s)
                        ORDER BY m.date_received DESC, m.id DESC
                        LIMIT %s""",
                    (horizon_s, allowed, _DEFAULT_LIMIT),
                )
            else:
                cur.execute(
                    """SELECT m.id, m.subject, m.from_addr, m.from_name, m.date_sent,
                              m.account_id, a.name
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
        mid, subject, from_addr, from_name, date_sent, account_id, account_name = row
        max_id = max(max_id, int(mid))
        new_messages.append({
            "message_id": str(mid),
            "subject": subject,
            "from": {"address": from_addr, "name": from_name},
            "date": date_sent.isoformat() if date_sent else None,
            "account": {"id": str(account_id), "name": account_name},
        })

    next_cursor = str(max_id) if max_id else (since or "0")
    return {"new_messages": new_messages, "next_cursor": next_cursor}
