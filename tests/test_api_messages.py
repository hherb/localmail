# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import psycopg
import pytest

from localmail.api.errors import NotFound, ValidationFailed
from localmail.api.messages import _build_cid_map, get_message, get_message_raw
from localmail.attachments import write_attachments
from localmail.parser import parse_message

from . import _eml


def _seed_msg(conn: psycopg.Connection, **overrides) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            ("acct", "horst@example.com", "imap.example.com", "password"),
        )
        row = cur.fetchone()
        assert row is not None
        aid = row[0]
        cur.execute(
            "INSERT INTO mailboxes (account_id, name) VALUES (%s, %s) RETURNING id",
            (aid, "INBOX"),
        )
        row = cur.fetchone()
        assert row is not None
        mb_id = row[0]
        now = datetime.now(timezone.utc)
        defaults: dict = dict(
            account_id=aid,
            message_id="<m1@example>",
            subject="hello",
            from_addr="anna@example.com",
            from_name="Anna",
            to_addrs=["horst@example.com"],
            cc_addrs=None,
            bcc_addrs=None,
            body_text="hi there",
            body_html="<p>hi <b>there</b></p>",
            attachments=[],
            raw_bytes=(
                b"Received: by 10.0.0.1 with SMTP id aaa\r\n"
                b"From: Anna <anna@example.com>\r\n"
                b"received: from relay.example\r\n"
                b"Subject: hello\r\n"
                b"Date: Wed, 04 Mar 2026 10:00:00 +0000\r\n"
                b"\r\nhi there"
            ),
            headers={"From": ["Anna <anna@example.com>"], "Subject": ["hello"]},
            date_sent=datetime(2026, 3, 4, 10, 0, tzinfo=timezone.utc),
            date_received=now,
        )
        defaults.update(overrides)
        defaults["raw_sha256"] = b"\x00" * 32
        defaults["size_bytes"] = len(defaults["raw_bytes"])
        cur.execute(
            """INSERT INTO messages
               (account_id, message_id, subject, from_addr, from_name, to_addrs,
                cc_addrs, bcc_addrs, body_text, body_html, attachments,
                raw_bytes, raw_sha256, size_bytes, headers, date_sent, date_received)
               VALUES (%(account_id)s, %(message_id)s, %(subject)s, %(from_addr)s,
                       %(from_name)s, %(to_addrs)s, %(cc_addrs)s, %(bcc_addrs)s,
                       %(body_text)s, %(body_html)s, %(attachments)s::jsonb,
                       %(raw_bytes)s, %(raw_sha256)s, %(size_bytes)s,
                       %(headers)s::jsonb, %(date_sent)s, %(date_received)s)
               RETURNING id""",
            {**defaults,
             "attachments": json.dumps(defaults["attachments"]),
             "headers": json.dumps(defaults["headers"])},
        )
        row = cur.fetchone()
        assert row is not None
        msg_id = row[0]
        cur.execute(
            "INSERT INTO message_labels (message_id, mailbox_id, uid) VALUES (%s, %s, %s)",
            (msg_id, mb_id, msg_id),  # use msg_id as a fake UID
        )
        return msg_id


_ANY_ACCOUNT = list(range(1, 1000))


def _insert_blob(conn: psycopg.Connection, sha_hex: str, mime: str | None, size: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO attachment_blobs (sha256, path, mime_type, size_bytes) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (sha256) DO NOTHING",
            (bytes.fromhex(sha_hex), f"/nonexistent/{sha_hex}", mime, size),
        )


def test_get_message_attachment_entries_include_content_type_and_size(
    db_conn: psycopg.Connection,
) -> None:
    """#196: each attachment entry carries the blob's stored MIME type and
    decoded byte size so a downstream agent can branch on type/size without
    an extra probe request per attachment."""
    sha_hex = "ab" * 32
    _insert_blob(db_conn, sha_hex, "application/pdf", 84213)
    mid = _seed_msg(
        db_conn,
        attachments=[{"filename": "booking.pdf", "sha256": sha_hex}],
    )
    db_conn.commit()
    msg = get_message(db_conn, mid, allowed_account_ids=_ANY_ACCOUNT)
    att = msg["attachments"][0]
    assert att == {
        "filename": "booking.pdf",
        "sha256": sha_hex,
        "content_type": "application/pdf",
        "size": 84213,
    }


def test_get_message_attachment_missing_blob_row_yields_null_type_and_size(
    db_conn: psycopg.Connection,
) -> None:
    """An attachment referencing a sha with no attachment_blobs row degrades
    to content_type/size = None rather than raising or dropping the entry."""
    sha_hex = "cd" * 32
    mid = _seed_msg(
        db_conn,
        attachments=[{"filename": "orphan.bin", "sha256": sha_hex}],
    )
    db_conn.commit()
    msg = get_message(db_conn, mid, allowed_account_ids=_ANY_ACCOUNT)
    att = msg["attachments"][0]
    assert att["content_type"] is None
    assert att["size"] is None
    assert att["filename"] == "orphan.bin"
    assert att["sha256"] == sha_hex


