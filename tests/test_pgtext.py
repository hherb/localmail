# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Unit tests for :mod:`localmail.pgtext` — the shared NUL-stripping boundary.

Postgres ``TEXT`` rejects ``\\x00``. Three independent producers write text into
``TEXT`` columns (the MIME parser, the attachment extractors, the extraction
worker's failure logs), and each used to carry its own private copy of the rule.
These tests pin the one shared implementation they now all use (#249).
"""

from __future__ import annotations

import pytest

from localmail.pgtext import strip_nuls, strip_nuls_all


class TestStripNuls:
    """``strip_nuls`` removes every NUL and is otherwise the identity."""

    def test_removes_a_nul(self) -> None:
        assert strip_nuls("a\x00b") == "ab"

    def test_removes_every_nul_not_just_the_first(self) -> None:
        assert strip_nuls("\x00a\x00\x00b\x00") == "ab"

    def test_leaves_clean_text_untouched(self) -> None:
        assert strip_nuls("hello wörld") == "hello wörld"

    def test_returns_the_same_object_when_there_is_nothing_to_strip(self) -> None:
        """The common path must not allocate a copy of every message body."""
        s = "no nuls here"
        assert strip_nuls(s) is s

    def test_passes_none_through(self) -> None:
        """Callers thread optional headers straight through (parser.py does)."""
        assert strip_nuls(None) is None

    def test_an_all_nul_string_becomes_empty_not_none(self) -> None:
        """`` '' `` is the parser's "normalize to SQL NULL" signal; conflating it
        with ``None`` here would move that decision into the wrong layer."""
        assert strip_nuls("\x00\x00") == ""

    @pytest.mark.parametrize("other", ["\r", "\n", "\t", "\x01", "\x7f", "�"])
    def test_only_nul_is_stripped(self, other: str) -> None:
        """Other control characters are legal in Postgres TEXT — leave them."""
        assert strip_nuls(f"a{other}b") == f"a{other}b"


class TestStripNulsAll:
    """``strip_nuls_all`` is the list flavour used for multi-valued headers."""

    def test_strips_each_element(self) -> None:
        assert strip_nuls_all(["a\x00", "\x00b", "c"]) == ["a", "b", "c"]

    def test_empty_list(self) -> None:
        assert strip_nuls_all([]) == []

    def test_preserves_order_and_length(self) -> None:
        """A header list is positional; dropping an emptied entry would
        silently renumber the rest."""
        assert strip_nuls_all(["\x00", "keep", "\x00"]) == ["", "keep", ""]
