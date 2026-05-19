"""Unit tests for the string-to-int ID parser at the api boundary."""
from __future__ import annotations

import pytest

from localmail.api.errors import ValidationFailed
from localmail.api.ids import parse_int_id


def test_parse_int_id_accepts_positive_digits() -> None:
    assert parse_int_id("42", field="account_id") == 42


def test_parse_int_id_accepts_zero() -> None:
    assert parse_int_id("0", field="account_id") == 0


def test_parse_int_id_accepts_large_bigserial() -> None:
    # > JS Number.MAX_SAFE_INTEGER (2^53 - 1 = 9_007_199_254_740_991)
    assert parse_int_id("9007199254740993", field="message_id") == 9007199254740993


def test_parse_int_id_rejects_negative() -> None:
    with pytest.raises(ValidationFailed):
        parse_int_id("-1", field="account_id")


def test_parse_int_id_rejects_plus_prefix() -> None:
    with pytest.raises(ValidationFailed):
        parse_int_id("+1", field="account_id")


def test_parse_int_id_rejects_alpha() -> None:
    with pytest.raises(ValidationFailed):
        parse_int_id("abc", field="message_id")


def test_parse_int_id_rejects_empty() -> None:
    with pytest.raises(ValidationFailed):
        parse_int_id("", field="message_id")


def test_parse_int_id_rejects_decimal() -> None:
    with pytest.raises(ValidationFailed):
        parse_int_id("1.5", field="message_id")


def test_parse_int_id_rejects_surrounding_whitespace() -> None:
    with pytest.raises(ValidationFailed):
        parse_int_id("  7  ", field="message_id")


def test_parse_int_id_rejects_hex() -> None:
    with pytest.raises(ValidationFailed):
        parse_int_id("0xff", field="account_id")


def test_parse_int_id_error_message_includes_field_and_value() -> None:
    with pytest.raises(ValidationFailed) as ei:
        parse_int_id("nope", field="account_id")
    assert "account_id" in str(ei.value)
    assert "'nope'" in str(ei.value)
