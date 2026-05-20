"""Integration tests for ETag + If-None-Match + If-Range on the
attachment route (#59). Companion to:

* `tests/test_api_conditional.py` — pure parser units.
* `tests/test_serve_attachments_routes.py` — #32 force-download, #54
  Range, #58 short-read WARNING.

Kept in its own file to honour the project's soft 500-line guideline —
the routes test file is already past the limit and was already flagged
as a split candidate in `NEXT_SESSION.md`.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import hashlib

import psycopg
from fastapi.testclient import TestClient

from localmail.serve.app import create_app

_PDF_PAYLOAD = b"%PDF-content-here-some-bytes-for-conditional-tests-ABC"
_HALF = len(_PDF_PAYLOAD) // 2


def _seed_blob_with_carrier(
    conn: psycopg.Connection,
    tmp_path: Path,
    sha_hex: str,
    payload: bytes,
    *,
    filename: str | None = "x.pdf",
    mime: str = "application/pdf",
) -> int:
    """Seed a blob row + on-disk file + a carrying message + an account.

    Returns the account_id so the test can wire `grant_alice_all_accounts`
    into place before issuing the request. Mirrors the helper in
    `test_serve_attachments_routes.py` — duplicated rather than imported
    so neither file owns a fixture the other has to follow.
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


def _strong_etag(sha_hex: str) -> str:
    return f'"{sha_hex}"'


# ---------- ETag emission on every status code -------------------------------


