"""Attachment streaming + extracted-text routes."""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from localmail.api.acl import allowed_account_ids
from localmail.api.attachments import (
    get_attachment_filename,
    get_attachment_text,
    open_attachment_bytes,
)
from localmail.serve.middleware import get_authenticated_user

_CHUNK = 64 * 1024

# MIME types that browsers happily render — and execute scripts from —
# when served inline. Content-Disposition: attachment is the primary
# defence (most browsers download instead of rendering), but some still
# sniff, so we clamp these to octet-stream on the wire too. See #32.
_INLINE_RISKY_MIMES = frozenset({
    "text/html",
    "application/xhtml+xml",
    "image/svg+xml",
    "text/xml",
    "application/xml",
})
_SAFE_FALLBACK_MIME = "application/octet-stream"

# Fallback filename when no carrying message has a 'filename' in its JSONB:
# 16 hex chars of the sha is enough to be effectively unique on disk while
# staying short enough to look reasonable in a Save-As dialog.
_SHA_PREFIX_LEN_FOR_FALLBACK_NAME = 16

# Chars that would break the RFC 6266 quoted-string `filename=` form, or
# that ASCII clients commonly mis-handle in download dialogs. The full
# UTF-8 original is still recoverable from the `filename*=UTF-8''…` form.
_QUOTED_STRING_UNSAFE = frozenset('"\\\r\n;,')
_PRINTABLE_ASCII_MIN = 32
_PRINTABLE_ASCII_MAX = 126


def _ascii_fallback_name(name: str) -> str:
    """Sanitise a filename for the legacy quoted-string `filename=` form."""
    out = []
    for ch in name:
        code = ord(ch)
        if (
            code < _PRINTABLE_ASCII_MIN
            or code > _PRINTABLE_ASCII_MAX
            or ch in _QUOTED_STRING_UNSAFE
        ):
            out.append("_")
        else:
            out.append(ch)
    sanitised = "".join(out).strip()
    return sanitised or "attachment"


def _content_disposition(name: str) -> str:
    """Build an RFC 6266 / 5987 Content-Disposition: attachment value."""
    ascii_name = _ascii_fallback_name(name)
    encoded = quote(name, safe="")
    return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{encoded}'


def _safe_response_mime(stored: str) -> str:
    return _SAFE_FALLBACK_MIME if stored.lower() in _INLINE_RISKY_MIMES else stored


def _fallback_filename(sha256_hex: str) -> str:
    return f"attachment-{sha256_hex[:_SHA_PREFIX_LEN_FOR_FALLBACK_NAME]}.bin"


router = APIRouter()


@router.get("/{sha256}")
def stream_blob(
    sha256: str,
    request: Request,
    user=Depends(get_authenticated_user),
) -> StreamingResponse:
    pool = request.app.state.pool
    with pool.connection() as conn:
        allowed = allowed_account_ids(conn, user.id)
        fp, mime, size = open_attachment_bytes(
            conn, sha256, allowed_account_ids=allowed,
        )
        original = get_attachment_filename(
            conn, sha256, allowed_account_ids=allowed,
        )
    filename = (original or "").strip() or _fallback_filename(sha256)

    def gen():
        try:
            while chunk := fp.read(_CHUNK):
                yield chunk
        finally:
            fp.close()

    return StreamingResponse(
        gen(),
        media_type=_safe_response_mime(mime),
        headers={
            "Content-Length": str(size),
            "Content-Disposition": _content_disposition(filename),
            "Accept-Ranges": "none",
        },
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
