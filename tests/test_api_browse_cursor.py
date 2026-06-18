# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Tests for the opaque browse-list cursor codec."""
from datetime import datetime, timezone

import pytest

from localmail.api.browse_cursor import (
    BrowseCursor,
    decode_browse_cursor,
    encode_browse_cursor,
)
from localmail.api.errors import ValidationFailed


def test_dated_cursor_roundtrip() -> None:
    ts = datetime(2026, 5, 20, 12, 34, 56, tzinfo=timezone.utc)
    cur = BrowseCursor(ts=ts, id=42)
    encoded = encode_browse_cursor(cur)
    # Wire form must be URL-safe (no '+', '/', '=' padding required).
    assert "/" not in encoded and "+" not in encoded
    decoded = decode_browse_cursor(encoded)
    assert decoded == cur


def test_null_date_cursor_roundtrip() -> None:
    cur = BrowseCursor(ts=None, id=99)
    encoded = encode_browse_cursor(cur)
    decoded = decode_browse_cursor(encoded)
    assert decoded == cur
    assert decoded.ts is None


def test_decode_rejects_garbage() -> None:
    with pytest.raises(ValidationFailed, match="cursor"):
        decode_browse_cursor("not-a-cursor")


def test_decode_rejects_empty_string() -> None:
    with pytest.raises(ValidationFailed):
        decode_browse_cursor("")


def test_decode_rejects_negative_id() -> None:
    # encode_browse_cursor never emits these, but a hostile client could.
    import base64
    payload = base64.urlsafe_b64encode(b"n|-1").rstrip(b"=").decode("ascii")
    with pytest.raises(ValidationFailed):
        decode_browse_cursor(payload)


def test_decode_rejects_unknown_kind() -> None:
    import base64
    payload = base64.urlsafe_b64encode(b"x|1").rstrip(b"=").decode("ascii")
    with pytest.raises(ValidationFailed):
        decode_browse_cursor(payload)