def test_etag_present_on_full_get_200(
    db_dsn: str, api_token: str, db_conn, tmp_path: Path, grant_alice_all_accounts,
) -> None:
    """A 200 must advertise `ETag: "<sha>"`. SHA-keyed URLs make this
    free and never-changing — proxies and clients can cache on it
    indefinitely without risk of a stale body."""
    sha = "10" * 32
    _seed_blob_with_carrier(db_conn, tmp_path, sha, _PDF_PAYLOAD)
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(
        f"/v1/attachments/{sha}",
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 200
    assert r.headers["etag"] == _strong_etag(sha)


def test_etag_present_on_206_partial_content(
    db_dsn: str, api_token: str, db_conn, tmp_path: Path, grant_alice_all_accounts,
) -> None:
    """The ETag must be identical on 206 — the entity didn't change just
    because the client sliced it. Same value lets a downstream cache
    coalesce range responses against full ones."""
    sha = "11" * 32
    _seed_blob_with_carrier(db_conn, tmp_path, sha, _PDF_PAYLOAD)
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(
        f"/v1/attachments/{sha}",
        headers={"Authorization": f"Bearer {api_token}", "Range": "bytes=0-9"},
    )
    assert r.status_code == 206
    assert r.headers["etag"] == _strong_etag(sha)


def test_etag_present_on_416_unsatisfiable_range(
    db_dsn: str, api_token: str, db_conn, tmp_path: Path, grant_alice_all_accounts,
) -> None:
    """416 still represents the same entity — emit the same ETag so a
    well-behaved client can decide it has a stale cache and retry without
    Range rather than re-fetching speculatively."""
    sha = "12" * 32
    _seed_blob_with_carrier(db_conn, tmp_path, sha, _PDF_PAYLOAD)
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(
        f"/v1/attachments/{sha}",
        headers={"Authorization": f"Bearer {api_token}", "Range": "bytes=9999999-"},
    )
    assert r.status_code == 416
    assert r.headers["etag"] == _strong_etag(sha)


# ---------- If-None-Match → 304 ----------------------------------------------


def test_if_none_match_with_current_etag_returns_304(
    db_dsn: str, api_token: str, db_conn, tmp_path: Path, grant_alice_all_accounts,
) -> None:
    """Conditional GET with a matching ETag → 304 Not Modified, empty
    body, ETag-only headers. Lets a downstream proxy or browser skip
    the bytes when its cache is already current. Per RFC 9110 §15.4.5
    a 304 MUST NOT include Content-Disposition / Accept-Ranges /
    Content-Length — guards against a regression that leaks the
    force-download headers onto the cache-hit response."""
    sha = "20" * 32
    _seed_blob_with_carrier(db_conn, tmp_path, sha, _PDF_PAYLOAD)
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(
        f"/v1/attachments/{sha}",
        headers={
            "Authorization": f"Bearer {api_token}",
            "If-None-Match": _strong_etag(sha),
        },
    )
    assert r.status_code == 304
    assert r.content == b""
    assert r.headers["etag"] == _strong_etag(sha)
    assert "content-disposition" not in r.headers
    assert "accept-ranges" not in r.headers
    assert "content-length" not in r.headers


def test_if_none_match_with_weak_etag_returns_304(
    db_dsn: str, api_token: str, db_conn, tmp_path: Path, grant_alice_all_accounts,
) -> None:
    """RFC 9110 §13.1.2 mandates **weak** comparison: a client sending
    `W/"<sha>"` must still see 304. Caches and CDNs that downgrade strong
    to weak under the hood need this to work."""
    sha = "21" * 32
    _seed_blob_with_carrier(db_conn, tmp_path, sha, _PDF_PAYLOAD)
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(
        f"/v1/attachments/{sha}",
        headers={
            "Authorization": f"Bearer {api_token}",
            "If-None-Match": f'W/"{sha}"',
        },
    )
    assert r.status_code == 304


def test_if_none_match_star_returns_304(
    db_dsn: str, api_token: str, db_conn, tmp_path: Path, grant_alice_all_accounts,
) -> None:
    """`If-None-Match: *` matches any current representation → 304."""
    sha = "22" * 32
    _seed_blob_with_carrier(db_conn, tmp_path, sha, _PDF_PAYLOAD)
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(
        f"/v1/attachments/{sha}",
        headers={"Authorization": f"Bearer {api_token}", "If-None-Match": "*"},
    )
    assert r.status_code == 304


def test_if_none_match_with_other_etag_returns_200_with_body(
    db_dsn: str, api_token: str, db_conn, tmp_path: Path, grant_alice_all_accounts,
) -> None:
    """A non-matching If-None-Match must fall through to a normal 200 —
    guards against an over-eager helper that returns 304 on any
    non-empty header."""
    sha = "23" * 32
    _seed_blob_with_carrier(db_conn, tmp_path, sha, _PDF_PAYLOAD)
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    other = '"' + ("9" * 64) + '"'
    r = c.get(
        f"/v1/attachments/{sha}",
        headers={"Authorization": f"Bearer {api_token}", "If-None-Match": other},
    )
    assert r.status_code == 200
    assert r.content == _PDF_PAYLOAD


def test_if_none_match_takes_precedence_over_range(
    db_dsn: str, api_token: str, db_conn, tmp_path: Path, grant_alice_all_accounts,
) -> None:
    """Per RFC 9110 §13.2.2 ordering, If-None-Match is evaluated before
    Range. If the precondition fires (304), the Range header is ignored
    — never serve a 206 body when the client is asking us to confirm a
    cache hit."""
    sha = "24" * 32
    _seed_blob_with_carrier(db_conn, tmp_path, sha, _PDF_PAYLOAD)
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(
        f"/v1/attachments/{sha}",
        headers={
            "Authorization": f"Bearer {api_token}",
            "If-None-Match": _strong_etag(sha),
            "Range": "bytes=0-9",
        },
    )
    assert r.status_code == 304
    assert r.content == b""


def test_if_none_match_non_matching_does_not_suppress_range(
    db_dsn: str, api_token: str, db_conn, tmp_path: Path, grant_alice_all_accounts,
) -> None:
    """Negative-precedence guard: a non-matching If-None-Match must NOT
    fire the 304 shortcut, so a co-sent Range still gets honoured with
    a 206. Closes the precedence matrix together with
    `test_if_none_match_takes_precedence_over_range`."""
    sha = "25" * 32
    _seed_blob_with_carrier(db_conn, tmp_path, sha, _PDF_PAYLOAD)
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    other = '"' + ("9" * 64) + '"'
    r = c.get(
        f"/v1/attachments/{sha}",
        headers={
            "Authorization": f"Bearer {api_token}",
            "If-None-Match": other,
            "Range": "bytes=0-9",
        },
    )
    assert r.status_code == 206
    assert r.content == _PDF_PAYLOAD[:10]
    assert r.headers["etag"] == _strong_etag(sha)


# ---------- If-Range with Range ----------------------------------------------


def test_if_range_matching_etag_serves_206(
    db_dsn: str, api_token: str, db_conn, tmp_path: Path, grant_alice_all_accounts,
) -> None:
    """A matching If-Range → 206, identical to the no-If-Range case.
    The resumer is on a still-fresh resource and the slice is safe."""
    sha = "30" * 32
    _seed_blob_with_carrier(db_conn, tmp_path, sha, _PDF_PAYLOAD)
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(
        f"/v1/attachments/{sha}",
        headers={
            "Authorization": f"Bearer {api_token}",
            "Range": f"bytes={_HALF}-",
            "If-Range": _strong_etag(sha),
        },
    )
    assert r.status_code == 206
    assert r.content == _PDF_PAYLOAD[_HALF:]


def test_if_range_non_matching_etag_falls_back_to_200_full(
    db_dsn: str, api_token: str, db_conn, tmp_path: Path, grant_alice_all_accounts,
) -> None:
    """The whole point of If-Range — if the resource has changed under
    the resumer, return the full new bytes with 200, NOT a partial
    response that would stitch onto the client's stale prefix."""
    sha = "31" * 32
    _seed_blob_with_carrier(db_conn, tmp_path, sha, _PDF_PAYLOAD)
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    stale = '"' + ("9" * 64) + '"'
    r = c.get(
        f"/v1/attachments/{sha}",
        headers={
            "Authorization": f"Bearer {api_token}",
            "Range": f"bytes={_HALF}-",
            "If-Range": stale,
        },
    )
    assert r.status_code == 200
    assert r.content == _PDF_PAYLOAD
    # 200 path must still advertise the canonical ETag so the client can
    # update its cache atomically.
    assert r.headers["etag"] == _strong_etag(sha)


def test_if_range_weak_etag_falls_back_to_200_full(
    db_dsn: str, api_token: str, db_conn, tmp_path: Path, grant_alice_all_accounts,
) -> None:
    """RFC 9110 §13.1.5 mandates **strong** comparison for If-Range.
    A weak form of the right opaque tag MUST be rejected — proxies that
    downgrade strong to weak for Vary-related reasons cannot be trusted
    to validate slice equivalence."""
    sha = "32" * 32
    _seed_blob_with_carrier(db_conn, tmp_path, sha, _PDF_PAYLOAD)
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(
        f"/v1/attachments/{sha}",
        headers={
            "Authorization": f"Bearer {api_token}",
            "Range": f"bytes={_HALF}-",
            "If-Range": f'W/"{sha}"',
        },
    )
    assert r.status_code == 200
    assert r.content == _PDF_PAYLOAD


def test_if_range_http_date_falls_back_to_200_full(
    db_dsn: str, api_token: str, db_conn, tmp_path: Path, grant_alice_all_accounts,
) -> None:
    """We don't track Last-Modified, so an HTTP-date If-Range can never
    strong-match. Conservative fallback to 200 keeps resumed downloads
    safe."""
    sha = "33" * 32
    _seed_blob_with_carrier(db_conn, tmp_path, sha, _PDF_PAYLOAD)
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(
        f"/v1/attachments/{sha}",
        headers={
            "Authorization": f"Bearer {api_token}",
            "Range": f"bytes={_HALF}-",
            "If-Range": "Tue, 19 May 2026 12:34:56 GMT",
        },
    )
    assert r.status_code == 200
    assert r.content == _PDF_PAYLOAD


def test_if_range_without_range_is_ignored(
    db_dsn: str, api_token: str, db_conn, tmp_path: Path, grant_alice_all_accounts,
) -> None:
    """RFC 9110 §13.1.5: a client MUST NOT send If-Range without a Range.
    If they do anyway, the server ignores it — the response shape is the
    same as a plain GET, including 200 + full body."""
    sha = "34" * 32
    _seed_blob_with_carrier(db_conn, tmp_path, sha, _PDF_PAYLOAD)
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(
        f"/v1/attachments/{sha}",
        headers={
            "Authorization": f"Bearer {api_token}",
            "If-Range": _strong_etag(sha),
        },
    )
    assert r.status_code == 200
    assert r.content == _PDF_PAYLOAD


def test_if_range_mismatch_preserves_full_content_disposition(
    db_dsn: str, api_token: str, db_conn, tmp_path: Path, grant_alice_all_accounts,
) -> None:
    """The 200-fallback path on If-Range mismatch must still go through
    the same #32 force-download invariants — Content-Disposition:
    attachment with both filename forms, MIME clamp where applicable.
    Guards against a fix that bypasses the security headers when it
    short-circuits the Range path."""
    sha = "35" * 32
    _seed_blob_with_carrier(
        db_conn, tmp_path, sha, b"<script>alert(1)</script>" * 4,
        filename="evil.html", mime="text/html",
    )
    grant_alice_all_accounts()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(
        f"/v1/attachments/{sha}",
        headers={
            "Authorization": f"Bearer {api_token}",
            "Range": "bytes=0-9",
            "If-Range": '"' + ("9" * 64) + '"',
        },
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/octet-stream")
    cd = r.headers["content-disposition"]
    assert cd.startswith("attachment;")
    assert 'filename="evil.html"' in cd


# ---------- 304 short-circuit skips file-open + filename lookup (#62) ---------


def test_304_does_not_call_open_attachment_bytes_or_filename(
    db_dsn: str, api_token: str, db_conn, tmp_path: Path,
    grant_alice_all_accounts, monkeypatch,
) -> None:
    """#62 acceptance: the conditional-GET 304 short-circuit must NOT
    invoke `open_attachment_bytes` (file pointer + Path.exists) or
    `get_attachment_filename` (JSONB scan across messages). The whole
    point of the refactor is to skip those costs when we know the
    client's cache is already current — the cheap probe in the api/
    layer is enough to evaluate the precondition. Spy on both via
    monkeypatch and assert zero invocations on the cache-hit path."""
    sha = "40" * 32
    _seed_blob_with_carrier(db_conn, tmp_path, sha, _PDF_PAYLOAD)
    grant_alice_all_accounts()

    import localmail.serve.routes.attachments as routes

    open_calls: list[tuple] = []
    filename_calls: list[tuple] = []

    real_open = routes.open_attachment_bytes
    real_filename = routes.get_attachment_filename

    def spy_open(*args, **kwargs):
        open_calls.append((args, kwargs))
        return real_open(*args, **kwargs)

    def spy_filename(*args, **kwargs):
        filename_calls.append((args, kwargs))
        return real_filename(*args, **kwargs)

    monkeypatch.setattr(routes, "open_attachment_bytes", spy_open)
    monkeypatch.setattr(routes, "get_attachment_filename", spy_filename)

    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(
        f"/v1/attachments/{sha}",
        headers={
            "Authorization": f"Bearer {api_token}",
            "If-None-Match": _strong_etag(sha),
        },
    )
    assert r.status_code == 304
    assert open_calls == [], (
        "open_attachment_bytes was called on the 304 short-circuit; "
        "#62 requires the route to skip the file open on a cache hit"
    )
    assert filename_calls == [], (
        "get_attachment_filename was called on the 304 short-circuit; "
        "#62 requires the route to skip the JSONB filename scan on a cache hit"
    )


def test_304_acl_denied_returns_404_not_304(
    db_dsn: str, api_token: str, db_conn, tmp_path: Path,
) -> None:
    """#62 acceptance: ACL must run **before** the conditional check.
    Without this guarantee, a 304 response leaks 'this sha exists on
    the server somewhere' to a user who has no grant to any account
    that carries the blob. Note: this test deliberately does NOT call
    `grant_alice_all_accounts` — alice can authenticate but cannot see
    any account, so every attachment lookup must surface as 404 even
    when the request would otherwise satisfy `If-None-Match`."""
    sha = "41" * 32
    _seed_blob_with_carrier(db_conn, tmp_path, sha, _PDF_PAYLOAD)
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(
        f"/v1/attachments/{sha}",
        headers={
            "Authorization": f"Bearer {api_token}",
            "If-None-Match": _strong_etag(sha),
        },
    )
    assert r.status_code == 404
