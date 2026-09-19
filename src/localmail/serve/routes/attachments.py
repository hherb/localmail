# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Attachment streaming + extracted-text routes, addressed by content hash."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from localmail.api.acl import allowed_account_ids
from localmail.api.attachments import (
    _open_blob_file_at,
    get_attachment_blob_info,
    get_attachment_filename,
    get_attachment_text,
)
from localmail.serve.middleware import get_authenticated_user
from localmail.serve.routes.blob_response import blob_response, not_modified

router = APIRouter()


@router.get("/{sha256}")
def stream_blob(
    sha256: str,
    request: Request,
    user=Depends(get_authenticated_user),
) -> Response:
    """Stream an attachment blob, optionally honouring ``Range: bytes=…``.

    Full GET returns 200 with the entire blob and ``Accept-Ranges: bytes``.
    A satisfiable Range header returns 206 Partial Content with a
    ``Content-Range`` header and exactly the requested slice. A valid-but-
    unsatisfiable Range (start past EOF, suffix of zero, etc.) returns 416
    with ``Content-Range: bytes */<size>``. Unparseable Range headers fall
    through to a full 200 (RFC 9110 §14.1.2 permissive branch).

    Every response — including 416 — carries the same #32 force-download
    headers: ``Content-Disposition: attachment`` with both ASCII and RFC 5987
    filename forms, and a clamped MIME for script-executable types.

    Conditional GET (#59): every 200/206/304/416 advertises a strong
    ``ETag: "<sha256-hex>"``. ``If-None-Match`` (weak compare, ``*``
    accepted) shortcuts to 304 with no body and only the ``ETag``
    header (§15.4.5 representation-metadata rules — no
    Content-Disposition / Accept-Ranges on 304). ``If-Range`` (strong
    compare only) on a request that also carries ``Range`` either lets
    the partial proceed or — on mismatch — falls back to a full 200 so
    a resumed download cannot stitch two distinct representations
    together.
    """
    pool = request.app.state.pool
    with pool.connection() as conn:
        allowed = allowed_account_ids(conn, user.id)
        mime, size, path = get_attachment_blob_info(
            conn, sha256, allowed_account_ids=allowed,
        )
        cached = not_modified(request, sha256)
        if cached is not None:
            return cached
        fp = _open_blob_file_at(path, sha256)
        original = get_attachment_filename(
            conn, sha256, allowed_account_ids=allowed,
        )
    return blob_response(
        request, sha256=sha256, mime=mime, size=size, fp=fp, filename=original,
    )


@router.get("/{sha256}/text")
def attachment_text(
    sha256: str,
    request: Request,
    user=Depends(get_authenticated_user),
) -> dict[str, str]:
    pool = request.app.state.pool
    with pool.connection() as conn:
        allowed = allowed_account_ids(conn, user.id)
        text = get_attachment_text(conn, sha256, allowed_account_ids=allowed)
    return {"text": text}
