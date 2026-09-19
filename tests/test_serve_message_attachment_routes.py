# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""GET /v1/messages/{id}/attachments/{index}[/text]."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from localmail.serve.app import create_app

_Part = tuple[str | None, bytes, str]  # (filename, payload, mime)


def _seed(
    conn: psycopg.Connection, tmp_path: Path, parts: list[_Part],
) -> tuple[int, list[str]]:
    """One message whose attachments are `parts`, in order. Returns (id, shas)."""
    shas: list[str] = []
    entries: list[dict[str, str]] = []
    with conn.cursor() as cur:
        for filename, payload, mime in parts:
            sha = hashlib.sha256(payload).hexdigest()
            blob = tmp_path / "blobs" / sha[:2] / sha[2:4] / sha
            blob.parent.mkdir(parents=True, exist_ok=True)
            blob.write_bytes(payload)
            cur.execute(
                "INSERT INTO attachment_blobs (sha256, mime_type, size_bytes, path) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (bytes.fromhex(sha), mime, len(payload), str(blob)),
            )
            shas.append(sha)
            entries.append(
                {"sha256": sha} if filename is None
                else {"filename": filename, "sha256": sha}
            )
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method) "
            "VALUES ('acct', 'x@y.test', 'imap.x', 'password') RETURNING id",
        )
        row = cur.fetchone(); assert row is not None
        raw = b"x"
        cur.execute(
            "INSERT INTO messages (account_id, message_id, raw_bytes, raw_sha256, "
            "size_bytes, headers, attachments, date_sent) "
            "VALUES (%s, '<m@x>', %s, %s, 1, '{}'::jsonb, %s, %s) RETURNING id",
            (row[0], raw, hashlib.sha256(raw).digest(),
             psycopg.types.json.Jsonb(entries), datetime.now(timezone.utc)),
        )
        row = cur.fetchone(); assert row is not None
        mid = int(row[0])
    conn.commit()
    return mid, shas


def _client(db_dsn: str) -> TestClient:
    return TestClient(create_app(db_dsn=db_dsn, searcher=None))


