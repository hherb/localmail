import pytest

from localmail.api.errors import ValidationFailed
from localmail.api.search_cursor import (
    SearchCursor, decode_search_cursor, encode_search_cursor,
)


def test_roundtrip() -> None:
    c = SearchCursor(token="abc123", page=4)
    s = encode_search_cursor(c)
    assert s == "abc123:4"
    assert decode_search_cursor(s) == c


def test_decode_rejects_missing_colon() -> None:
    with pytest.raises(ValidationFailed):
        decode_search_cursor("abc123")


def test_decode_rejects_non_digit_page() -> None:
    with pytest.raises(ValidationFailed):
        decode_search_cursor("abc123:x")


def test_decode_rejects_page_zero_or_negative() -> None:
    with pytest.raises(ValidationFailed):
        decode_search_cursor("abc123:0")
    with pytest.raises(ValidationFailed):
        decode_search_cursor("abc123:-1")


def test_decode_rejects_empty_token() -> None:
    with pytest.raises(ValidationFailed):
        decode_search_cursor(":3")
