from datetime import datetime, timezone
from pathlib import Path

import hashlib
import psycopg
from fastapi.testclient import TestClient

from localmail.serve.app import create_app


def _seed_blob_with_carrier(
    conn: psycopg.Connection,
    tmp_path: Path,
    sha_hex: str,
    payload: bytes,
    *,
    filename: str | None = "x.pdf",
    mime: str = "application/pdf",
) -> int:
    """Seed the blob row + on-disk file + a carrying message + an account.

    Returns the account_id of the carrier (the test then grants alice access
    to it so the ACL filter on `/v1/attachments/...` lets the bytes through).

    Pass filename=None to seed a message whose JSONB attachment entry has no
    'filename' key (exercises the route's fallback path).
    """
    fanout = tmp_path / "blobs" / sha_hex[:2] / sha_hex[2:4]
    fanout.mkdir(parents=True, exist_ok=True)
    p = fanout / sha_hex
    p.write_bytes(payload)
    if filename is None:
        att_entry: dict[str, str] = {"sha256": sha_hex}
    else:
        att_entry = {"filename": filename, "sha256": sha_hex}
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO attachment_blobs (sha256, mime_type, size_bytes, path) "
            "VALUES (%s, %s, %s, %s)",
            (bytes.fromhex(sha_hex), mime, len(payload), str(p)),
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
                psycopg.types.json.Jsonb([att_entry]),
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


def test_stream_attachment_emits_content_disposition_attachment(
    db_dsn: str, api_token: str, db_conn, tmp_path: Path, grant_alice_all_accounts,
) -> None:
    """Forces browser-download path to neutralize the inline-render XSS sink (#32)."""
    sha = "e1" * 32
    _seed_blob_with_carrier(db_conn, tmp_path, sha, b"%PDF-content")
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(f"/v1/attachments/{sha}", headers={"Authorization": f"Bearer {api_token}"})
    assert r.status_code == 200
    cd = r.headers["content-disposition"]
    assert cd.startswith("attachment;")
    assert 'filename="x.pdf"' in cd
    assert "filename*=UTF-8''x.pdf" in cd


def test_stream_attachment_unicode_filename_uses_rfc5987(
    db_dsn: str, api_token: str, db_conn, tmp_path: Path, grant_alice_all_accounts,
) -> None:
    """Non-ASCII filenames must use the RFC 5987 filename*= form so browsers
    can recover the original name; the plain filename= form must be sanitised
    ASCII so legacy clients don't choke."""
    sha = "e2" * 32
    _seed_blob_with_carrier(db_conn, tmp_path, sha, b"x", filename="naïve résumé.pdf")
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(f"/v1/attachments/{sha}", headers={"Authorization": f"Bearer {api_token}"})
    assert r.status_code == 200
    cd = r.headers["content-disposition"]
    assert "filename*=UTF-8''na%C3%AFve%20r%C3%A9sum%C3%A9.pdf" in cd
    assert 'filename="' in cd
    # ASCII fallback must not contain raw non-ASCII bytes.
    quoted = cd.split('filename="', 1)[1].split('"', 1)[0]
    assert quoted.isascii(), f"ASCII fallback contained non-ASCII: {quoted!r}"


def test_stream_attachment_falls_back_when_no_filename(
    db_dsn: str, api_token: str, db_conn, tmp_path: Path, grant_alice_all_accounts,
) -> None:
    """If the JSONB row has no filename key, fall back to a deterministic
    sha-prefix name so the response still has a download-safe filename."""
    sha = "e3" * 32
    _seed_blob_with_carrier(db_conn, tmp_path, sha, b"x", filename=None)
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(f"/v1/attachments/{sha}", headers={"Authorization": f"Bearer {api_token}"})
    assert r.status_code == 200
    cd = r.headers["content-disposition"]
    assert f'filename="attachment-{sha[:16]}.bin"' in cd


def test_stream_attachment_sanitises_quote_in_filename(
    db_dsn: str, api_token: str, db_conn, tmp_path: Path, grant_alice_all_accounts,
) -> None:
    """A filename containing a `"` would break the quoted-string header if
    emitted literally; sanitise it in the ASCII fallback. The filename*= form
    percent-encodes everything so it must contain the encoded original."""
    sha = "e4" * 32
    _seed_blob_with_carrier(db_conn, tmp_path, sha, b"x", filename='ev"il.pdf')
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(f"/v1/attachments/{sha}", headers={"Authorization": f"Bearer {api_token}"})
    assert r.status_code == 200
    cd = r.headers["content-disposition"]
    # ASCII fallback: the literal `"` must be replaced.
    quoted = cd.split('filename="', 1)[1].split('"', 1)[0]
    assert '"' not in quoted
    # RFC 5987 form: `"` → %22.
    assert "filename*=UTF-8''ev%22il.pdf" in cd


def test_stream_attachment_clamps_html_mime_to_octet_stream(
    db_dsn: str, api_token: str, db_conn, tmp_path: Path, grant_alice_all_accounts,
) -> None:
    """A stored text/html blob must NOT be served as text/html — defense in
    depth alongside Content-Disposition: attachment, since some browsers
    still sniff."""
    sha = "e5" * 32
    _seed_blob_with_carrier(
        db_conn, tmp_path, sha, b"<script>alert(1)</script>",
        filename="evil.html", mime="text/html",
    )
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(f"/v1/attachments/{sha}", headers={"Authorization": f"Bearer {api_token}"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/octet-stream")


def test_stream_attachment_clamps_svg_mime(
    db_dsn: str, api_token: str, db_conn, tmp_path: Path, grant_alice_all_accounts,
) -> None:
    """SVG can carry inline script — same XSS sink as HTML, same clamp."""
    sha = "e6" * 32
    _seed_blob_with_carrier(
        db_conn, tmp_path, sha, b"<svg><script>x</script></svg>",
        filename="evil.svg", mime="image/svg+xml",
    )
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(f"/v1/attachments/{sha}", headers={"Authorization": f"Bearer {api_token}"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/octet-stream")


def test_stream_attachment_preserves_safe_mimes(
    db_dsn: str, api_token: str, db_conn, tmp_path: Path, grant_alice_all_accounts,
) -> None:
    """PDFs, images, and plain text aren't XSS sinks — their MIME passes through."""
    sha = "e7" * 32
    _seed_blob_with_carrier(
        db_conn, tmp_path, sha, b"\x89PNG\r\n\x1a\n",
        filename="ok.png", mime="image/png",
    )
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(f"/v1/attachments/{sha}", headers={"Authorization": f"Bearer {api_token}"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/png")


def test_stream_attachment_accept_ranges_none(
    db_dsn: str, api_token: str, db_conn, tmp_path: Path, grant_alice_all_accounts,
) -> None:
    """We don't (yet) implement Range — be explicit so clients don't hang on
    retry expecting partial-content."""
    sha = "e8" * 32
    _seed_blob_with_carrier(db_conn, tmp_path, sha, b"x")
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(f"/v1/attachments/{sha}", headers={"Authorization": f"Bearer {api_token}"})
    assert r.status_code == 200
    assert r.headers["accept-ranges"] == "none"
