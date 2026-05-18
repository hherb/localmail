from pathlib import Path

import psycopg
from fastapi.testclient import TestClient

from localmail.serve.app import create_app


def _seed_blob(conn: psycopg.Connection, tmp_path: Path, sha_hex: str, payload: bytes) -> None:
    fanout = tmp_path / "blobs" / sha_hex[:2] / sha_hex[2:4]
    fanout.mkdir(parents=True, exist_ok=True)
    p = fanout / sha_hex
    p.write_bytes(payload)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO attachment_blobs (sha256, mime_type, size_bytes, path) VALUES (%s, %s, %s, %s)",
            (bytes.fromhex(sha_hex), "application/pdf", len(payload), str(p)),
        )
    conn.commit()


def test_stream_attachment_bytes(db_dsn: str, api_token: str, db_conn, tmp_path: Path) -> None:
    sha = "aa" * 32
    _seed_blob(db_conn, tmp_path, sha, b"%PDF-content")
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(f"/v1/attachments/{sha}", headers={"Authorization": f"Bearer {api_token}"})
    assert r.status_code == 200
    assert r.content == b"%PDF-content"
    assert r.headers["content-type"] == "application/pdf"


def test_attachment_not_found(db_dsn: str, api_token: str) -> None:
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(f"/v1/attachments/{'bb' * 32}", headers={"Authorization": f"Bearer {api_token}"})
    assert r.status_code == 404


def test_attachment_text(db_dsn: str, api_token: str, db_conn, tmp_path: Path) -> None:
    sha = "cc" * 32
    _seed_blob(db_conn, tmp_path, sha, b"%PDF")
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO attachment_text (sha256, extractor, extracted_text) VALUES (%s, %s, %s)",
            (bytes.fromhex(sha), "pypdf", "Hello world"),
        )
    db_conn.commit()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(f"/v1/attachments/{sha}/text", headers={"Authorization": f"Bearer {api_token}"})
    assert r.status_code == 200
    assert r.json() == {"text": "Hello world"}


def test_attachment_text_not_extracted(db_dsn: str, api_token: str, db_conn, tmp_path: Path) -> None:
    sha = "dd" * 32
    _seed_blob(db_conn, tmp_path, sha, b"%PDF")
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(f"/v1/attachments/{sha}/text", headers={"Authorization": f"Bearer {api_token}"})
    assert r.status_code == 404
