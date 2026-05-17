"""Message detail + raw RFC822 routes."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response

from localmail.api.messages import get_message, get_message_raw
from localmail.serve.middleware import get_authenticated_user

router = APIRouter()


@router.get("/{message_id}")
def detail(
    message_id: int,
    request: Request,
    headers: str = Query("compact"),
    _user=Depends(get_authenticated_user),
) -> dict[str, Any]:
    pool = request.app.state.pool
    with pool.connection() as conn:
        return get_message(conn, message_id, full_headers=(headers == "full"))


@router.get("/{message_id}/raw")
def raw(
    message_id: int,
    request: Request,
    _user=Depends(get_authenticated_user),
) -> Response:
    pool = request.app.state.pool
    with pool.connection() as conn:
        body = get_message_raw(conn, message_id)
    return Response(content=body, media_type="message/rfc822")
