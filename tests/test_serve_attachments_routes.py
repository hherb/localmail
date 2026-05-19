from datetime import datetime, timezone
from pathlib import Path

import hashlib
import logging

import psycopg
from fastapi.testclient import TestClient

from localmail.serve.app import create_app

_SERVE_LOGGER = "localmail.serve"


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


def test_stream_attachment_advertises_accept_ranges_bytes(
    db_dsn: str, api_token: str, db_conn, tmp_path: Path, grant_alice_all_accounts,
) -> None:
    """Range support is on (#54): full-GET responses must advertise it so
    clients can resume on dropped connections and seek into media."""
    sha = "e8" * 32
    _seed_blob_with_carrier(db_conn, tmp_path, sha, b"x")
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(f"/v1/attachments/{sha}", headers={"Authorization": f"Bearer {api_token}"})
    assert r.status_code == 200
    assert r.headers["accept-ranges"] == "bytes"


_PDF_PAYLOAD = b"%PDF-content-here-some-bytes-for-range-tests-XYZ"


def test_range_first_ten_bytes_returns_206(
    db_dsn: str, api_token: str, db_conn, tmp_path: Path, grant_alice_all_accounts,
) -> None:
    """``Range: bytes=0-9`` → 206 Partial Content with the first 10 bytes."""
    sha = "f1" * 32
    _seed_blob_with_carrier(db_conn, tmp_path, sha, _PDF_PAYLOAD)
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(
        f"/v1/attachments/{sha}",
        headers={"Authorization": f"Bearer {api_token}", "Range": "bytes=0-9"},
    )
    assert r.status_code == 206
    assert r.content == _PDF_PAYLOAD[:10]
    assert r.headers["content-range"] == f"bytes 0-9/{len(_PDF_PAYLOAD)}"
    assert r.headers["content-length"] == "10"
    assert r.headers["accept-ranges"] == "bytes"


def test_range_open_ended_from_offset_returns_206(
    db_dsn: str, api_token: str, db_conn, tmp_path: Path, grant_alice_all_accounts,
) -> None:
    """``Range: bytes=10-`` → 206 with bytes 10..end."""
    sha = "f2" * 32
    _seed_blob_with_carrier(db_conn, tmp_path, sha, _PDF_PAYLOAD)
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(
        f"/v1/attachments/{sha}",
        headers={"Authorization": f"Bearer {api_token}", "Range": "bytes=10-"},
    )
    assert r.status_code == 206
    assert r.content == _PDF_PAYLOAD[10:]
    last = len(_PDF_PAYLOAD) - 1
    assert r.headers["content-range"] == f"bytes 10-{last}/{len(_PDF_PAYLOAD)}"
    assert r.headers["content-length"] == str(len(_PDF_PAYLOAD) - 10)


def test_range_suffix_returns_trailing_bytes(
    db_dsn: str, api_token: str, db_conn, tmp_path: Path, grant_alice_all_accounts,
) -> None:
    """``Range: bytes=-10`` → 206 with the trailing 10 bytes."""
    sha = "f3" * 32
    _seed_blob_with_carrier(db_conn, tmp_path, sha, _PDF_PAYLOAD)
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(
        f"/v1/attachments/{sha}",
        headers={"Authorization": f"Bearer {api_token}", "Range": "bytes=-10"},
    )
    assert r.status_code == 206
    assert r.content == _PDF_PAYLOAD[-10:]
    total = len(_PDF_PAYLOAD)
    assert r.headers["content-range"] == f"bytes {total - 10}-{total - 1}/{total}"
    assert r.headers["content-length"] == "10"


def test_range_start_past_eof_returns_416(
    db_dsn: str, api_token: str, db_conn, tmp_path: Path, grant_alice_all_accounts,
) -> None:
    """A start position past EOF is unsatisfiable → 416 with ``bytes */N``."""
    sha = "f4" * 32
    _seed_blob_with_carrier(db_conn, tmp_path, sha, _PDF_PAYLOAD)
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(
        f"/v1/attachments/{sha}",
        headers={"Authorization": f"Bearer {api_token}", "Range": "bytes=9999999-"},
    )
    assert r.status_code == 416
    assert r.headers["content-range"] == f"bytes */{len(_PDF_PAYLOAD)}"


def test_range_end_past_eof_is_clamped(
    db_dsn: str, api_token: str, db_conn, tmp_path: Path, grant_alice_all_accounts,
) -> None:
    """``bytes=0-<huge>`` with start in range → 206 with end clamped to size-1
    (RFC 9110 §14.1.2)."""
    sha = "f5" * 32
    _seed_blob_with_carrier(db_conn, tmp_path, sha, _PDF_PAYLOAD)
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(
        f"/v1/attachments/{sha}",
        headers={"Authorization": f"Bearer {api_token}", "Range": "bytes=0-9999999"},
    )
    assert r.status_code == 206
    assert r.content == _PDF_PAYLOAD
    total = len(_PDF_PAYLOAD)
    assert r.headers["content-range"] == f"bytes 0-{total - 1}/{total}"


