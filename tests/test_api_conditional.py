# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Unit tests for `localmail.api.conditional` — pure parsers for ETag,
If-None-Match (weak compare), and If-Range (strong compare). No DB, no
HTTP. Mirrors `tests/test_api_range_requests.py` in shape and intent.

RFC references throughout: RFC 9110 §8.8.3 (entity tags), §13.1.2
(If-None-Match → weak comparison), §13.1.5 (If-Range → strong
comparison).
"""
from __future__ import annotations

import pytest

from localmail.api.conditional import (
    etag_for_sha256,
    if_none_match_satisfies,
    if_range_allows_partial,
)

_SHA = "a" * 64
_OTHER_SHA = "b" * 64
_STRONG_ETAG = f'"{_SHA}"'
_WEAK_ETAG = f'W/"{_SHA}"'


def test_etag_for_sha256_is_quoted_strong_form() -> None:
    """The ETag is the SHA-256 hex wrapped in DQUOTE — strong form (no W/).
    Strong matters for If-Range, which requires strong comparison."""
    assert etag_for_sha256(_SHA) == _STRONG_ETAG


def test_etag_for_sha256_round_trips_through_the_helpers() -> None:
    """Output of `etag_for_sha256` must be the canonical input that the
    other two helpers compare against — guards against double-quoting or
    case-mangling drift across the module."""
    tag = etag_for_sha256(_SHA)
    assert if_none_match_satisfies(tag, tag) is True
    assert if_range_allows_partial(tag, tag) is True


def test_if_none_match_absent_header_is_not_a_match() -> None:
    """No header → not a precondition → don't shortcut to 304."""
    assert if_none_match_satisfies(None, _STRONG_ETAG) is False


def test_if_none_match_exact_strong_etag_matches() -> None:
    assert if_none_match_satisfies(_STRONG_ETAG, _STRONG_ETAG) is True


def test_if_none_match_weak_compare_accepts_weak_client_token() -> None:
    """If-None-Match uses **weak** comparison (RFC 9110 §13.1.2). A client
    sending W/"abc" must match a server etag of "abc"."""
    assert if_none_match_satisfies(_WEAK_ETAG, _STRONG_ETAG) is True


def test_if_none_match_star_matches_any_existing_etag() -> None:
    """`*` matches any current representation (RFC 9110 §13.1.2)."""
    assert if_none_match_satisfies("*", _STRONG_ETAG) is True


def test_if_none_match_non_matching_etag_does_not_match() -> None:
    assert if_none_match_satisfies(f'"{_OTHER_SHA}"', _STRONG_ETAG) is False


def test_if_none_match_comma_list_with_matching_member() -> None:
    """Header grammar is `1#entity-tag` — comma-separated, OWS-tolerant."""
    header = f'"{_OTHER_SHA}",   {_STRONG_ETAG}'
    assert if_none_match_satisfies(header, _STRONG_ETAG) is True


def test_if_none_match_comma_list_without_match() -> None:
    header = f'"{_OTHER_SHA}", "{"c" * 64}"'
    assert if_none_match_satisfies(header, _STRONG_ETAG) is False


def test_if_none_match_empty_header_is_not_a_match() -> None:
    """Empty header is malformed by §8.8.3 — be permissive, don't 304."""
    assert if_none_match_satisfies("", _STRONG_ETAG) is False


def test_if_none_match_only_whitespace_is_not_a_match() -> None:
    assert if_none_match_satisfies("   ", _STRONG_ETAG) is False


def test_if_range_absent_header_allows_partial() -> None:
    """No If-Range = no precondition; the Range proceeds as normal."""
    assert if_range_allows_partial(None, _STRONG_ETAG) is True


def test_if_range_exact_strong_match_allows_partial() -> None:
    assert if_range_allows_partial(_STRONG_ETAG, _STRONG_ETAG) is True


def test_if_range_weak_etag_does_not_match() -> None:
    """If-Range mandates **strong** comparison (RFC 9110 §13.1.5). A weak
    tag NEVER matches — server falls back to 200 to avoid stitching two
    different byte streams onto a resumed download."""
    assert if_range_allows_partial(_WEAK_ETAG, _STRONG_ETAG) is False


def test_if_range_non_matching_etag_does_not_match() -> None:
    assert if_range_allows_partial(f'"{_OTHER_SHA}"', _STRONG_ETAG) is False


def test_if_range_http_date_does_not_match() -> None:
    """We don't track Last-Modified — any HTTP-date value never matches.
    Range is ignored, server serves 200 full. This is conservative but
    correct: per RFC 9110, if you can't validate strong equality you MUST
    NOT serve 206."""
    http_date = "Tue, 19 May 2026 12:34:56 GMT"
    assert if_range_allows_partial(http_date, _STRONG_ETAG) is False


def test_if_range_empty_header_does_not_match() -> None:
    assert if_range_allows_partial("", _STRONG_ETAG) is False


def test_if_range_whitespace_tolerant_on_strong_match() -> None:
    """RFC 7230/9110 allow OWS around field values; client SDKs sometimes
    add a leading/trailing space. Strong comparison should still succeed
    after a `.strip()` on the candidate."""
    padded = f"  {_STRONG_ETAG}  "
    assert if_range_allows_partial(padded, _STRONG_ETAG) is True


@pytest.mark.parametrize(
    "bogus",
    ["W/", '"', '""', "garbage", "W/garbage"],
)
def test_if_range_garbage_does_not_match(bogus: str) -> None:
    """Anything that doesn't strong-compare equal must NOT honour the
    Range — be conservative."""
    assert if_range_allows_partial(bogus, _STRONG_ETAG) is False
