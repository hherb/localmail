# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The paged extracted-text read, against real rows."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import psycopg
import pytest

from localmail.api.attachments import (
    get_attachment_text,
    get_attachment_text_page,
    text_window_from_query,
)
from localmail.api.errors import NotFound, ValidationFailed
from localmail.text_window import TextWindow

_ASTRAL = "a\U0001D11Eb\U0001D11Ec"  # five code points, seven UTF-16 units


def _seed_text(conn: psycopg.Connection, text: str) -> tuple[str, int]:
    """Blob + carrier message + account + extracted text. Returns (sha, account)."""
    sha = hashlib.sha256(text.encode()).hexdigest()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO attachment_blobs (sha256, mime_type, size_bytes, path) "
            "VALUES (%s, 'application/pdf', 1, '/nonexistent')",
            (bytes.fromhex(sha),),
        )
        cur.execute(
            "INSERT INTO attachment_text (sha256, extractor, extracted_text) "
            "VALUES (%s, 'pypdf', %s)",
            (bytes.fromhex(sha), text),
        )
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method) "
            "VALUES (%s, 'x@y.test', 'imap.x', 'password') RETURNING id",
            (f"acct-{sha[:8]}",),
        )
        row = cur.fetchone(); assert row is not None
        aid = int(row[0])
        raw = b"x"
        cur.execute(
            "INSERT INTO messages (account_id, message_id, raw_bytes, raw_sha256, "
            "size_bytes, headers, attachments, date_sent) "
            "VALUES (%s, %s, %s, %s, 1, '{}'::jsonb, %s, %s)",
            (aid, f"<{sha}@x>", raw, hashlib.sha256(raw).digest(),
             psycopg.types.json.Jsonb([{"filename": "t.pdf", "sha256": sha}]),
             datetime.now(timezone.utc)),
        )
    conn.commit()
    return sha, aid


def test_pages_concatenate_to_the_whole_text_even_across_astral_characters(
    db_conn: psycopg.Connection,
) -> None:
    sha, aid = _seed_text(db_conn, _ASTRAL)
    pieces: list[str] = []
    offset: int | None = 0
    while offset is not None:
        page = get_attachment_text_page(
            db_conn, sha, allowed_account_ids=[aid],
            window=TextWindow(offset=offset, limit=2),
        )
        assert page.total == 5
        pieces.append(page.text)
        offset = page.next_offset
    assert pieces == ["a\U0001D11E", "b\U0001D11E", "c"]
    assert "".join(pieces) == _ASTRAL


def test_an_offset_past_int4_is_clamped_not_a_500(db_conn: psycopg.Connection) -> None:
    sha, aid = _seed_text(db_conn, "hello")
    page = get_attachment_text_page(
        db_conn, sha, allowed_account_ids=[aid],
        window=TextWindow(offset=2**31, limit=2**40),
    )
    assert (page.text, page.total, page.next_offset) == ("", 5, None)


def test_a_limit_past_int4_reads_to_the_end(db_conn: psycopg.Connection) -> None:
    sha, aid = _seed_text(db_conn, "hello")
    page = get_attachment_text_page(
        db_conn, sha, allowed_account_ids=[aid],
        window=TextWindow(offset=1, limit=2**40),
    )
    assert (page.text, page.next_offset) == ("ello", None)


def test_get_attachment_text_still_returns_the_whole_string(
    db_conn: psycopg.Connection,
) -> None:
    sha, aid = _seed_text(db_conn, _ASTRAL)
    assert get_attachment_text(db_conn, sha, allowed_account_ids=[aid]) == _ASTRAL


def test_an_ungranted_caller_gets_the_shared_404(db_conn: psycopg.Connection) -> None:
    sha, _aid = _seed_text(db_conn, "hello")
    with pytest.raises(NotFound):
        get_attachment_text_page(
            db_conn, sha, allowed_account_ids=[], window=TextWindow(),
        )


def test_window_is_keyword_only_with_no_default(db_conn: psycopg.Connection) -> None:
    sha, aid = _seed_text(db_conn, "hello")
    with pytest.raises(TypeError):
        get_attachment_text_page(db_conn, sha, allowed_account_ids=[aid])  # type: ignore[call-arg]


@pytest.mark.parametrize(("offset", "limit", "expected"), [
    (None, None, TextWindow()),
    ("3", None, TextWindow(offset=3)),
    ("0", "20000", TextWindow(offset=0, limit=20_000)),
])
def test_the_query_becomes_a_window(
    offset: str | None, limit: str | None, expected: TextWindow,
) -> None:
    assert text_window_from_query(offset, limit) == expected


@pytest.mark.parametrize(("offset", "limit", "fragment"), [
    ("-1", None, "offset must be a base-10 integer"),
    ("x", None, "offset must be a base-10 integer"),
    (None, "abc", "limit must be a base-10 integer"),
    (None, "0", "limit must be >= 1, got 0"),
])
def test_a_bad_query_is_a_validation_failure(
    offset: str | None, limit: str | None, fragment: str,
) -> None:
    with pytest.raises(ValidationFailed, match=fragment):
        text_window_from_query(offset, limit)


def test_an_offset_without_a_limit_reads_from_the_offset(
    db_conn: psycopg.Connection,
) -> None:
    # The no-limit branch is its own statement (`substring … for NULL` is
    # strict), so it needs its own pin: reading from 1 there would return the
    # whole text labelled `offset: 3`.
    sha, aid = _seed_text(db_conn, "hello")
    page = get_attachment_text_page(
        db_conn, sha, allowed_account_ids=[aid], window=TextWindow(offset=3),
    )
    assert (page.text, page.offset, page.total, page.next_offset) == ("lo", 3, 5, None)


def test_get_attachment_text_is_never_capped(db_conn: psycopg.Connection) -> None:
    # MCP reads through this with the whole-text window; every other fixture
    # is shorter than any plausible page size, so a silent cap would pass them.
    text = "x" * 300_000 + _ASTRAL
    sha, aid = _seed_text(db_conn, text)
    assert get_attachment_text(db_conn, sha, allowed_account_ids=[aid]) == text
