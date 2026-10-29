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


def _safe_response_mime(stored: str | None) -> str:
    # `attachment_blobs.mime_type` is nullable; no type is the safe type.
    if not stored or stored.lower() in _INLINE_RISKY_MIMES:
        return _SAFE_FALLBACK_MIME
    return stored


def _fallback_filename(sha256_hex: str) -> str:
    return f"attachment-{sha256_hex[:_SHA_PREFIX_LEN_FOR_FALLBACK_NAME]}.bin"


def _log_truncation(where: str, expected: int, sent: int) -> None:
    """Surface a short-read against the DB-recorded blob size.

    By the time the streamer notices, headers are already flushed and the
    client sees a stalled / prematurely-closed connection. We can't fix the
    response — only flag it for ops to investigate (#58).
    """
    logger.warning(
        "attachment stream truncated: %s expected=%d sent=%d",
        where, expected, sent,
    )


def _stream_slice(
    fp: BinaryIO, start: int, length: int, where: str,
) -> Iterator[bytes]:
    """Yield at most ``length`` bytes from ``start``; the caller closes ``fp``.

    Never reads past the slice, so a file longer than recorded cannot overrun
    ``Content-Length``. A short file logs the #58 WARNING; a read error is
    logged against ``where`` and re-raised, since an EIO would otherwise end
    the stream with nothing naming the blob (#386).
    """
    sent = 0
    try:
        fp.seek(start)
        while sent < length:
            chunk = fp.read(min(_CHUNK, length - sent))
            if not chunk:
                _log_truncation(where, length, sent)
                return
            sent += len(chunk)
            yield chunk
    except OSError:
        logger.exception("attachment stream read failed: %s sent=%d", where, sent)
        raise


def _stream_full(fp: BinaryIO, where: str, expected: int) -> Iterator[bytes]:
    """Stream the whole blob — exactly ``expected`` bytes — closing on exit.

    A file longer than ``attachment_blobs.size_bytes`` is cut at the size
    and logged: sending the excess would make h11 abort the response with no
    localmail line at all (#386, the counterpart of #58's short read).
    """
    try:
        yield from _stream_slice(fp, 0, expected, where)
        try:
            over = fp.read(1)
        except OSError:
            # The body is already complete, so log and finish rather than
            # dropping a download that succeeded.
            logger.exception("attachment stream overrun probe failed: %s", where)
            over = b""
        if over:
            logger.warning(
                "attachment stream overran: %s file is longer than expected=%d",
                where, expected,
            )
    finally:
        fp.close()


def _stream_range(
    fp: BinaryIO, byte_range: ByteRange, where: str,
) -> Iterator[bytes]:
    """Stream exactly the bytes covered by ``byte_range``, closing on exit."""
    try:
        yield from _stream_slice(fp, byte_range.start, byte_range.length, where)
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
    mime: str | None,
    size: int,
    fp: BinaryIO,
    filename: str | None,
    log_context: str = "",
) -> Response:
    """200 / 206 / 416 for an already-opened, ACL-cleared blob.

    Takes ownership of ``fp``: the streamer closes it, or this function does
    on the 416 branch. ``filename`` falls back to a sha-prefix name when it
    is ``None`` or blank. Every response carries the #32 force-download
    headers and the strong ``ETag``. ``log_context`` is appended to the
    sha in any stream log line — a blob is shared, so the index route names
    the message and index that asked for it (#386).
    """
    etag = etag_for_sha256(sha256)
    where = f"sha256={sha256} {log_context}".rstrip()
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
            _stream_full(fp, where, size),
            media_type=response_mime,
            headers={
                "Content-Length": str(size),
                "Content-Disposition": disposition,
                "Accept-Ranges": "bytes",
                "ETag": etag,
            },
        )

    return StreamingResponse(
        _stream_range(fp, byte_range, where),
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