def test_malformed_range_falls_through_to_200(
    db_dsn: str, api_token: str, db_conn, tmp_path: Path, grant_alice_all_accounts,
) -> None:
    """RFC 9110: servers MAY ignore unparseable Range — we serve full 200."""
    sha = "f6" * 32
    _seed_blob_with_carrier(db_conn, tmp_path, sha, _PDF_PAYLOAD)
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(
        f"/v1/attachments/{sha}",
        headers={"Authorization": f"Bearer {api_token}", "Range": "bytes=abc-"},
    )
    assert r.status_code == 200
    assert r.content == _PDF_PAYLOAD


def test_multi_range_falls_through_to_200(
    db_dsn: str, api_token: str, db_conn, tmp_path: Path, grant_alice_all_accounts,
) -> None:
    """Multi-range requests fall through to 200 — we don't emit multipart."""
    sha = "f7" * 32
    _seed_blob_with_carrier(db_conn, tmp_path, sha, _PDF_PAYLOAD)
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(
        f"/v1/attachments/{sha}",
        headers={"Authorization": f"Bearer {api_token}", "Range": "bytes=0-9,20-29"},
    )
    assert r.status_code == 200
    assert r.content == _PDF_PAYLOAD


def test_range_request_preserves_content_disposition_and_mime_clamp(
    db_dsn: str, api_token: str, db_conn, tmp_path: Path, grant_alice_all_accounts,
) -> None:
    """A 206 must go through the same #32 path as a full GET: download-only
    Content-Disposition + risky-MIME clamp."""
    sha = "f8" * 32
    _seed_blob_with_carrier(
        db_conn, tmp_path, sha, b"<script>alert(1)</script>" * 4,
        filename="evil.html", mime="text/html",
    )
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(
        f"/v1/attachments/{sha}",
        headers={"Authorization": f"Bearer {api_token}", "Range": "bytes=0-9"},
    )
    assert r.status_code == 206
    assert r.headers["content-type"].startswith("application/octet-stream")
    cd = r.headers["content-disposition"]
    assert cd.startswith("attachment;")
    assert "filename=" in cd


def test_416_response_preserves_content_disposition(
    db_dsn: str, api_token: str, db_conn, tmp_path: Path, grant_alice_all_accounts,
) -> None:
    """A 416 must still set Content-Disposition: attachment so a downstream
    proxy or naive client can't be tricked into rendering an error body
    inline if it ever has one."""
    sha = "f9" * 32
    _seed_blob_with_carrier(db_conn, tmp_path, sha, _PDF_PAYLOAD)
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(
        f"/v1/attachments/{sha}",
        headers={"Authorization": f"Bearer {api_token}", "Range": "bytes=9999999-"},
    )
    assert r.status_code == 416
    assert r.headers["content-disposition"].startswith("attachment;")


def test_range_spans_multiple_chunk_boundaries(
    db_dsn: str, api_token: str, db_conn, tmp_path: Path, grant_alice_all_accounts,
) -> None:
    """Ranges larger than _CHUNK (64 KiB) must reassemble correctly across
    multiple read iterations — guards against off-by-one in the
    ``remaining -= len(chunk)`` accounting in ``_stream_range``."""
    sha = "fa" * 32
    chunk_size = 64 * 1024
    payload = bytes((i % 256) for i in range(chunk_size * 3 + 1234))
    _seed_blob_with_carrier(db_conn, tmp_path, sha, payload)
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    start = chunk_size - 100
    end = chunk_size * 2 + 500
    r = c.get(
        f"/v1/attachments/{sha}",
        headers={
            "Authorization": f"Bearer {api_token}",
            "Range": f"bytes={start}-{end}",
        },
    )
    assert r.status_code == 206
    assert r.content == payload[start:end + 1]
    assert r.headers["content-length"] == str(end - start + 1)
    assert r.headers["content-range"] == f"bytes {start}-{end}/{len(payload)}"


def test_416_response_clamps_risky_mime(
    db_dsn: str, api_token: str, db_conn, tmp_path: Path, grant_alice_all_accounts,
) -> None:
    """A 416 must clamp script-executable MIME types to octet-stream — same
    defense-in-depth as 200 / 206. The 416 body is empty so this is belt-and-
    braces, but matches the route docstring's promise."""
    sha = "fb" * 32
    _seed_blob_with_carrier(
        db_conn, tmp_path, sha, _PDF_PAYLOAD,
        filename="evil.html", mime="text/html",
    )
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(
        f"/v1/attachments/{sha}",
        headers={"Authorization": f"Bearer {api_token}", "Range": "bytes=9999999-"},
    )
    assert r.status_code == 416
    assert r.headers["content-type"].startswith("application/octet-stream")


def _truncate_blob_on_disk(tmp_path: Path, sha_hex: str, new_size: int) -> None:
    """Shrink the on-disk blob without touching the DB-recorded size.

    Simulates filesystem corruption, partial sync, or manual `rm`+restore —
    the DB still says ``size_bytes = N`` but the file holds ``new_size < N``.
    """
    p = tmp_path / "blobs" / sha_hex[:2] / sha_hex[2:4] / sha_hex
    data = p.read_bytes()
    assert new_size < len(data), "test set-up bug: new_size must shrink the file"
    p.write_bytes(data[:new_size])


