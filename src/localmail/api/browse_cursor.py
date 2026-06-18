# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Opaque cursor codec for GET /v1/messages.

Wire form is URL-safe base64 of one of:
  - "d|<iso-ts>|<id>"   — dated row
  - "n|<id>"            — NULL-date tail row

Clients MUST treat the cursor as opaque; the encoding can change without an
API version bump as long as `decode_browse_cursor` keeps accepting any
encoding `encode_browse_cursor` ever emitted.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime

from localmail.api.errors import ValidationFailed


@dataclass(frozen=True)
class BrowseCursor:
    """Keyset position for the message browse list.

    `ts is None` means "already in the NULLS-LAST tail; paginate by id alone".
    """
    ts: datetime | None
    id: int


def encode_browse_cursor(cur: BrowseCursor) -> str:
    if cur.ts is None:
        payload = f"n|{cur.id}".encode("ascii")
    else:
        payload = f"d|{cur.ts.isoformat()}|{cur.id}".encode("ascii")
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def decode_browse_cursor(raw: str) -> BrowseCursor:
    if not raw:
        raise ValidationFailed("cursor: empty")
    try:
        # Restore base64 padding the encoder stripped.
        padded = raw + "=" * (-len(raw) % 4)
        body = base64.urlsafe_b64decode(padded.encode("ascii")).decode("ascii")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValidationFailed(f"cursor: malformed base64 ({exc})") from exc
    parts = body.split("|")
    if not parts:
        raise ValidationFailed("cursor: empty payload")
    kind = parts[0]
    if kind == "n" and len(parts) == 2:
        return BrowseCursor(ts=None, id=_parse_nonneg_int(parts[1]))
    if kind == "d" and len(parts) == 3:
        try:
            ts = datetime.fromisoformat(parts[1])
        except ValueError as exc:
            raise ValidationFailed(f"cursor: bad timestamp {parts[1]!r}") from exc
        return BrowseCursor(ts=ts, id=_parse_nonneg_int(parts[2]))
    raise ValidationFailed(f"cursor: unknown kind {kind!r}")


def _parse_nonneg_int(s: str) -> int:
    if not s.isascii() or not s.isdigit():
        raise ValidationFailed(f"cursor: bad id {s!r}")
    return int(s)
