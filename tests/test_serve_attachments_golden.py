# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""What `/v1/attachments/{sha256}` answers, pinned before the streaming rules
moved to `blob_response.py` — so the move can be shown to change nothing.

Written green on the unchanged tree. If a literal here disagrees with the
tree *before* the refactor, the literal is what is wrong: fix it to the
tree's answer, then refactor.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from fastapi.testclient import TestClient

from localmail.serve.app import create_app

_PAYLOAD = b"0123456789"
_SHA = "84d89877f0d4041efb6bf91a16f0248f2fd573e6af05c19f96bedb9f882f7882"
_ETAG = f'"{_SHA}"'
_DISPOSITION = "attachment; filename=\"report.pdf\"; filename*=UTF-8''report.pdf"


def _seed(conn: psycopg.Connection, tmp_path: Path) -> None:
    assert hashlib.sha256(_PAYLOAD).hexdigest() == _SHA
    blob = tmp_path / "blobs" / _SHA[:2] / _SHA[2:4] / _SHA
    blob.parent.mkdir(parents=True)
    blob.write_bytes(_PAYLOAD)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO attachment_blobs (sha256, mime_type, size_bytes, path) "
            "VALUES (%s, 'application/pdf', %s, %s)",
            (bytes.fromhex(_SHA), len(_PAYLOAD), str(blob)),
        )
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method) "
            "VALUES ('golden', 'x@y.test', 'imap.x', 'password') RETURNING id",
        )
        row = cur.fetchone(); assert row is not None
        raw = b"x"
        cur.execute(
            "INSERT INTO messages (account_id, message_id, raw_bytes, raw_sha256, "
            "size_bytes, headers, attachments, date_sent) "
            "VALUES (%s, '<g@x>', %s, %s, 1, '{}'::jsonb, %s, %s)",
            (row[0], raw, hashlib.sha256(raw).digest(),
             psycopg.types.json.Jsonb([{"filename": "report.pdf", "sha256": _SHA}]),
             datetime.now(timezone.utc)),
        )
    conn.commit()


def _get(db_dsn: str, token: str, **headers: str):
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    return c.get(
        f"/v1/attachments/{_SHA}",
        headers={"Authorization": f"Bearer {token}", **headers},
    )


def test_full_200(db_dsn, api_token, db_conn, tmp_path, grant_alice_all_accounts) -> None:
    _seed(db_conn, tmp_path); grant_alice_all_accounts()
    r = _get(db_dsn, api_token)
    assert r.status_code == 200
    assert r.content == _PAYLOAD
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["content-length"] == "10"
    assert r.headers["content-disposition"] == _DISPOSITION
    assert r.headers["accept-ranges"] == "bytes"
    assert r.headers["etag"] == _ETAG
    assert "content-range" not in r.headers


def test_partial_206(db_dsn, api_token, db_conn, tmp_path, grant_alice_all_accounts) -> None:
    _seed(db_conn, tmp_path); grant_alice_all_accounts()
    r = _get(db_dsn, api_token, Range="bytes=2-5")
    assert r.status_code == 206
    assert r.content == b"2345"
    assert r.headers["content-length"] == "4"
    assert r.headers["content-range"] == "bytes 2-5/10"
    assert r.headers["content-disposition"] == _DISPOSITION
    assert r.headers["etag"] == _ETAG


def test_unsatisfiable_416(db_dsn, api_token, db_conn, tmp_path, grant_alice_all_accounts) -> None:
    _seed(db_conn, tmp_path); grant_alice_all_accounts()
    r = _get(db_dsn, api_token, Range="bytes=50-")
    assert r.status_code == 416
    assert r.content == b""
    assert r.headers["content-range"] == "bytes */10"
    assert r.headers["content-disposition"] == _DISPOSITION
    assert r.headers["accept-ranges"] == "bytes"
    assert r.headers["etag"] == _ETAG


def test_not_modified_304(db_dsn, api_token, db_conn, tmp_path, grant_alice_all_accounts) -> None:
    _seed(db_conn, tmp_path); grant_alice_all_accounts()
    r = _get(db_dsn, api_token, **{"If-None-Match": _ETAG})
    assert r.status_code == 304
    assert r.content == b""
    assert r.headers["etag"] == _ETAG
    assert "content-disposition" not in r.headers


def test_the_304_spy_intercepts_when_the_body_is_served(
    db_dsn, api_token, db_conn, tmp_path, grant_alice_all_accounts, monkeypatch,
) -> None:
    """Positive control for `test_304_does_not_call_open_attachment_bytes_or_filename`.

    That test asserts the route module's `_open_blob_file_at` is NOT called on
    a 304. Were the open ever moved out of the route module, its spy would
    stop intercepting and `== []` would pass for nothing. Here the same spy
    must see exactly one call on a 200.
    """
    _seed(db_conn, tmp_path); grant_alice_all_accounts()
    import localmail.serve.routes.attachments as routes
    calls: list[object] = []
    real = routes._open_blob_file_at

    def spy(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(routes, "_open_blob_file_at", spy)
    assert _get(db_dsn, api_token).status_code == 200
    assert len(calls) == 1
