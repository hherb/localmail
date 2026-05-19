"""HTTP conditional-request helpers (RFC 9110 §13.1).

Pure parsing utilities. No IO; no FastAPI dependencies. Used by
`localmail.serve.routes.attachments` for ETag, If-None-Match, and
If-Range handling on the SHA-keyed attachment route.

Attachment URLs are themselves content-addressable — the path is the
SHA-256 of the bytes — so every ETag is canonically strong, never
changes for a given URL, and ETag emission is free.

The helpers below are intentionally generic over their ``etag`` argument
so future endpoints with non-SHA tags can reuse the same parsing.

Comparison rules per RFC 9110:

* ``If-None-Match`` (§13.1.2) → **weak comparison**: strip a leading
  ``W/`` from either side, then compare opaque-tag bytes. A literal
  ``*`` matches any existing representation.
* ``If-Range`` (§13.1.5) → **strong comparison**: weak etags NEVER
  match; HTTP-date values (the other allowed If-Range shape) never
  match either, because we don't track per-blob mtime.
"""
from __future__ import annotations

_WEAK_PREFIX = "W/"
_DQUOTE = '"'
_STAR = "*"


def etag_for_sha256(sha256_hex: str) -> str:
    """Build a strong ETag header value from a SHA-256 hex digest.

    The result is wrapped in DQUOTE per RFC 9110 §8.8.3 grammar
    (``entity-tag = [ weak ] opaque-tag``; ``opaque-tag = DQUOTE *etagc
    DQUOTE``) and carries no ``W/`` prefix — content-addressable URLs
    are canonically strong.
    """
    return f'{_DQUOTE}{sha256_hex}{_DQUOTE}'


def _strip_weak(token: str) -> str:
    """Drop a leading ``W/`` prefix for weak-comparison normalisation."""
    return token[len(_WEAK_PREFIX):] if token.startswith(_WEAK_PREFIX) else token


def _split_etag_list(header: str) -> list[str]:
    """Split a `1#entity-tag` header into individual tokens.

    The grammar in RFC 9110 §5.6.1 allows OWS around commas; etags
    themselves never contain commas (only DQUOTE-delimited etagc chars
    or the literal ``*``), so a plain split-then-strip is safe.
    """
    return [token.strip() for token in header.split(",") if token.strip()]


def if_none_match_satisfies(header: str | None, etag: str) -> bool:
    """Return True if If-None-Match should cause a 304 for ``etag``.

    Args:
        header: Raw If-None-Match header value, or ``None``.
        etag: Canonical server ETag, e.g. the output of
            :func:`etag_for_sha256`.

    Returns:
        True if the precondition fails (caller should emit 304); False
        if absent, malformed, or non-matching (caller should serve the
        normal response).

    Per RFC 9110 §13.1.2 weak comparison is used: ``W/"abc"`` matches
    ``"abc"`` and vice-versa. A literal ``*`` matches any existing
    representation.
    """
    if not header:
        return False
    target = _strip_weak(etag)
    for token in _split_etag_list(header):
        if token == _STAR:
            return True
        if _strip_weak(token) == target:
            return True
    return False


def if_range_allows_partial(header: str | None, etag: str) -> bool:
    """Return True if a Range request may be honoured under If-Range.

    Args:
        header: Raw If-Range header value, or ``None``.
        etag: Canonical server ETag, e.g. the output of
            :func:`etag_for_sha256`.

    Returns:
        True if the Range should be honoured (no If-Range, or strong
        etag match); False if the precondition fails (caller serves
        200 full so a resumed download cannot stitch two distinct
        representations together).

    Per RFC 9110 §13.1.5 **strong** comparison is required: a weak
    etag (``W/"…"``) NEVER matches, and an HTTP-date value never
    matches our SHA-derived strong tag (we don't track Last-Modified,
    so dates are uncomparable; failing closed is the safe default).

    An empty-string header is treated as malformed and fails closed
    (serve 200 full) — distinct from a missing header.
    """
    if header is None:
        return True
    candidate = header.strip()
    if not candidate.startswith(_DQUOTE):
        return False
    return candidate == etag
