"""Attachment streaming + extracted-text routes."""
from __future__ import annotations

import logging
from typing import BinaryIO, Iterator
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response, StreamingResponse

from localmail.api.acl import allowed_account_ids
from localmail.api.attachments import (
    get_attachment_blob_info,
    get_attachment_filename,
    get_attachment_text,
    open_attachment_bytes,
)
from localmail.api.conditional import (
    etag_for_sha256,
    if_none_match_satisfies,
    if_range_allows_partial,
)
from localmail.api.range_requests import (
    ByteRange,
    UnsatisfiableRange,
    content_range_header,
    parse_byte_range,
    unsatisfiable_content_range,
)
from localmail.serve.middleware import get_authenticated_user

logger = logging.getLogger("localmail.serve")

_CHUNK = 64 * 1024
_HTTP_NOT_MODIFIED = 304
_HTTP_PARTIAL_CONTENT = 206
_HTTP_RANGE_NOT_SATISFIABLE = 416

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


def _log_truncation(sha256_hex: str, expected: int, sent: int) -> None:
    """Surface a short-read against the DB-recorded blob size.

    By the time the streamer notices, headers are already flushed and the
    client sees a stalled / prematurely-closed connection. We can't fix the
    response — only flag it for ops to investigate (#58).
    """
    logger.warning(
        "attachment stream truncated: sha256=%s expected=%d sent=%d",
        sha256_hex, expected, sent,
    )


def _stream_full(fp: BinaryIO, sha256_hex: str, expected: int) -> Iterator[bytes]:
    """Iterate the rest of ``fp`` in fixed chunks, closing on exit.

    Logs a WARNING if the file ends before ``expected`` bytes have been
    sent — i.e. on-disk blob is shorter than ``attachment_blobs.size_bytes``.
    """
    sent = 0
    try:
        while chunk := fp.read(_CHUNK):
            sent += len(chunk)
            yield chunk
        if sent < expected:
            _log_truncation(sha256_hex, expected, sent)
    finally:
        fp.close()


def _stream_range(
    fp: BinaryIO, byte_range: ByteRange, sha256_hex: str,
) -> Iterator[bytes]:
    """Iterate exactly the bytes covered by ``byte_range``, closing on exit.

    Seeks once to ``byte_range.start`` then reads in ``_CHUNK``-sized blocks
    without slurping the whole blob into memory. Logs a WARNING if the file
    runs short of the requested slice length.
    """
    sent = 0
    try:
        fp.seek(byte_range.start)
        remaining = byte_range.length
        while remaining > 0:
            chunk = fp.read(min(_CHUNK, remaining))
            if not chunk:
                _log_truncation(sha256_hex, byte_range.length, sent)
                break
            remaining -= len(chunk)
            sent += len(chunk)
            yield chunk
    finally:
        fp.close()


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
        mime, size = get_attachment_blob_info(
            conn, sha256, allowed_account_ids=allowed,
        )
        etag = etag_for_sha256(sha256)
        if if_none_match_satisfies(request.headers.get("if-none-match"), etag):
            return Response(status_code=_HTTP_NOT_MODIFIED, headers={"ETag": etag})
        fp, _mime, _size = open_attachment_bytes(
            conn, sha256, allowed_account_ids=allowed,
        )
        original = get_attachment_filename(
            conn, sha256, allowed_account_ids=allowed,
        )
    filename = (original or "").strip() or _fallback_filename(sha256)
    disposition = _content_disposition(filename)
    response_mime = _safe_response_mime(mime)

    range_header = request.headers.get("range")
    if range_header is not None and not if_range_allows_partial(
        request.headers.get("if-range"), etag,
    ):
        range_header = None

    try:
        byte_range = parse_byte_range(range_header, size)
    except UnsatisfiableRange:
        fp.close()
        return Response(
            status_code=_HTTP_RANGE_NOT_SATISFIABLE,
            media_type=response_mime,
            headers={
                "Content-Range": unsatisfiable_content_range(size),
                "Content-Disposition": disposition,
                "Accept-Ranges": "bytes",
                "ETag": etag,
            },
        )

    if byte_range is None:
        return StreamingResponse(
            _stream_full(fp, sha256, size),
            media_type=response_mime,
            headers={
                "Content-Length": str(size),
                "Content-Disposition": disposition,
                "Accept-Ranges": "bytes",
                "ETag": etag,
            },
        )

    return StreamingResponse(
        _stream_range(fp, byte_range, sha256),
        status_code=_HTTP_PARTIAL_CONTENT,
        media_type=response_mime,
        headers={
            "Content-Length": str(byte_range.length),
            "Content-Range": content_range_header(byte_range, size),
            "Content-Disposition": disposition,
            "Accept-Ranges": "bytes",
            "ETag": etag,
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
