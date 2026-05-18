"""Attachment streaming + extracted-text routes."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from localmail.api.attachments import get_attachment_text, open_attachment_bytes
from localmail.serve.middleware import get_authenticated_user

_CHUNK = 64 * 1024

router = APIRouter()


@router.get("/{sha256}")
def stream_blob(
    sha256: str,
    request: Request,
    _user=Depends(get_authenticated_user),
) -> StreamingResponse:
    pool = request.app.state.pool
    with pool.connection() as conn:
        fp, mime, size = open_attachment_bytes(conn, sha256)

    def gen():
        try:
            while chunk := fp.read(_CHUNK):
                yield chunk
        finally:
            fp.close()

    return StreamingResponse(
        gen(),
        media_type=mime,
        headers={"Content-Length": str(size)},
    )


@router.get("/{sha256}/text")
def attachment_text(
    sha256: str,
    request: Request,
    _user=Depends(get_authenticated_user),
) -> dict[str, str]:
    pool = request.app.state.pool
    with pool.connection() as conn:
        text = get_attachment_text(conn, sha256)
    return {"text": text}
