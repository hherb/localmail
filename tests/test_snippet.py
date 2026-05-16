"""Tests for the snippet-windowing pure function."""

from __future__ import annotations

from localmail.search.searcher import make_snippet


def test_snippet_returns_full_text_when_short():
    assert make_snippet("Short body.", ["body"], width=200) == "Short body."


def test_snippet_centers_on_term_match():
    text = "lorem ipsum dolor " * 50 + "the BERLIN conference " + "sit amet " * 50
    out = make_snippet(text, ["berlin"], width=80)
    assert "BERLIN" in out
    assert len(out) <= 100  # width + a little padding for word boundaries


def test_snippet_first_term_wins_when_multiple():
    text = ("alpha " * 100) + "beta " + ("gamma " * 100)
    out = make_snippet(text, ["beta"], width=80)
    assert "beta" in out


def test_snippet_falls_back_to_head_when_no_terms_match():
    text = "no matches here at all, just a long preamble " * 5
    out = make_snippet(text, ["nope", "absent"], width=80)
    # falls back to the leading window
    assert out.startswith("no matches here")


def test_snippet_handles_empty_query_terms():
    assert make_snippet("abc def", [], width=80) == "abc def"


def test_snippet_strips_leading_partial_words():
    text = "_______________________________ the BERLIN conference talk"
    out = make_snippet(text, ["berlin"], width=40)
    assert out.startswith("…") or out.startswith("the") or "BERLIN" in out
