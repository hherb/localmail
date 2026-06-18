# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""End-to-end ACL tests through the HTTP layer.

Alice has grants to account A; Bob has grants to account B. Every
account-scoped route must show A-only data to Alice and B-only data to Bob.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from unittest.mock import MagicMock

import psycopg
from fastapi.testclient import TestClient

from localmail.api.acl import grant_account
from localmail.api.auth import create_user, login, reset_login_rate_limiter
from localmail.serve.app import create_app


def _seed_acct_msg_blob(
    conn: psycopg.Connection, name: str, blob_payload: bytes, tmp_path,
) -> tuple[int, int, str]:
    """Return (account_id, message_id, blob_sha_hex). Also writes the blob."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method) "
            "VALUES (%s, %s, 'imap.x', 'password') RETURNING id",
            (name, f"{name}@x"),
        )
        row = cur.fetchone()
        assert row is not None
        aid = int(row[0])
        cur.execute(
            "INSERT INTO mailboxes (account_id, name) VALUES (%s, 'INBOX') RETURNING id",
            (aid,),
        )
        row = cur.fetchone()
        assert row is not None
        mb = int(row[0])
    sha_hex = hashlib.sha256(blob_payload).hexdigest()
    blob_path = tmp_path / f"{name}_{sha_hex}"
    blob_path.write_bytes(blob_payload)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO attachment_blobs (sha256, mime_type, size_bytes, path) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (bytes.fromhex(sha_hex), "application/pdf", len(blob_payload), str(blob_path)),
        )
        raw = b"x"
        now = datetime.now(timezone.utc)
        cur.execute(
            "INSERT INTO messages (account_id, message_id, raw_bytes, raw_sha256, "
            "size_bytes, subject, body_text, headers, attachments, date_sent, date_received) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s) RETURNING id",
            (
                aid, f"<{name}-{sha_hex[:8]}@x>", raw, hashlib.sha256(raw).digest(), 1,
                f"{name} message", "body", "{}",
                psycopg.types.json.Jsonb([{"filename": "x.pdf", "sha256": sha_hex}]),
                now, now,
            ),
        )
        row = cur.fetchone()
        assert row is not None
        mid = int(row[0])
        cur.execute(
            "INSERT INTO message_labels (message_id, mailbox_id, uid) VALUES (%s, %s, %s)",
            (mid, mb, mid),
        )
    conn.commit()
    return aid, mid, sha_hex


def _seed_alice_and_bob(db_conn, tmp_path):
    """Build the alice/bob fixture: each gets their own account + message + blob.

    Returns a dict with keys: alice (token), bob (token), a_account_id,
    a_message_id, a_sha_hex, b_account_id, b_message_id, b_sha_hex.
    """
    reset_login_rate_limiter(db_conn)
    db_conn.commit()
    a_aid, a_mid, a_sha = _seed_acct_msg_blob(db_conn, "acct-a", b"%PDF-A-payload", tmp_path)
    b_aid, b_mid, b_sha = _seed_acct_msg_blob(db_conn, "acct-b", b"%PDF-B-payload", tmp_path)
    alice_uid = create_user(db_conn, "alice", "hunter2")
    bob_uid = create_user(db_conn, "bob", "hunter3")
    grant_account(db_conn, alice_uid, a_aid)
    grant_account(db_conn, bob_uid, b_aid)
    db_conn.commit()
    alice_tok, _ = login(db_conn, "alice", "hunter2")
    bob_tok, _ = login(db_conn, "bob", "hunter3")
    db_conn.commit()
    return {
        "alice": alice_tok, "bob": bob_tok,
        "a_aid": a_aid, "a_mid": a_mid, "a_sha": a_sha,
        "b_aid": b_aid, "b_mid": b_mid, "b_sha": b_sha,
    }


def _h(tok: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}"}


def test_alice_only_sees_account_a(db_dsn, db_conn, tmp_path):
    ctx = _seed_alice_and_bob(db_conn, tmp_path)
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get("/v1/accounts", headers=_h(ctx["alice"]))
    assert r.status_code == 200
    names = [a["name"] for a in r.json()]
    assert names == ["acct-a"]
    # bob only sees b
    r = c.get("/v1/accounts", headers=_h(ctx["bob"]))
    assert [a["name"] for a in r.json()] == ["acct-b"]


def test_alice_cannot_fetch_b_message(db_dsn, db_conn, tmp_path):
    ctx = _seed_alice_and_bob(db_conn, tmp_path)
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(f"/v1/messages/{ctx['b_mid']}", headers=_h(ctx["alice"]))
    assert r.status_code == 404
    r = c.get(f"/v1/messages/{ctx['a_mid']}", headers=_h(ctx["alice"]))
    assert r.status_code == 200


def test_alice_cannot_fetch_b_raw_message(db_dsn, db_conn, tmp_path):
    ctx = _seed_alice_and_bob(db_conn, tmp_path)
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(f"/v1/messages/{ctx['b_mid']}/raw", headers=_h(ctx["alice"]))
    assert r.status_code == 404


def test_alice_cannot_stream_b_attachment(db_dsn, db_conn, tmp_path):
    ctx = _seed_alice_and_bob(db_conn, tmp_path)
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(f"/v1/attachments/{ctx['b_sha']}", headers=_h(ctx["alice"]))
    assert r.status_code == 404
    r = c.get(f"/v1/attachments/{ctx['a_sha']}", headers=_h(ctx["alice"]))
    assert r.status_code == 200


def test_alice_cannot_read_b_attachment_text(db_dsn, db_conn, tmp_path):
    ctx = _seed_alice_and_bob(db_conn, tmp_path)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO attachment_text (sha256, extractor, extracted_text) "
            "VALUES (%s, 'test', %s)",
            (bytes.fromhex(ctx["b_sha"]), "secret-b-content"),
        )
        cur.execute(
            "INSERT INTO attachment_text (sha256, extractor, extracted_text) "
            "VALUES (%s, 'test', %s)",
            (bytes.fromhex(ctx["a_sha"]), "secret-a-content"),
        )
    db_conn.commit()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get(f"/v1/attachments/{ctx['b_sha']}/text", headers=_h(ctx["alice"]))
    assert r.status_code == 404
    r = c.get(f"/v1/attachments/{ctx['a_sha']}/text", headers=_h(ctx["alice"]))
    assert r.status_code == 200
    assert r.json() == {"text": "secret-a-content"}


def test_changes_filters_by_acl(db_dsn, db_conn, tmp_path):
    """Bob's poll must not surface alice's account messages."""
    ctx = _seed_alice_and_bob(db_conn, tmp_path)
    from localmail.config import ServeConfig
    cfg = ServeConfig(changes_safe_horizon_s=0)
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None, serve_config=cfg))
    r = c.get("/v1/changes", headers=_h(ctx["bob"]))
    assert r.status_code == 200
    seen = {int(m["account"]["id"]) for m in r.json()["new_messages"]}
    assert seen == {ctx["b_aid"]}


def test_user_with_no_grants_sees_empty_accounts_and_404s(db_dsn, db_conn, tmp_path):
    _seed_alice_and_bob(db_conn, tmp_path)
    # Create a third user "ghost" with zero grants.
    ghost_uid = create_user(db_conn, "ghost", "hunter4")
    db_conn.commit()
    tok, _ = login(db_conn, "ghost", "hunter4")
    db_conn.commit()
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    assert c.get("/v1/accounts", headers=_h(tok)).json() == []
    # any message/attachment lookup must 404
    r = c.get("/v1/messages/1", headers=_h(tok))
    assert r.status_code == 404


def test_capabilities_is_shared_reflects_grant_count(db_dsn, db_conn, tmp_path):
    """Single-account user → is_shared=False; multi-account user → True."""
    ctx = _seed_alice_and_bob(db_conn, tmp_path)
    # alice only has access to A
    c = TestClient(create_app(db_dsn=db_dsn, searcher=None))
    r = c.get("/v1/accounts", headers=_h(ctx["alice"]))
    assert r.status_code == 200
    rows = r.json()
    assert all(row["capabilities"]["is_shared"] is False for row in rows)
    # grant alice access to B too
    grant_account(db_conn, _user_id(db_conn, "alice"), ctx["b_aid"])
    db_conn.commit()
    r = c.get("/v1/accounts", headers=_h(ctx["alice"]))
    rows = r.json()
    assert len(rows) == 2
    assert all(row["capabilities"]["is_shared"] is True for row in rows)


def _user_id(conn, username):
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM api_users WHERE username = %s", (username,))
        row = cur.fetchone()
        assert row is not None
        return int(row[0])


def _account_scoped_fake_searcher(account_to_hits: dict[int, list[dict[str, object]]]):
    """Build a fake Searcher that honours the ACL filter in the query string.

    The route layer is expected to inject ``account_id:N`` tokens derived from
    the caller's grants (see :func:`localmail.api.search._scope_filters_by_acl`).
    The fake parses those tokens out of the query string and only returns hits
    whose account_id matches — so a wiring bug that drops the ACL would cause
    cross-user leakage detectable by the test.
    """
    s = MagicMock()

    def _search(query: str, **kwargs):
        ids = {int(m) for m in re.findall(r"account_id:(\d+)", query)}
        results = []
        for aid in sorted(ids):
            for spec in account_to_hits.get(aid, []):
                r = MagicMock()
                r.message_id = spec["message_id"]
                r.account_id = aid
                r.rank = 1
                r.score = 0.9
                r.rrf_score = 0.5
                r.subject = spec["subject"]
                r.from_addr = "a@x"
                r.from_name = "A"
                r.date_sent = None
                r.snippet = "hit"
                r.snippet_source = "body"
                r.attachment_filename = None
                r.matched_chunk_id = None
                r.matched_chunk_table = "message_chunks"
                results.append(r)
        page = MagicMock()
        page.results = results
        page.search_token = "tok-fake"
        page.timing_ms = {"total": 1.0}
        return page

    s.search.side_effect = _search
    return s


def test_search_isolates_alice_from_bob_messages(db_dsn, db_conn, tmp_path):
    """Alice and Bob have disjoint grants — neither can see the other's hits via /v1/search."""
    ctx = _seed_alice_and_bob(db_conn, tmp_path)
    hits_per_account = {
        ctx["a_aid"]: [{"message_id": ctx["a_mid"], "subject": "alice secret"}],
        ctx["b_aid"]: [{"message_id": ctx["b_mid"], "subject": "bob secret"}],
    }
    searcher = _account_scoped_fake_searcher(hits_per_account)
    c = TestClient(create_app(db_dsn=db_dsn, searcher=searcher))

    r = c.post("/v1/search",
               json={"query": "secret", "filters": {}, "limit": 20},
               headers=_h(ctx["alice"]))
    assert r.status_code == 200
    seen = {int(hit["account"]["id"]) for hit in r.json()["results"]}
    assert seen == {ctx["a_aid"]}

    r = c.post("/v1/search",
               json={"query": "secret", "filters": {}, "limit": 20},
               headers=_h(ctx["bob"]))
    assert r.status_code == 200
    seen = {int(hit["account"]["id"]) for hit in r.json()["results"]}
    assert seen == {ctx["b_aid"]}


def test_search_intersects_account_ids_filter_with_acl(db_dsn, db_conn, tmp_path):
    """Alice asking for bob's account explicitly gets an empty result, not a 403."""
    ctx = _seed_alice_and_bob(db_conn, tmp_path)
    hits_per_account = {
        ctx["a_aid"]: [{"message_id": ctx["a_mid"], "subject": "alice doc"}],
        ctx["b_aid"]: [{"message_id": ctx["b_mid"], "subject": "bob doc"}],
    }
    searcher = _account_scoped_fake_searcher(hits_per_account)
    c = TestClient(create_app(db_dsn=db_dsn, searcher=searcher))

    r = c.post(
        "/v1/search",
        json={"query": "doc", "filters": {"account_ids": [str(ctx["b_aid"])]}, "limit": 20},
        headers=_h(ctx["alice"]),
    )
    assert r.status_code == 200
    assert r.json()["results"] == []
