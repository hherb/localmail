# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The blank-text rule agrees with the chunker it stands in for (#266)."""

from __future__ import annotations

import sys

from localmail.search.chunking import normalize_whitespace
from localmail.search.text_empty import is_blank

#: Every character Python considers whitespace. Derived rather than listed so a
#: future Unicode table update cannot leave the pin testing a stale set.
_WHITESPACE_CHARS = [chr(cp) for cp in range(sys.maxunicode + 1) if chr(cp).isspace()]


def test_every_whitespace_character_is_blank_on_its_own() -> None:
    assert _WHITESPACE_CHARS, "sanity: the sweep found no whitespace at all"
    for ch in _WHITESPACE_CHARS:
        assert is_blank(ch), f"U+{ord(ch):04X} not recognised as blank"


def test_agrees_with_the_chunker_for_every_whitespace_character() -> None:
    """`is_blank` exists to answer `normalize_whitespace(t) == ''` without
    running it. Drift between the two is what makes the embed worker's heal
    act on a row it was never meant to touch, so pin the equivalence over the
    whole whitespace set — including the line-boundary characters
    `str.splitlines()` treats specially (\\x0b, \\x1c-\\x1e, \\x85, U+2028)."""
    for ch in _WHITESPACE_CHARS:
        assert is_blank(ch) == (normalize_whitespace(ch) == "")


def test_agrees_with_the_chunker_on_composite_text() -> None:
    for text in [
        "",
        " ",
        " \t\n \n\t ",
        "\u00a0\u00a0\n\u2003",  # NBSP + EM space
        "\x0b\x1c\x85",
        "  ",
        "x",
        "  x  \n",
        "\u200b",  # zero-width space: not whitespace to either rule
        "\ufeff",  # BOM: likewise
        "a\u00a0",
    ]:
        assert is_blank(text) == (normalize_whitespace(text) == ""), repr(text)


def test_zero_width_characters_are_not_blank() -> None:
    """They survive `normalize_whitespace`, so they chunk — collapsing them to
    the sentinel would discard a row the chunker would have indexed."""
    assert not is_blank("\u200b")
    assert not is_blank("\ufeff")
