"""Polling endpoint: messages inserted since a cursor."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from localmail.serve.middleware import get_authenticated_user

router = APIRouter()

_DEFAULT_LIMIT = 200


@router.get("")
def changes(
    request: Request,
    since: str | None = Query(default=None),
    _user=Depends(get_authenticated_user),
) -> dict[str, Any]:
    """Return messages whose id > since cursor (or recent if no cursor).

    Cursor is the highest `messages.id` from the previous response.
    """
    pool = request.app.state.pool
    new_messages: list[dict[str, Any]] = []
    with pool.connection() as conn, conn.cursor() as cur:
        if since is None:
            cur.execute(
                """SELECT m.id, m.subject, m.from_addr, m.from_name, m.date_sent,
                          m.account_id, a.name
                     FROM messages m JOIN accounts a ON a.id = m.account_id
                    ORDER BY m.id DESC
                    LIMIT %s""",
                (_DEFAULT_LIMIT,),
            )
        else:
            try:
                since_id = int(since)
            except ValueError:
                since_id = 0
            cur.execute(
                """SELECT m.id, m.subject, m.from_addr, m.from_name, m.date_sent,
                          m.account_id, a.name
                     FROM messages m JOIN accounts a ON a.id = m.account_id
                    WHERE m.id > %s
                    ORDER BY m.id ASC
                    LIMIT %s""",
                (since_id, _DEFAULT_LIMIT),
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
