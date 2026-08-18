# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Message detail + raw RFC822 routes."""
from __future__ import annotations

from typing import Any, List

from fastapi import APIRouter, Depends, Query, Request, Response

from localmail.api.acl import allowed_account_ids
from localmail.api.browse import list_messages
from localmail.api.browse_cursor import decode_browse_cursor
from localmail.api.ids import parse_int_id
from localmail.api.messages import get_message, get_message_raw
from localmail.serve.middleware import get_authenticated_user

router = APIRouter()


@router.get("")
def browse(
    request: Request,
    account_id: List[str] = Query(default_factory=list),
    folder_id: List[str] = Query(default_factory=list),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
    user=Depends(get_authenticated_user),
) -> dict[str, Any]:
    """Keyset-paginated browse of messages, newest first.

    **Canonical browse / backfill endpoint (#38).** Clients use this for
    initial mail-list load and "load older" pagination. Sort order is
    ``COALESCE(internal_date, date_sent) DESC NULLS LAST, id DESC``,
    matching ``/v1/changes`` so a client that mixes the two renders rows
    identically. Unbounded scroll (no row cap, no ``since`` analogue);
    live polling for newly-arrived mail goes through ``GET /v1/changes``.

    `account_id` / `folder_id` are repeatable query parameters and intersect
    with the caller's ACL grants at the service-layer SQL boundary.
    """
    # Validate cursor eagerly so a malformed token always yields 400,
    # even when the caller has no ACL grants (list_messages short-circuits
    # before reaching decode_browse_cursor when allowed_account_ids is empty).
    if cursor is not None:
        decode_browse_cursor(cursor)
    parsed_account_ids = [parse_int_id(v, field="account_id") for v in account_id]
    parsed_folder_ids = [parse_int_id(v, field="folder_id") for v in folder_id]
    pool = request.app.state.pool
    with pool.connection() as conn:
        allowed = allowed_account_ids(conn, user.id)
        return list_messages(
            conn,
            allowed_account_ids=allowed,
            account_ids=parsed_account_ids or None,
            folder_ids=parsed_folder_ids or None,
            limit=limit,
            cursor=cursor,
        )


@router.get("/{message_id}")
def detail(
    message_id: str,
    request: Request,
    headers: str = Query("compact"),
    external_images: bool = Query(False),
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
            allow_external_images=external_images,
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