def _truncation_warnings_for(records: list[logging.LogRecord], sha_hex: str) -> list[str]:
    """Pull WARNING messages for ``sha_hex`` out of caplog records."""
    return [
        rec.getMessage() for rec in records
        if rec.levelno == logging.WARNING and sha_hex in rec.getMessage()
    ]


def test_stream_full_logs_warning_when_on_disk_blob_truncated(
    db_dsn: str, api_token: str, db_conn, tmp_path: Path,
    grant_alice_all_accounts, caplog,
) -> None:
    """If the on-disk blob is shorter than ``attachment_blobs.size_bytes``,
    the full-GET streamer must log a WARNING with sha + expected + actual
    bytes (#58). Headers are already flushed, so logging is the only signal
    ops have. The response body is the short content; we don't try to patch
    Content-Length after the fact."""
    sha = "1a" * 32
    full_size = len(_PDF_PAYLOAD)
    truncated_size = full_size // 2
    _seed_blob_with_carrier(db_conn, tmp_path, sha, _PDF_PAYLOAD)
    _truncate_blob_on_disk(tmp_path, sha, truncated_size)
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    with caplog.at_level(logging.WARNING, logger=_SERVE_LOGGER):
        r = c.get(
            f"/v1/attachments/{sha}",
            headers={"Authorization": f"Bearer {api_token}"},
        )
    assert r.status_code == 200
    assert r.content == _PDF_PAYLOAD[:truncated_size]
    warnings = _truncation_warnings_for(caplog.records, sha)
    assert len(warnings) == 1, f"expected one WARNING, got {warnings!r}"
    msg = warnings[0]
    assert str(full_size) in msg, f"expected size {full_size} missing from {msg!r}"
    assert str(truncated_size) in msg, f"sent count {truncated_size} missing from {msg!r}"


def test_stream_range_logs_warning_when_on_disk_blob_truncated(
    db_dsn: str, api_token: str, db_conn, tmp_path: Path,
    grant_alice_all_accounts, caplog,
) -> None:
    """Same correctness contract as the full-GET path, but on the 206 path
    (#58). The Range header asks for the whole file; on-disk is truncated;
    streamer must log expected slice length and actual bytes sent."""
    sha = "1b" * 32
    full_size = len(_PDF_PAYLOAD)
    truncated_size = full_size // 2
    _seed_blob_with_carrier(db_conn, tmp_path, sha, _PDF_PAYLOAD)
    _truncate_blob_on_disk(tmp_path, sha, truncated_size)
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    last = full_size - 1
    with caplog.at_level(logging.WARNING, logger=_SERVE_LOGGER):
        r = c.get(
            f"/v1/attachments/{sha}",
            headers={
                "Authorization": f"Bearer {api_token}",
                "Range": f"bytes=0-{last}",
            },
        )
    assert r.status_code == 206
    assert r.content == _PDF_PAYLOAD[:truncated_size]
    warnings = _truncation_warnings_for(caplog.records, sha)
    assert len(warnings) == 1, f"expected one WARNING, got {warnings!r}"
    msg = warnings[0]
    assert str(full_size) in msg, f"expected range length {full_size} missing from {msg!r}"
    assert str(truncated_size) in msg, f"sent count {truncated_size} missing from {msg!r}"


def test_stream_full_does_not_warn_when_blob_matches_db_size(
    db_dsn: str, api_token: str, db_conn, tmp_path: Path,
    grant_alice_all_accounts, caplog,
) -> None:
    """Happy path: no truncation, no spurious WARNINGs. Guards against a
    fix that fires the warning on every download (#58)."""
    sha = "1c" * 32
    _seed_blob_with_carrier(db_conn, tmp_path, sha, _PDF_PAYLOAD)
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    with caplog.at_level(logging.WARNING, logger=_SERVE_LOGGER):
        r = c.get(
            f"/v1/attachments/{sha}",
            headers={"Authorization": f"Bearer {api_token}"},
        )
    assert r.status_code == 200
    assert r.content == _PDF_PAYLOAD
    assert _truncation_warnings_for(caplog.records, sha) == []


def test_stream_range_does_not_warn_when_slice_is_fully_satisfiable(
    db_dsn: str, api_token: str, db_conn, tmp_path: Path,
    grant_alice_all_accounts, caplog,
) -> None:
    """Happy path: 206 with a satisfiable slice over an intact blob must
    not log a WARNING (#58)."""
    sha = "1d" * 32
    _seed_blob_with_carrier(db_conn, tmp_path, sha, _PDF_PAYLOAD)
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    with caplog.at_level(logging.WARNING, logger=_SERVE_LOGGER):
        r = c.get(
            f"/v1/attachments/{sha}",
            headers={"Authorization": f"Bearer {api_token}", "Range": "bytes=0-9"},
        )
    assert r.status_code == 206
    assert r.content == _PDF_PAYLOAD[:10]
    assert _truncation_warnings_for(caplog.records, sha) == []
