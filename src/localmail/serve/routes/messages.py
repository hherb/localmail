# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Message detail + raw RFC822 routes."""
from __future__ import annotations

from typing import Any, List

from fastapi import APIRouter, Depends, Query, Request, Response

from localmail.api.acl import allowed_account_ids
from localmail.api.attachments import (
    _open_blob_file_at,
    get_attachment_blob_info,
    get_attachment_text_page,
    resolve_message_attachment,
    text_window_from_query,
)
from localmail.api.browse import list_messages
from localmail.api.browse_cursor import decode_browse_cursor
from localmail.api.ids import parse_int_id
from localmail.api.messages import get_message, get_message_raw
from localmail.serve.middleware import get_authenticated_user
from localmail.serve.routes.blob_response import blob_response, not_modified

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
    """One message. `headers` is `compact` (default, no headers key), `full`
    (an object keyed by wire spelling) or `list` (one entry per occurrence, in
    wire order); any other value is a 400.
    """
    mid = parse_int_id(message_id, field="message_id")
    pool = request.app.state.pool
    with pool.connection() as conn:
        allowed = allowed_account_ids(conn, user.id)
        return get_message(
            conn, mid,
            allowed_account_ids=allowed,
            headers=headers,
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


@router.get("/{message_id}/attachments/{index}")
def message_attachment(
    message_id: str,
    index: str,
    request: Request,
    user=Depends(get_authenticated_user),
) -> Response:
    """Attachment ``index`` (0-based, in ``get_message``'s order) of a message.

    The same bytes, Range, ETag and force-download rules as
    ``/v1/attachments/{sha256}``. The difference is the name: the
    ``Content-Disposition`` carries this entry's own filename, where the sha
    route can only pick one of the names a blob is carried under.
    """
    mid = parse_int_id(message_id, field="message_id")
    idx = parse_int_id(index, field="index")
    pool = request.app.state.pool
    with pool.connection() as conn:
        allowed = allowed_account_ids(conn, user.id)
        entry = resolve_message_attachment(
            conn, mid, idx, allowed_account_ids=allowed,
        )
        mime, size, path = get_attachment_blob_info(
            conn, entry.sha256, allowed_account_ids=allowed,
        )
        cached = not_modified(request, entry.sha256)
        if cached is not None:
            return cached
        fp = _open_blob_file_at(path, entry.sha256)
    return blob_response(
        request, sha256=entry.sha256, mime=mime, size=size, fp=fp,
        filename=entry.filename,
    )


@router.get("/{message_id}/attachments/{index}/text")
def message_attachment_text(
    message_id: str,
    index: str,
    request: Request,
    offset: str | None = Query(None),
    limit: str | None = Query(None),
    user=Depends(get_authenticated_user),
) -> dict[str, object]:
    """Extracted text of attachment ``index``, paged by character.

    Every parameter is judged before the connection opens, so a malformed
    request is a 400 even for a caller granted nothing.
    """
    mid = parse_int_id(message_id, field="message_id")
    idx = parse_int_id(index, field="index")
    window = text_window_from_query(offset, limit)
    pool = request.app.state.pool
    with pool.connection() as conn:
        allowed = allowed_account_ids(conn, user.id)
        entry = resolve_message_attachment(
            conn, mid, idx, allowed_account_ids=allowed,
        )
        page = get_attachment_text_page(
            conn, entry.sha256, allowed_account_ids=allowed, window=window,
        )
    return page.to_wire()
