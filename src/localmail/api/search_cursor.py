# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Opaque cursor codec for POST /v1/search.

Two cursor kinds share the wire field:

  * Pool cursor (hybrid search) — ``"{search_token}:{page}"``.
  * Keyset cursor (lexical-date search) — ``"K|<base64>"`` where the
    payload is the same encoding ``api.browse_cursor`` uses.

The route layer selects by leading prefix. Clients MUST treat the cursor
as opaque — the prefix is an internal discriminator, not API.
"""
from __future__ import annotations

from dataclasses import dataclass

from localmail.api.browse_cursor import (
    BrowseCursor, decode_browse_cursor, encode_browse_cursor,
)
from localmail.api.errors import ValidationFailed
from localmail.search.searcher import KeysetCursor

_KEYSET_PREFIX = "K|"


@dataclass(frozen=True)
class SearchCursor:
    token: str
    page: int


def encode_search_cursor(cursor: SearchCursor) -> str:
    return f"{cursor.token}:{cursor.page}"


def decode_search_cursor(raw: str) -> SearchCursor:
    if ":" not in raw:
        raise ValidationFailed(f"cursor: missing ':' separator in {raw!r}")
    token, _, page_str = raw.rpartition(":")
    if not token:
        raise ValidationFailed("cursor: empty token")
    if not page_str.isascii() or not page_str.isdigit():
        raise ValidationFailed(f"cursor: page must be a positive integer, got {page_str!r}")
    page = int(page_str)
    if page < 1:
        raise ValidationFailed(f"cursor: page must be >= 1, got {page}")
    return SearchCursor(token=token, page=page)


def is_keyset_cursor(raw: str) -> bool:
    return raw.startswith(_KEYSET_PREFIX)


def encode_keyset_cursor(ks: KeysetCursor) -> str:
    payload = encode_browse_cursor(BrowseCursor(ts=ks.ts, id=ks.id))
    return f"{_KEYSET_PREFIX}{payload}"


def decode_keyset_cursor(raw: str) -> KeysetCursor:
    if not is_keyset_cursor(raw):
        raise ValidationFailed(f"cursor: not a keyset cursor: {raw!r}")
    bc = decode_browse_cursor(raw[len(_KEYSET_PREFIX):])
    return KeysetCursor(ts=bc.ts, id=bc.id)
