"""Unit tests for the pure Range-header parser (`localmail.api.range_requests`).

These cover RFC 9110 §14.1 single-range semantics and the "ignore unparseable
Range → serve full 200" branch — see the module docstring for the design.
"""
from __future__ import annotations

import pytest

from localmail.api.range_requests import (
    ByteRange,
    UnsatisfiableRange,
    content_range_header,
    parse_byte_range,
    unsatisfiable_content_range,
)

DEFAULT_SIZE = 100


def test_absent_header_returns_none() -> None:
    assert parse_byte_range(None, DEFAULT_SIZE) is None


def test_non_bytes_unit_returns_none() -> None:
    """Range units other than ``bytes`` are ignored (RFC 9110 §14.1.2)."""
    assert parse_byte_range("pages=0-9", DEFAULT_SIZE) is None


def test_missing_equals_returns_none() -> None:
    assert parse_byte_range("bytes 0-9", DEFAULT_SIZE) is None


def test_empty_spec_returns_none() -> None:
    assert parse_byte_range("bytes=", DEFAULT_SIZE) is None


def test_no_dash_returns_none() -> None:
    assert parse_byte_range("bytes=5", DEFAULT_SIZE) is None


def test_just_dash_returns_none() -> None:
    assert parse_byte_range("bytes=-", DEFAULT_SIZE) is None


def test_first_n_bytes() -> None:
    assert parse_byte_range("bytes=0-9", DEFAULT_SIZE) == ByteRange(0, 9)


def test_open_ended_range() -> None:
    """``bytes=N-`` → ``N`` to end of file."""
    assert parse_byte_range("bytes=10-", DEFAULT_SIZE) == ByteRange(10, 99)


def test_suffix_range_returns_trailing_bytes() -> None:
    """``bytes=-N`` → last N bytes."""
    assert parse_byte_range("bytes=-10", DEFAULT_SIZE) == ByteRange(90, 99)


def test_suffix_larger_than_file_returns_whole_file() -> None:
    """RFC 9110: a suffix length larger than the file → whole file."""
    assert parse_byte_range("bytes=-1000", DEFAULT_SIZE) == ByteRange(0, 99)


def test_end_past_eof_is_clamped() -> None:
    """RFC 9110 §14.1.2: end >= size → end becomes size - 1."""
    assert parse_byte_range("bytes=0-99999", DEFAULT_SIZE) == ByteRange(0, 99)


def test_start_past_eof_raises_unsatisfiable() -> None:
    """Start past end-of-file is the canonical 416 case."""
    with pytest.raises(UnsatisfiableRange):
        parse_byte_range("bytes=200-300", DEFAULT_SIZE)


def test_open_ended_start_past_eof_raises_unsatisfiable() -> None:
    with pytest.raises(UnsatisfiableRange):
        parse_byte_range("bytes=200-", DEFAULT_SIZE)


def test_suffix_zero_raises_unsatisfiable() -> None:
    """``bytes=-0`` requests zero trailing bytes — unsatisfiable."""
    with pytest.raises(UnsatisfiableRange):
        parse_byte_range("bytes=-0", DEFAULT_SIZE)


def test_negative_suffix_raises_unsatisfiable() -> None:
    """``bytes=--5`` parses end_str='-5' → int=-5 → unsatisfiable."""
    with pytest.raises(UnsatisfiableRange):
        parse_byte_range("bytes=--5", DEFAULT_SIZE)


def test_non_numeric_returns_none() -> None:
    """Garbage → ignore (caller serves full 200, per RFC permissive clause)."""
    assert parse_byte_range("bytes=abc-", DEFAULT_SIZE) is None
    assert parse_byte_range("bytes=0-abc", DEFAULT_SIZE) is None
    assert parse_byte_range("bytes=abc-def", DEFAULT_SIZE) is None


def test_end_before_start_returns_none() -> None:
    """``bytes=5-3`` is malformed → fall through to 200."""
    assert parse_byte_range("bytes=5-3", DEFAULT_SIZE) is None


def test_multi_range_returns_none() -> None:
    """Multi-range falls through to 200; we don't emit multipart/byteranges."""
    assert parse_byte_range("bytes=0-9,20-29", DEFAULT_SIZE) is None


def test_range_on_empty_resource_raises_unsatisfiable() -> None:
    """Any byte range against a 0-byte file is unsatisfiable."""
    with pytest.raises(UnsatisfiableRange):
        parse_byte_range("bytes=0-9", 0)


def test_byterange_length_property() -> None:
    """ByteRange.length is end - start + 1 (inclusive)."""
    assert ByteRange(0, 9).length == 10
    assert ByteRange(10, 10).length == 1
    assert ByteRange(0, 0).length == 1


def test_content_range_header_format() -> None:
    """206 responses use ``Content-Range: bytes start-end/total``."""
    assert content_range_header(ByteRange(10, 19), 100) == "bytes 10-19/100"


def test_unsatisfiable_content_range_header_format() -> None:
    """416 responses use ``Content-Range: bytes */total``."""
    assert unsatisfiable_content_range(100) == "bytes */100"


def test_full_file_request_via_zero_open_ended() -> None:
    """``bytes=0-`` is the canonical "give me the whole thing, resumable" form."""
    assert parse_byte_range("bytes=0-", DEFAULT_SIZE) == ByteRange(0, 99)


def test_single_byte_range() -> None:
    """``bytes=5-5`` requests exactly one byte."""
    r = parse_byte_range("bytes=5-5", DEFAULT_SIZE)
    assert r == ByteRange(5, 5)
    assert r is not None and r.length == 1
