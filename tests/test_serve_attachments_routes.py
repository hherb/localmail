from datetime import datetime, timezone
from pathlib import Path

import hashlib
import psycopg
from fastapi.testclient import TestClient

from localmail.serve.app import create_app


def _seed_blob_with_carrier(
    conn: psycopg.Connection, tmp_path: Path, sha_hex: str, payload: bytes,
) -> int:
    """Seed the blob row + on-disk file + a carrying message + an account.

    Returns the account_id of the carrier (the test then grants alice access
    to it so the ACL filter on `/v1/attachments/...` lets the bytes through).
    """
    fanout = tmp_path / "blobs" / sha_hex[:2] / sha_hex[2:4]
    fanout.mkdir(parents=True, exist_ok=True)
    p = fanout / sha_hex
    p.write_bytes(payload)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO attachment_blobs (sha256, mime_type, size_bytes, path) "
            "VALUES (%s, %s, %s, %s)",
            (bytes.fromhex(sha_hex), "application/pdf", len(payload), str(p)),
        )
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method) "
            "VALUES (%s, 'x@y.test', 'imap.x', 'password') RETURNING id",
            (f"acct-{sha_hex[:8]}",),
        )
        row = cur.fetchone()
        assert row is not None
        aid = int(row[0])
        raw = b"x"
        cur.execute(
            "INSERT INTO messages (account_id, message_id, raw_bytes, raw_sha256, "
            "size_bytes, headers, attachments, date_sent) "
            "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)",
            (
                aid, f"<{sha_hex}@x>", raw, hashlib.sha256(raw).digest(), 1, "{}",
                psycopg.types.json.Jsonb([{"filename": "x.pdf", "sha256": sha_hex}]),
                datetime.now(timezone.utc),
            ),
        )
    conn.commit()
    return aid


def test_stream_attachment_bytes(
    db_dsn: str, api_token: str, db_conn, tmp_path: Path, grant_alice_all_accounts,
) -> None:
    sha = "aa" * 32
    _seed_blob_with_carrier(db_conn, tmp_path, sha, b"%PDF-content")
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(f"/v1/attachments/{sha}", headers={"Authorization": f"Bearer {api_token}"})
    assert r.status_code == 200
    assert r.content == b"%PDF-content"
    assert r.headers["content-type"] == "application/pdf"


def test_attachment_not_found(db_dsn: str, api_token: str) -> None:
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(f"/v1/attachments/{'bb' * 32}", headers={"Authorization": f"Bearer {api_token}"})
    assert r.status_code == 404


def test_attachment_text(
    db_dsn: str, api_token: str, db_conn, tmp_path: Path, grant_alice_all_accounts,
) -> None:
    sha = "cc" * 32
    _seed_blob_with_carrier(db_conn, tmp_path, sha, b"%PDF")
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO attachment_text (sha256, extractor, extracted_text) VALUES (%s, %s, %s)",
            (bytes.fromhex(sha), "pypdf", "Hello world"),
        )
    db_conn.commit()
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(f"/v1/attachments/{sha}/text", headers={"Authorization": f"Bearer {api_token}"})
    assert r.status_code == 200
    assert r.json() == {"text": "Hello world"}


def test_attachment_text_not_extracted(
    db_dsn: str, api_token: str, db_conn, tmp_path: Path, grant_alice_all_accounts,
) -> None:
    sha = "dd" * 32
    _seed_blob_with_carrier(db_conn, tmp_path, sha, b"%PDF")
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(f"/v1/attachments/{sha}/text", headers={"Authorization": f"Bearer {api_token}"})
    assert r.status_code == 404
