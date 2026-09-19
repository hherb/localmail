# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Character paging of a stored text — the one rule both text routes use."""
from __future__ import annotations

import pytest

from localmail.text_window import (
    MAX_TEXT_CHARS,
    TextPage,
    TextWindow,
    text_window_error,
)


def test_a_default_window_is_the_whole_text() -> None:
    assert text_window_error(0, None) is None
    w = TextWindow()
    assert (w.offset, w.limit, w.sql_from, w.sql_for) == (0, None, 1, None)


@pytest.mark.parametrize(("offset", "limit", "expected"), [
    (-1, None, "offset must be >= 0, got -1"),
    (0, 0, "limit must be >= 1, got 0"),
    (0, -5, "limit must be >= 1, got -5"),
])
def test_refusals_are_worded(offset: int, limit: int | None, expected: str) -> None:
    assert text_window_error(offset, limit) == expected


def test_a_zero_limit_is_refused_because_a_client_would_never_advance() -> None:
    # A zero-length window answers next_offset == offset for any unfinished
    # text, so a client looping on next_offset would spin forever.
    assert text_window_error(3, 0) is not None


@pytest.mark.parametrize(("offset", "limit"), [(-1, None), (0, 0)])
def test_construction_refuses_what_the_rule_refuses(
    offset: int, limit: int | None,
) -> None:
    with pytest.raises(ValueError, match="must be >= "):
        TextWindow(offset=offset, limit=limit)


def test_sql_from_is_one_based() -> None:
    assert TextWindow(offset=0).sql_from == 1
    assert TextWindow(offset=7).sql_from == 8


def test_positions_are_clamped_below_int4() -> None:
    # substring() takes int4 positions; 2**31 is a 500 from Postgres. Every
    # offset at or past MAX_TEXT_CHARS is past the end of every text anyway.
    w = TextWindow(offset=2**40, limit=2**40)
    assert w.sql_from == MAX_TEXT_CHARS + 1
    assert w.sql_for == MAX_TEXT_CHARS
    assert w.sql_from <= 2**31 - 1


def test_the_clamp_leaves_ordinary_values_alone() -> None:
    w = TextWindow(offset=5, limit=20_000)
    assert (w.sql_from, w.sql_for) == (6, 20_000)


@pytest.mark.parametrize(("offset", "limit", "text", "total", "next_offset"), [
    (0, 2, "ab", 5, 2),          # first page
    (2, 2, "cd", 5, 4),          # middle page
    (4, 2, "e", 5, None),        # last, short page
    (0, 5, "abcde", 5, None),    # exact fit
    (10, 2, "", 5, None),        # past the end
    (0, None, "", 0, None),      # empty text
    (3, None, "de", 5, None),    # un-paged remainder
])
def test_page_arithmetic(
    offset: int, limit: int | None, text: str, total: int,
    next_offset: int | None,
) -> None:
    page = TextWindow(offset=offset, limit=limit).page(text, total)
    assert page == TextPage(
        text=text, offset=offset, limit=limit, total=total,
        next_offset=next_offset,
    )


def test_next_offset_counts_code_points_not_utf16_units() -> None:
    # U+1D11E is one code point (what Postgres and Python count) but two
    # UTF-16 units (what JavaScript's .length counts). A client computing
    # offset + text.length would skip a character on every such text.
    text = "a\U0001D11E"
    page = TextWindow(offset=0, limit=2).page(text, total=4)
    assert page.next_offset == 2
    assert len(text.encode("utf-16-le")) // 2 == 3


def test_to_wire_carries_exactly_the_five_keys() -> None:
    wire = TextWindow(offset=0, limit=2).page("ab", 5).to_wire()
    assert wire == {
        "text": "ab", "offset": 0, "limit": 2, "total": 5, "next_offset": 2,
    }


@pytest.mark.parametrize(("text", "offset", "limit", "total", "next_offset", "fragment"), [
    # The stall: more to read, but next_offset does not move.
    ("", 3, 5, 10, 3, "does not advance"),
    # Disagrees with offset + len(text).
    ("ab", 0, 2, 5, 4, "disagrees"),
    # Claims the end when there is more.
    ("ab", 0, 2, 5, None, "disagrees"),
    # Claims more when the text is exhausted.
    ("ab", 3, 2, 5, 5, "disagrees"),
    ("abc", 0, 2, 5, 3, "over its limit"),
])
def test_an_inconsistent_page_cannot_be_built(
    text: str, offset: int, limit: int | None, total: int,
    next_offset: int | None, fragment: str,
) -> None:
    with pytest.raises(ValueError, match=fragment):
        TextPage(
            text=text, offset=offset, limit=limit, total=total,
            next_offset=next_offset,
        )


def test_a_consistent_page_builds() -> None:
    """Positive control for the refusals above."""
    assert TextPage(text="ab", offset=0, limit=2, total=5, next_offset=2).next_offset == 2
    assert TextPage(text="", offset=9, limit=2, total=5, next_offset=None).text == ""
