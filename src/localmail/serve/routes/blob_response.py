# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Serving an attachment blob: the #32/#54/#58/#59 response rules.

Shared by every route that addresses a blob — by content hash
(`/v1/attachments/{sha256}`) and by position in a message
(`/v1/messages/{id}/attachments/{index}`). Callers run the ACL probe, then
:func:`not_modified`, then open the file themselves, then hand it to
:func:`blob_response`. The open stays at the call site on purpose: the #62
tests spy on each route module's ``_open_blob_file_at``, and an open moved in
here would make those spies pass whether or not a file was opened.
"""
from __future__ import annotations

import logging
from typing import BinaryIO, Iterator
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import Response, StreamingResponse

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

# Spelled out rather than __name__: the #58 truncation tests listen on it.
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


def not_modified(request: Request, sha256: str) -> Response | None:
    """A 304 when the client's copy is current, else ``None``.

    Run after the ACL probe — so a caller without a grant still sees 404,
    never 304 — and before the file open and any filename lookup (#62).
    The 304 carries only the ``ETag`` (RFC 9110 §15.4.5).
    """
    etag = etag_for_sha256(sha256)
    if if_none_match_satisfies(request.headers.get("if-none-match"), etag):
        return Response(status_code=_HTTP_NOT_MODIFIED, headers={"ETag": etag})
    return None


def blob_response(
    request: Request,
    *,
    sha256: str,
    mime: str,
    size: int,
    fp: BinaryIO,
    filename: str | None,
) -> Response:
    """200 / 206 / 416 for an already-opened, ACL-cleared blob.

    Takes ownership of ``fp``: the streamer closes it, or this function does
    on the 416 branch. ``filename`` falls back to a sha-prefix name when it
    is ``None`` or blank. Every response carries the #32 force-download
    headers and the strong ``ETag``.
    """
    etag = etag_for_sha256(sha256)
    disposition = _content_disposition(
        (filename or "").strip() or _fallback_filename(sha256)
    )
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