def test_get_message_attachment_null_mime_yields_null_content_type(
    db_conn: psycopg.Connection,
) -> None:
    """A blob row with a NULL mime_type surfaces content_type=None but keeps
    the (NOT NULL) size — the case the ``str | None`` map type exists for."""
    sha_hex = "ef" * 32
    _insert_blob(db_conn, sha_hex, None, 512)
    mid = _seed_msg(
        db_conn,
        attachments=[{"filename": "unknown.dat", "sha256": sha_hex}],
    )
    db_conn.commit()
    msg = get_message(db_conn, mid, allowed_account_ids=_ANY_ACCOUNT)
    att = msg["attachments"][0]
    assert att["content_type"] is None
    assert att["size"] == 512


def test_get_message_multiple_attachments_each_carry_meta(
    db_conn: psycopg.Connection,
) -> None:
    """The batched blob lookup resolves every referenced sha, not just one."""
    sha_a = "a1" * 32
    sha_b = "b2" * 32
    _insert_blob(db_conn, sha_a, "application/pdf", 84213)
    _insert_blob(db_conn, sha_b, "image/png", 2048)
    mid = _seed_msg(
        db_conn,
        attachments=[
            {"filename": "booking.pdf", "sha256": sha_a},
            {"filename": "logo.png", "sha256": sha_b},
        ],
    )
    db_conn.commit()
    msg = get_message(db_conn, mid, allowed_account_ids=_ANY_ACCOUNT)
    by_name = {a["filename"]: a for a in msg["attachments"]}
    assert by_name["booking.pdf"]["content_type"] == "application/pdf"
    assert by_name["booking.pdf"]["size"] == 84213
    assert by_name["logo.png"]["content_type"] == "image/png"
    assert by_name["logo.png"]["size"] == 2048


def test_get_message_attachment_malformed_sha_degrades_to_null(
    db_conn: psycopg.Connection,
) -> None:
    """A non-hex sha in the JSONB must not 500 the whole message; it degrades
    to null metadata like an absent blob row (guards the bytes.fromhex path)."""
    mid = _seed_msg(
        db_conn,
        attachments=[{"filename": "corrupt.bin", "sha256": "not-a-valid-sha"}],
    )
    db_conn.commit()
    msg = get_message(db_conn, mid, allowed_account_ids=_ANY_ACCOUNT)
    att = msg["attachments"][0]
    assert att["filename"] == "corrupt.bin"
    assert att["sha256"] == "not-a-valid-sha"
    assert att["content_type"] is None
    assert att["size"] is None


def test_get_message_returns_compact_headers(db_conn: psycopg.Connection) -> None:
    mid = _seed_msg(db_conn)
    db_conn.commit()
    msg = get_message(db_conn, mid, allowed_account_ids=_ANY_ACCOUNT, headers="compact")
    assert msg["id"] == str(mid)
    assert msg["subject"] == "hello"
    assert msg["from"]["address"] == "anna@example.com"
    assert msg["from"]["name"] == "Anna"
    assert msg["to"][0]["address"] == "horst@example.com"
    assert "<p>hi" in msg["body_html"]
    assert msg["body_text"] == "hi there"
    assert msg["account"]["name"] == "acct"
    assert msg["folders"][0]["name"] == "INBOX"
    assert "headers" not in msg


def test_full_headers_are_read_from_the_message_not_the_stored_column(
    db_conn: psycopg.Connection,
) -> None:
    """The column is a snapshot of the parser that ran at sync (#379).

    The fixture's JSONB deliberately omits `Date` and `Received`; a passthrough
    would return the column and miss them.
    """
    mid = _seed_msg(db_conn)
    db_conn.commit()
    msg = get_message(db_conn, mid, allowed_account_ids=_ANY_ACCOUNT, headers="full")
    assert msg["headers"]["From"] == ["Anna <anna@example.com>"]
    assert msg["headers"]["Date"] == ["Wed, 04 Mar 2026 10:00:00 +0000"]
    assert msg["headers"]["Received"] == ["by 10.0.0.1 with SMTP id aaa"]


def test_list_keeps_every_occurrence_in_wire_order(db_conn: psycopg.Connection) -> None:
    mid = _seed_msg(db_conn)
    db_conn.commit()
    msg = get_message(db_conn, mid, allowed_account_ids=_ANY_ACCOUNT, headers="list")
    assert [e["name"] for e in msg["headers"]] == [
        "Received", "From", "received", "Subject", "Date",
    ]
    assert msg["headers"][0]["value"] == "by 10.0.0.1 with SMTP id aaa"


