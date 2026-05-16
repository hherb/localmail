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


from datetime import datetime, timezone

from localmail.config import SearchConfig
from localmail.search.chunking import (
    ChunkSpec,
    MessageRow,
    chunk_message,
    split_by_tokens,
)


def _cfg(**overrides) -> SearchConfig:
    cfg = SearchConfig()
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def test_split_by_tokens_respects_size_and_overlap():
    text = " ".join(f"word{i}" for i in range(500))
    chunks = split_by_tokens(text, size=100, overlap=20)
    assert len(chunks) >= 5
    # First chunk's last ~20 tokens should appear in chunk 2 (overlap)
    assert chunks[0].split()[-5] in chunks[1]


def test_split_by_tokens_short_input_returns_one_chunk():
    assert split_by_tokens("hello world", size=100, overlap=20) == ["hello world"]


def test_chunk_message_short_body_emits_header_only():
    msg = MessageRow(
        id=1,
        subject="Quick note",
        from_addr="anna@x", from_name="Anna",
        to_addrs=["bob@x"],
        date_sent=datetime(2024, 9, 14, tzinfo=timezone.utc),
        body_text="See you Tuesday.",
    )
    chunks = chunk_message(msg, _cfg())
    assert len(chunks) == 1
    assert chunks[0].kind == "header"
    assert chunks[0].chunk_idx == 0
    assert "Quick note" in chunks[0].text
    assert "See you Tuesday" in chunks[0].text


def test_chunk_message_long_body_emits_header_plus_body_chunks():
    body = " ".join(f"sentence{i}." for i in range(800))
    msg = MessageRow(
        id=2, subject="Long", from_addr=None, from_name=None,
        to_addrs=None, date_sent=None, body_text=body,
    )
    chunks = chunk_message(msg, _cfg(chunk_size_tokens=200, chunk_overlap_tokens=40))
    assert chunks[0].kind == "header"
    assert any(c.kind == "body" for c in chunks)
    body_chunks = [c for c in chunks if c.kind == "body"]
    assert body_chunks[0].chunk_idx == 0
    assert all(c.token_count > 0 for c in chunks)


def test_chunk_message_strips_quoted_reply_when_enabled():
    body = (
        "My fresh content here.\n\n"
        "On Tue, Sep 14, 2024 at 10:23, Anna <a@x> wrote:\n"
        "> the old quoted bits we don't want indexed\n" * 50
    )
    msg = MessageRow(
        id=3, subject="Re:", from_addr=None, from_name=None,
        to_addrs=None, date_sent=None, body_text=body,
    )
    chunks = chunk_message(msg, _cfg(chunk_strip_quoted_replies=True))
    all_text = " ".join(c.text for c in chunks)
    assert "fresh content" in all_text
    assert "old quoted bits" not in all_text


def test_chunk_message_handles_none_body():
    msg = MessageRow(
        id=4, subject="Subject only", from_addr=None, from_name=None,
        to_addrs=None, date_sent=None, body_text=None,
    )
    chunks = chunk_message(msg, _cfg())
    assert len(chunks) == 1
    assert "Subject only" in chunks[0].text