def _auth(token: str, **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", **extra}


_SAME_NAME: list[_Part] = [
    ("note.txt", b"file-one", "text/plain"),
    ("note.txt", b"file-two", "text/plain"),
]


def test_each_index_serves_its_own_bytes(
    db_dsn, api_token, db_conn, tmp_path, grant_alice_all_accounts,
) -> None:
    mid, _ = _seed(db_conn, tmp_path, _SAME_NAME)
    grant_alice_all_accounts()
    c = _client(db_dsn)
    assert c.get(f"/v1/messages/{mid}/attachments/0", headers=_auth(api_token)).content == b"file-one"
    assert c.get(f"/v1/messages/{mid}/attachments/1", headers=_auth(api_token)).content == b"file-two"


def test_disposition_carries_the_entrys_own_name(
    db_dsn, api_token, db_conn, tmp_path, grant_alice_all_accounts,
) -> None:
    # Same bytes under two names in one message: the sha route can serve only
    # one of them; the index route serves each entry's own.
    payload = b"\x89PNG-same-bytes"
    mid, shas = _seed(db_conn, tmp_path, [
        ("logo.png", payload, "image/png"),
        ("image001.png", payload, "image/png"),
    ])
    grant_alice_all_accounts()
    c = _client(db_dsn)
    r0 = c.get(f"/v1/messages/{mid}/attachments/0", headers=_auth(api_token))
    r1 = c.get(f"/v1/messages/{mid}/attachments/1", headers=_auth(api_token))
    assert 'filename="logo.png"' in r0.headers["content-disposition"]
    assert 'filename="image001.png"' in r1.headers["content-disposition"]
    # One representation, one validator: the ETag is the blob's.
    assert r0.headers["etag"] == r1.headers["etag"] == f'"{shas[0]}"'


def test_an_unnamed_entry_falls_back_to_the_sha_prefix_name(
    db_dsn, api_token, db_conn, tmp_path, grant_alice_all_accounts,
) -> None:
    mid, shas = _seed(db_conn, tmp_path, [(None, b"anon", "application/pdf")])
    grant_alice_all_accounts()
    r = _client(db_dsn).get(f"/v1/messages/{mid}/attachments/0", headers=_auth(api_token))
    assert f'filename="attachment-{shas[0][:16]}.bin"' in r.headers["content-disposition"]


def test_range_and_416_and_304(
    db_dsn, api_token, db_conn, tmp_path, grant_alice_all_accounts,
) -> None:
    mid, shas = _seed(db_conn, tmp_path, [("d.pdf", b"0123456789", "application/pdf")])
    grant_alice_all_accounts()
    c = _client(db_dsn)
    url = f"/v1/messages/{mid}/attachments/0"
    r = c.get(url, headers=_auth(api_token, Range="bytes=2-5"))
    assert (r.status_code, r.content, r.headers["content-range"]) == (206, b"2345", "bytes 2-5/10")
    r = c.get(url, headers=_auth(api_token, Range="bytes=50-"))
    assert (r.status_code, r.headers["content-range"]) == (416, "bytes */10")
    r = c.get(url, headers=_auth(api_token, **{"If-None-Match": f'"{shas[0]}"'}))
    assert (r.status_code, r.content) == (304, b"")


def test_risky_mime_is_clamped(
    db_dsn, api_token, db_conn, tmp_path, grant_alice_all_accounts,
) -> None:
    mid, _ = _seed(db_conn, tmp_path, [("x.html", b"<script>", "text/html")])
    grant_alice_all_accounts()
    r = _client(db_dsn).get(f"/v1/messages/{mid}/attachments/0", headers=_auth(api_token))
    assert r.headers["content-type"] == "application/octet-stream"


@pytest.mark.parametrize("path", ["attachments/2", "attachments/2147483648"])
def test_absent_index_is_404(
    db_dsn, api_token, db_conn, tmp_path, grant_alice_all_accounts, path: str,
) -> None:
    mid, _ = _seed(db_conn, tmp_path, _SAME_NAME)
    grant_alice_all_accounts()
    r = _client(db_dsn).get(f"/v1/messages/{mid}/{path}", headers=_auth(api_token))
    assert r.status_code == 404


def test_ungranted_is_404(db_dsn, api_token, db_conn, tmp_path) -> None:
    mid, _ = _seed(db_conn, tmp_path, _SAME_NAME)
    c = _client(db_dsn)
    ungranted = c.get(f"/v1/messages/{mid}/attachments/0", headers=_auth(api_token))
    nonexistent = c.get(f"/v1/messages/{mid + 999_999}/attachments/0", headers=_auth(api_token))
    assert ungranted.status_code == nonexistent.status_code == 404
    # No enumeration: an ungranted-but-real message and one that doesn't
    # exist at all read exactly the same, each for its own message id.
    assert ungranted.json()["detail"] == f"attachment 0 of message {mid} not found"
    assert nonexistent.json()["detail"] == f"attachment 0 of message {mid + 999_999} not found"


@pytest.mark.parametrize("index", ["-1", "x", "1.0"])
def test_malformed_index_is_problem_json(
    db_dsn, api_token, db_conn, tmp_path, grant_alice_all_accounts, index: str,
) -> None:
    mid, _ = _seed(db_conn, tmp_path, _SAME_NAME)
    grant_alice_all_accounts()
    r = _client(db_dsn).get(f"/v1/messages/{mid}/attachments/{index}", headers=_auth(api_token))
    assert r.status_code == 400
    assert r.json()["type"] == "/problems/validation-failed"


def test_a_malformed_index_is_a_400_even_ungranted(db_dsn, api_token, db_conn, tmp_path) -> None:
    # index is judged before the connection opens, so it's a 400 even for a
    # caller granted nothing — the bytes-route counterpart of
    # test_a_bad_text_window_is_a_400_even_ungranted.
    mid, _ = _seed(db_conn, tmp_path, _SAME_NAME)
    r = _client(db_dsn).get(f"/v1/messages/{mid}/attachments/-1", headers=_auth(api_token))
    assert r.status_code == 400
    assert r.json()["type"] == "/problems/validation-failed"


def _spy_open(monkeypatch) -> list[object]:
    import localmail.serve.routes.messages as routes
    calls: list[object] = []
    real = routes._open_blob_file_at

    def spy(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(routes, "_open_blob_file_at", spy)
    return calls


def test_a_304_opens_no_file(
    db_dsn, api_token, db_conn, tmp_path, grant_alice_all_accounts, monkeypatch,
) -> None:
    mid, shas = _seed(db_conn, tmp_path, [("d.pdf", b"payload", "application/pdf")])
    grant_alice_all_accounts()
    calls = _spy_open(monkeypatch)
    r = _client(db_dsn).get(
        f"/v1/messages/{mid}/attachments/0",
        headers=_auth(api_token, **{"If-None-Match": f'"{shas[0]}"'}),
    )
    assert r.status_code == 304
    assert calls == []


def test_the_spy_does_intercept_a_served_body(
    db_dsn, api_token, db_conn, tmp_path, grant_alice_all_accounts, monkeypatch,
) -> None:
    """Positive control: without it, `calls == []` above could pass for a spy
    that intercepts nothing."""
    mid, _ = _seed(db_conn, tmp_path, [("d.pdf", b"payload", "application/pdf")])
    grant_alice_all_accounts()
    calls = _spy_open(monkeypatch)
    r = _client(db_dsn).get(f"/v1/messages/{mid}/attachments/0", headers=_auth(api_token))
    assert r.status_code == 200
    assert len(calls) == 1


def _seed_text(conn: psycopg.Connection, sha: str, text: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO attachment_text (sha256, extractor, extracted_text) "
            "VALUES (%s, 'pypdf', %s)",
            (bytes.fromhex(sha), text),
        )
    conn.commit()


def test_text_pages_by_index(
    db_dsn, api_token, db_conn, tmp_path, grant_alice_all_accounts,
) -> None:
    mid, shas = _seed(db_conn, tmp_path, _SAME_NAME)
    _seed_text(db_conn, shas[1], "second document")
    grant_alice_all_accounts()
    r = _client(db_dsn).get(
        f"/v1/messages/{mid}/attachments/1/text?offset=7&limit=4",
        headers=_auth(api_token),
    )
    assert r.status_code == 200
    assert r.json() == {
        "text": "docu", "offset": 7, "limit": 4, "total": 15, "next_offset": 11,
    }


def test_text_without_extraction_is_404(
    db_dsn, api_token, db_conn, tmp_path, grant_alice_all_accounts,
) -> None:
    mid, _ = _seed(db_conn, tmp_path, _SAME_NAME)
    grant_alice_all_accounts()
    r = _client(db_dsn).get(f"/v1/messages/{mid}/attachments/0/text", headers=_auth(api_token))
    assert r.status_code == 404


def test_a_bad_text_window_is_a_400_even_ungranted(
    db_dsn, api_token, db_conn, tmp_path,
) -> None:
    mid, _ = _seed(db_conn, tmp_path, _SAME_NAME)
    r = _client(db_dsn).get(
        f"/v1/messages/{mid}/attachments/0/text?limit=0", headers=_auth(api_token),
    )
    assert r.status_code == 400
    assert r.json()["type"] == "/problems/validation-failed"
