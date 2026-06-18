# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""ACL filtering of the api-layer accessors.

Each test seeds two accounts plus a user with grants to exactly one of them
and asserts the accessors hide the un-granted account's rows.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import psycopg
import pytest

from localmail.api.accounts import list_accounts, list_folders
from localmail.api.acl import grant_account
from localmail.api.attachments import (
    get_attachment_metadata,
    get_attachment_text,
    open_attachment_bytes,
)
from localmail.api.auth import create_user
from localmail.api.errors import NotFound
from localmail.api.messages import get_message, get_message_raw


def _seed_account(conn: psycopg.Connection, name: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method) "
            "VALUES (%s, %s, 'imap.example.com', 'password') RETURNING id",
            (name, f"{name}@example.com"),
        )
        row = cur.fetchone()
        assert row is not None
        return int(row[0])


def _seed_mailbox(conn: psycopg.Connection, account_id: int, name: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO mailboxes (account_id, name) VALUES (%s, %s) RETURNING id",
            (account_id, name),
        )
        row = cur.fetchone()
        assert row is not None
        return int(row[0])


def _seed_message(
    conn: psycopg.Connection,
    account_id: int,
    subject: str,
    body_text: str = "hi",
    attachments: list[dict] | None = None,
) -> int:
    raw = f"Subject: {subject}\r\n\r\n{body_text}".encode("utf-8")
    raw_sha256 = hashlib.sha256(raw).digest()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO messages "
            "(account_id, message_id, raw_sha256, subject, body_text, "
            " raw_bytes, size_bytes, headers, attachments, date_sent) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s) "
            "RETURNING id",
            (
                account_id,
                f"<{subject}@example.com>",
                raw_sha256,
                subject,
                body_text,
                raw,
                len(raw),
                "{}",
                psycopg.types.json.Jsonb(attachments or []),
                datetime.now(timezone.utc),
            ),
        )
        row = cur.fetchone()
        assert row is not None
        return int(row[0])


def _seed_attachment(
    conn: psycopg.Connection, sha_hex: str, mime: str, body: bytes, tmp_path: Path,
) -> str:
    sha_bytes = bytes.fromhex(sha_hex)
    path = tmp_path / sha_hex
    path.write_bytes(body)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO attachment_blobs (sha256, mime_type, size_bytes, path) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (sha_bytes, mime, len(body), str(path)),
        )
    return sha_hex


@pytest.fixture
def two_accounts(db_conn):
    a_id = _seed_account(db_conn, "acct-a")
    b_id = _seed_account(db_conn, "acct-b")
    db_conn.commit()
    return a_id, b_id


@pytest.fixture
def alice_with_a(db_conn, two_accounts):
    a_id, _ = two_accounts
    uid = create_user(db_conn, "alice", "hunter2")
    grant_account(db_conn, uid, a_id)
    db_conn.commit()
    return uid


def test_list_accounts_returns_only_granted_accounts(db_conn, two_accounts, alice_with_a):
    a_id, _b_id = two_accounts
    rows = list_accounts(db_conn, allowed_account_ids=[a_id])
    assert [r["id"] for r in rows] == [str(a_id)]


def test_list_accounts_marks_is_shared_for_multi_account_user(
    db_conn, two_accounts,
):
    a_id, b_id = two_accounts
    rows = list_accounts(db_conn, allowed_account_ids=[a_id, b_id])
    assert all(r["capabilities"]["is_shared"] is True for r in rows)


def test_list_accounts_is_shared_false_for_single_account_user(
    db_conn, two_accounts,
):
    a_id, _b_id = two_accounts
    rows = list_accounts(db_conn, allowed_account_ids=[a_id])
    assert rows[0]["capabilities"]["is_shared"] is False


def test_list_accounts_empty_for_user_with_no_grants(db_conn, two_accounts):
    assert list_accounts(db_conn, allowed_account_ids=[]) == []


def test_list_folders_returns_empty_for_ungranted_account(db_conn, two_accounts):
    a_id, b_id = two_accounts
    _seed_mailbox(db_conn, b_id, "INBOX")
    db_conn.commit()
    assert list_folders(db_conn, b_id, allowed_account_ids=[a_id]) == []


