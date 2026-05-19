"""Message detail + raw RFC822 routes."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response

from localmail.api.acl import allowed_account_ids
from localmail.api.ids import parse_int_id
from localmail.api.messages import get_message, get_message_raw
from localmail.serve.middleware import get_authenticated_user

router = APIRouter()


@router.get("/{message_id}")
def detail(
    message_id: str,
    request: Request,
    headers: str = Query("compact"),
    user=Depends(get_authenticated_user),
) -> dict[str, Any]:
    mid = parse_int_id(message_id, field="message_id")
    pool = request.app.state.pool
    with pool.connection() as conn:
        allowed = allowed_account_ids(conn, user.id)
        return get_message(
            conn, mid,
            allowed_account_ids=allowed,
            full_headers=(headers == "full"),
        )


@router.get("/{message_id}/raw")
def raw(
    message_id: str,
    request: Request,
    user=Depends(get_authenticated_user),
) -> Response:
    mid = parse_int_id(message_id, field="message_id")
    pool = request.app.state.pool
    with pool.connection() as conn:
        allowed = allowed_account_ids(conn, user.id)
        body = get_message_raw(conn, mid, allowed_account_ids=allowed)
    return Response(content=body, media_type="message/rfc822")
