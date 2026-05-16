"""Tests for pure chunking helpers."""

from __future__ import annotations

from localmail.search.chunking import (
    normalize_whitespace,
    strip_quoted_replies,
    strip_signature,
)


def test_strip_quoted_replies_gmail_english():
    body = (
        "Hi Anna,\nLooks good — let's meet Tuesday.\nBest, H\n\n"
        "On Tue, Sep 14, 2024 at 10:23, Anna Schmidt <anna@x> wrote:\n"
        "> Hi Horst, I wanted to ask about the Berlin conference\n"
    )
    out = strip_quoted_replies(body)
    assert "Berlin conference" not in out
    assert "Tuesday" in out


def test_strip_quoted_replies_outlook_arrow_lines():
    body = "My answer.\n\n> original line one\n> original line two\n"
    out = strip_quoted_replies(body)
    assert "My answer." in out
    assert "original line" not in out


def test_strip_quoted_replies_german():
    body = (
        "Vielen Dank!\n\n"
        "Am 14. September 2024 um 10:23 schrieb Anna Schmidt <a@x>:\n"
        "> Hallo Horst\n"
    )
    out = strip_quoted_replies(body)
    assert "Vielen Dank!" in out
    assert "Hallo Horst" not in out


def test_strip_quoted_replies_spanish():
    body = (
        "Gracias!\n\nEl 14 de septiembre de 2024, Anna <a@x> escribió:\n> hola\n"
    )
    out = strip_quoted_replies(body)
    assert "Gracias!" in out
    assert "hola" not in out


def test_strip_signature_dash_dash_space():
    body = "Body text here.\nMore body.\n-- \nHorst Herb\nMD\n"
    out = strip_signature(body)
    assert "Body text here." in out
    assert "Horst Herb" not in out


def test_strip_signature_keeps_body_with_no_sig():
    body = "Just a body, no sig at all."
    assert strip_signature(body) == body


def test_normalize_whitespace_collapses_runs():
    assert normalize_whitespace("a   b\n\n\n c") == "a b\n\nc"
    assert normalize_whitespace("   leading \t") == "leading"