def test_list_folders_returns_rows_for_granted_account(db_conn, two_accounts):
    a_id, _b_id = two_accounts
    _seed_mailbox(db_conn, a_id, "INBOX")
    db_conn.commit()
    out = list_folders(db_conn, a_id, allowed_account_ids=[a_id])
    assert len(out) == 1
    assert out[0]["name"] == "INBOX"


def test_get_message_raises_notfound_when_account_not_in_acl(db_conn, two_accounts):
    _a_id, b_id = two_accounts
    mid = _seed_message(db_conn, b_id, "private")
    db_conn.commit()
    with pytest.raises(NotFound):
        get_message(db_conn, mid, allowed_account_ids=[_a_id])


def test_get_message_returns_dict_for_granted_account(db_conn, two_accounts):
    a_id, _ = two_accounts
    mid = _seed_message(db_conn, a_id, "hello")
    db_conn.commit()
    msg = get_message(db_conn, mid, allowed_account_ids=[a_id])
    assert msg["subject"] == "hello"


def test_get_message_empty_acl_raises_notfound(db_conn, two_accounts):
    a_id, _ = two_accounts
    mid = _seed_message(db_conn, a_id, "hello")
    db_conn.commit()
    with pytest.raises(NotFound):
        get_message(db_conn, mid, allowed_account_ids=[])


def test_get_message_raw_filters_by_acl(db_conn, two_accounts):
    _a_id, b_id = two_accounts
    mid = _seed_message(db_conn, b_id, "private")
    db_conn.commit()
    with pytest.raises(NotFound):
        get_message_raw(db_conn, mid, allowed_account_ids=[_a_id])
    assert get_message_raw(db_conn, mid, allowed_account_ids=[b_id]).startswith(b"Subject")


def test_attachment_filters_by_acl(db_conn, two_accounts, tmp_path):
    a_id, b_id = two_accounts
    blob = b"shared bytes"
    sha_hex = hashlib.sha256(blob).hexdigest()
    _seed_attachment(db_conn, sha_hex, "text/plain", blob, tmp_path)
    _seed_message(db_conn, b_id, "carrier",
                  attachments=[{"filename": "x.txt", "sha256": sha_hex}])
    db_conn.commit()

    # Alice has grants to A only — she must not see the blob even though it
    # exists on disk, because no message in an A-granted account references it.
    with pytest.raises(NotFound):
        get_attachment_metadata(db_conn, sha_hex, allowed_account_ids=[a_id])
    with pytest.raises(NotFound):
        open_attachment_bytes(db_conn, sha_hex, allowed_account_ids=[a_id])

    # Bob (grants to B) can read the metadata.
    meta = get_attachment_metadata(db_conn, sha_hex, allowed_account_ids=[b_id])
    assert meta["mime_type"] == "text/plain"
    fp, mime, size = open_attachment_bytes(db_conn, sha_hex, allowed_account_ids=[b_id])
    try:
        assert fp.read() == blob
    finally:
        fp.close()
    assert mime == "text/plain"
    assert size == len(blob)


def test_attachment_text_filters_by_acl(db_conn, two_accounts, tmp_path):
    a_id, b_id = two_accounts
    blob = b"shared bytes"
    sha_hex = hashlib.sha256(blob).hexdigest()
    sha_bytes = bytes.fromhex(sha_hex)
    _seed_attachment(db_conn, sha_hex, "text/plain", blob, tmp_path)
    _seed_message(db_conn, b_id, "carrier",
                  attachments=[{"filename": "x.txt", "sha256": sha_hex}])
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO attachment_text (sha256, extracted_text, extractor) "
            "VALUES (%s, %s, 'test')",
            (sha_bytes, "hello world"),
        )
    db_conn.commit()
    with pytest.raises(NotFound):
        get_attachment_text(db_conn, sha_hex, allowed_account_ids=[a_id])
    assert get_attachment_text(db_conn, sha_hex, allowed_account_ids=[b_id]) == "hello world"