def test_grouping_the_list_reproduces_full(db_conn: psycopg.Connection) -> None:
    """The refinement invariant, end to end through the accessor."""
    mid = _seed_msg(db_conn)
    db_conn.commit()
    listed = get_message(db_conn, mid, allowed_account_ids=_ANY_ACCOUNT, headers="list")
    full = get_message(db_conn, mid, allowed_account_ids=_ANY_ACCOUNT, headers="full")
    grouped: dict[str, list[str]] = {}
    for entry in listed["headers"]:
        grouped.setdefault(entry["name"], []).append(entry["value"])
    assert grouped == full["headers"]


def test_an_unusable_mode_is_refused_even_with_an_empty_acl(
    db_conn: psycopg.Connection,
) -> None:
    """A refusal must never be disguised as a 404 (slice A's ordering rule)."""
    mid = _seed_msg(db_conn)
    db_conn.commit()
    with pytest.raises(ValidationFailed) as excinfo:
        get_message(db_conn, mid, allowed_account_ids=[], headers="xyzzy")
    assert "'list'" in str(excinfo.value)


def test_a_header_block_past_the_ceiling_is_re_read_in_full(
    db_conn: psycopg.Connection, monkeypatch, caplog,
) -> None:
    """Never a short list that looks complete."""
    padding = b"".join(
        b"X-Pad-%d: %s\r\n" % (i, b"y" * 200) for i in range(40)
    )
    mid = _seed_msg(
        db_conn,
        raw_bytes=padding + b"Subject: beyond the ceiling\r\n\r\nbody",
    )
    db_conn.commit()
    monkeypatch.setattr("localmail.api.messages.HEADER_BLOCK_READ_BYTES", 512)
    with caplog.at_level(logging.WARNING, logger="localmail.api.messages"):
        msg = get_message(db_conn, mid, allowed_account_ids=_ANY_ACCOUNT, headers="list")
    assert msg["headers"][-1] == {"name": "Subject", "value": "beyond the ceiling"}
    assert any(str(mid) in r.getMessage() for r in caplog.records)


def test_get_message_not_found_raises(db_conn: psycopg.Connection) -> None:
    with pytest.raises(NotFound):
        get_message(db_conn, 999999, allowed_account_ids=_ANY_ACCOUNT)


def test_get_message_raw_returns_bytes(db_conn: psycopg.Connection) -> None:
    mid = _seed_msg(db_conn)
    db_conn.commit()
    raw = get_message_raw(db_conn, mid, allowed_account_ids=_ANY_ACCOUNT)
    assert raw.startswith(b"Received: by 10.0.0.1 with SMTP id aaa")


def test_get_message_raw_not_found_raises(db_conn: psycopg.Connection) -> None:
    with pytest.raises(NotFound):
        get_message_raw(db_conn, 999999, allowed_account_ids=_ANY_ACCOUNT)


def test_build_cid_map_emits_cid_to_sha_for_inline_attachments() -> None:
    """Pins the JSONB-row → cid-map contract that the sanitizer depends on.

    Inline rows carry `content_id`; regular rows omit it. The map must
    include only the inline entries and key them on the bare cid token
    (no angle brackets)."""
    sha_inline = "ab" * 32
    sha_regular = "cd" * 32
    attachments = [
        {"filename": "inline.png", "sha256": sha_inline, "content_id": "inline-pixel@example"},
        {"filename": "report.pdf", "sha256": sha_regular},
    ]
    assert _build_cid_map(attachments) == {"inline-pixel@example": sha_inline}


def test_build_cid_map_strips_residual_angle_brackets() -> None:
    """Parser already strips brackets, but the rewrite path is defense-in-depth
    against legacy rows that might carry the bracketed form."""
    sha = "ef" * 32
    attachments = [{"sha256": sha, "content_id": "<legacy@example>"}]
    assert _build_cid_map(attachments) == {"legacy@example": sha}


def test_get_message_rewrites_cid_img_src_to_attachment_url(
    db_conn: psycopg.Connection, tmp_path: Path
) -> None:
    """End-to-end: parse a multipart/related message with an inline image,
    write attachments via the production path, fetch via get_message, and
    assert the sanitized HTML carries `/v1/attachments/<sha>` — not `cid:`,
    not `src=""`. This is the user-visible promise of #10/#12."""
    parsed = parse_message(_eml.html_with_inline_image())
    jsonb_rows = write_attachments(db_conn, parsed, root=tmp_path)
    assert len(jsonb_rows) == 1
    sha_hex = jsonb_rows[0]["sha256"]

    mid = _seed_msg(
        db_conn,
        message_id="<inline-1@example.com>",
        body_html=parsed.body_html,
        attachments=jsonb_rows,
    )
    db_conn.commit()

    msg = get_message(db_conn, mid, allowed_account_ids=_ANY_ACCOUNT)
    assert msg["body_html"] is not None
    assert f'src="/v1/attachments/{sha_hex}"' in msg["body_html"]
    assert "cid:" not in msg["body_html"]
    assert 'src=""' not in msg["body_html"]
