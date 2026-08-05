# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Tests for the language-detector input rule (#255)."""

from __future__ import annotations

import pytest

from localmail.search.lang_text import normalize_for_detection


@pytest.mark.parametrize(
    "raw",
    [
        "Read more at https://ct.klclick.com/f/a/IgDYzk3AXlDhs1ogMp1raw~~/AASl5QA~/RgRlWoXg now",
        "Read more at http://info.cirrusmedia.com/x?id=t8689239,64ac8b1 now",
        "Read more at www.askapatient.com/viewrating.asp?drug=20233&name=RHINOCORT now",
        "Read more at <http://info.cirrusmedia.com/x?id=t868> now",
    ],
)
def test_urls_are_removed(raw: str) -> None:
    """Every URL form seen in the live archive leaves no residue."""
    out = normalize_for_detection(raw)
    assert "http" not in out
    assert "www." not in out
    assert out.startswith("Read more at")
    assert out.endswith("now")


def test_markdown_link_keeps_its_anchor_text() -> None:
    """The human-readable anchor is the linguistic signal worth keeping."""
    out = normalize_for_detection("[View in Your Browser](https://ct.klclick.com/f/a/x~~/A)")
    assert "View in Your Browser" in out
    assert "klclick" not in out


def test_text_without_urls_is_unchanged_apart_from_whitespace() -> None:
    """The common path must not mangle ordinary prose."""
    assert normalize_for_detection("Hallo Horst, wie geht es dir?") == (
        "Hallo Horst, wie geht es dir?"
    )


def test_whitespace_is_collapsed() -> None:
    assert normalize_for_detection("a\n\n\tb   c") == "a b c"


def test_url_only_body_normalises_to_empty() -> None:
    """The load-bearing case: no linguistic content means the caller declines.

    A body of pure tracking URLs clears the 20-char floor when measured raw and
    receives a confident garbage label. Measured after normalisation it is
    empty, so `LinguaDetector.detect` declines it.
    """
    assert normalize_for_detection("https://a.example/x  http://b.example/y") == ""


def test_is_idempotent() -> None:
    once = normalize_for_detection("see https://x.example/a b")
    assert normalize_for_detection(once) == once


def test_empty_input() -> None:
    assert normalize_for_detection("") == ""
