# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""`fields` and `snippet_chars` on POST /v1/search, through the real transport.

A real `Searcher` over a seeded archive, never a MagicMock hit: an unset
MagicMock attribute serialises to `{}` rather than failing, so a mocked hit
proves nothing about which keys reach the wire.
"""
from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient

from localmail.api.search import run_search
from localmail.api.errors import ValidationFailed
from localmail.config import SearchConfig
from localmail.db import open_pool
from localmail.search.embed_worker import run_embed_worker_once
from localmail.search.searcher import Searcher
from localmail.serve.app import create_app

_FILLER = " ".join(f"word{i}" for i in range(400))
_DEFAULT_HIT_KEYS = {
    "message_id", "account", "folder", "subject", "from", "to", "date",
    "snippet_html", "has_attachments", "score", "matched_arms",
}
_ENVELOPE = {"results", "next_cursor", "total_estimate", "took_ms",
             "sort_applied", "rankable", "rewrite_skipped", "rewrite_status",
             "rewrite_note", "rewrite_note_code"}


class _E:
    """Chunks containing "zebra" point at the query; every other chunk points
    away. A constant embedder ties every chunk in the vector arm, and the
    tie-break (#360) could then pick a header or a zebra-less body chunk as
    the snippet source."""
    name = "s"; model = "s"; dimension = 768
    def embed_documents(self, t):
        return [[1.0 if "zebra" in x else -1.0] * 768 for x in t]
    def embed_query(self, t): return [0.5] * 768
    def health_check(self): pass


def _seed(conn: psycopg.Connection, n: int = 3) -> int:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO accounts (name,email_address,imap_host,auth_method)"
                    " VALUES ('a','a@x','h','password') RETURNING id")
        row = cur.fetchone()
        assert row is not None
        acct = int(row[0])
        for i in range(n):
            cur.execute(
                "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
                " body_text, headers, raw_bytes, size_bytes)"
                " VALUES (%s, %s, %s, %s, %s, '{}'::jsonb, 'r', 1)",
                (acct, f"<m{i}>", bytes([i + 1]) * 32, f"Note {i}",
                 f"{_FILLER} zebra {_FILLER}"),
            )
    conn.commit()
    return acct


@pytest.fixture
def archive(db_dsn, db_conn):
    acct = _seed(db_conn)
    cfg = SearchConfig(snippet_max_chars=1000)
    run_embed_worker_once(db_conn, cfg, _E())
    pool = open_pool(db_dsn)
    try:
        yield acct, Searcher(pool=pool, cfg=cfg, embeddings=_E(), reranker=None,
                             rewriter=None)
    finally:
        pool.close()


@pytest.fixture
def narrow_archive(db_dsn, db_conn):
    """The same archive under an operator-lowered `snippet_max_chars`.

    `archive` uses 1000, which is the *default*, so every test built on it
    passes against a hardcoded `max_chars=1000` at the api gate — leaving the
    knob's whole purpose, an operator's bound, unproven.
    """
    acct = _seed(db_conn)
    cfg = SearchConfig(snippet_max_chars=300)
    run_embed_worker_once(db_conn, cfg, _E())
    pool = open_pool(db_dsn)
    try:
        yield acct, Searcher(pool=pool, cfg=cfg, embeddings=_E(), reranker=None,
                             rewriter=None)
    finally:
        pool.close()


def _post(db_dsn, searcher, token, body):
    client = TestClient(create_app(db_dsn=db_dsn, searcher=searcher))
    return client.post("/v1/search", json=body,
                       headers={"Authorization": f"Bearer {token}"})


def _grant(db_conn, user_id: int, acct: int) -> None:
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO user_accounts (user_id, account_id) VALUES (%s, %s)",
                    (user_id, acct))
    db_conn.commit()


def test_the_default_response_is_unchanged(archive, db_dsn, db_conn, api_user, api_token):
    acct, searcher = archive
    _grant(db_conn, api_user.id, acct)
    r = _post(db_dsn, searcher, api_token, {"query": "zebra"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == _ENVELOPE
    assert body["results"], "the seeded archive must match, or this proves nothing"
    for hit in body["results"]:
        assert set(hit) == _DEFAULT_HIT_KEYS
        assert 150 < len(hit["snippet_html"]) <= 202


def test_fields_returns_exactly_the_named_keys(archive, db_dsn, db_conn, api_user, api_token):
    acct, searcher = archive
    _grant(db_conn, api_user.id, acct)
    r = _post(db_dsn, searcher, api_token,
              {"query": "zebra", "fields": ["snippet", "message_id"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == _ENVELOPE  # the envelope is never projected
    assert body["results"]
    for hit in body["results"]:
        assert list(hit) == ["message_id", "snippet"]
        assert "zebra" in hit["snippet"]


def test_snippet_chars_widens_on_the_wire(archive, db_dsn, db_conn, api_user, api_token):
    acct, searcher = archive
    _grant(db_conn, api_user.id, acct)
    r = _post(db_dsn, searcher, api_token,
              {"query": "zebra", "fields": ["snippet"], "snippet_chars": 800})
    assert r.status_code == 200, r.text
    hits = r.json()["results"]
    assert hits, "all() over [] is True; without this the only widening pin is vacuous"
    assert all(len(h["snippet"]) > 600 for h in hits)


def test_a_pool_continuation_is_projected_and_sized(archive, db_dsn, db_conn, api_user, api_token):
    acct, searcher = archive
    _grant(db_conn, api_user.id, acct)
    first = _post(db_dsn, searcher, api_token,
                  {"query": "zebra", "limit": 1, "fields": ["message_id"]}).json()
    assert first["next_cursor"], "need a second page"
    r = _post(db_dsn, searcher, api_token,
              {"query": "zebra", "limit": 1, "cursor": first["next_cursor"],
               "fields": ["snippet"], "snippet_chars": 30})
    assert r.status_code == 200, r.text
    [hit] = r.json()["results"]
    assert list(hit) == ["snippet"] and len(hit["snippet"]) <= 32


def test_a_keyset_continuation_is_projected(archive, db_dsn, db_conn, api_user, api_token):
    acct, searcher = archive
    _grant(db_conn, api_user.id, acct)
    first = _post(db_dsn, searcher, api_token,
                  {"query": "", "limit": 1, "fields": ["message_id"]}).json()
    assert first["next_cursor"], "need a second page"
    r = _post(db_dsn, searcher, api_token,
              {"query": "", "limit": 1, "cursor": first["next_cursor"],
               "fields": ["message_id", "date"]})
    assert r.status_code == 200, r.text
    [hit] = r.json()["results"]
    assert list(hit) == ["message_id", "date"]


_REFUSALS = [
    ({"fields": ["bogus"]}, "'bogus'"),
    ({"fields": []}, "empty"),
    ({"snippet_chars": 0}, "snippet_chars"),
    ({"snippet_chars": 1001}, "1000"),
    ({"snippet_chars": True}, "snippet_chars"),
    # `Any` on the wire (#370, F3): every non-integer must reach
    # `snippet_chars_error` as a problem+json 400, never pydantic's own 422
    # with an array `detail`, and never a silent coercion to `5`.
    ({"snippet_chars": "5"}, "snippet_chars"),
    ({"snippet_chars": 5.0}, "snippet_chars"),
    ({"snippet_chars": 1.5}, "snippet_chars"),
    ({"snippet_chars": "abc"}, "snippet_chars"),
]


@pytest.mark.parametrize(("extra", "needle"), _REFUSALS)
def test_a_bad_argument_is_a_problem_json_400(
    archive, db_dsn, db_conn, api_user, api_token, extra, needle,
):
    acct, searcher = archive
    _grant(db_conn, api_user.id, acct)
    r = _post(db_dsn, searcher, api_token, {"query": "zebra", **extra})
    assert r.status_code == 400, r.text
    assert r.headers["content-type"].startswith("application/problem+json")
    assert needle in r.json()["detail"]


@pytest.mark.parametrize(("extra", "needle"), _REFUSALS)
def test_a_bad_argument_is_refused_even_for_a_caller_granted_nothing(
    archive, db_dsn, api_token, extra, needle,
):
    # No grant: the empty-ACL short-circuit would answer 200 with an empty
    # page, byte-identical to "no results", if the gate sat after it.
    _, searcher = archive
    r = _post(db_dsn, searcher, api_token, {"query": "zebra", **extra})
    assert r.status_code == 400, r.text
    assert needle in r.json()["detail"]


def test_the_cap_is_the_configured_one_not_a_constant(
    narrow_archive, db_dsn, db_conn, api_user, api_token,
):
    # 500 is under the default 1000 and over this deployment's 300, so a
    # hardcoded `max_chars=1000` at the gate answers 200 here.
    acct, searcher = narrow_archive
    _grant(db_conn, api_user.id, acct)
    r = _post(db_dsn, searcher, api_token, {"query": "zebra", "snippet_chars": 500})
    assert r.status_code == 400, r.text
    assert "300" in r.json()["detail"]


def test_a_width_at_the_configured_cap_is_accepted(
    narrow_archive, db_dsn, db_conn, api_user, api_token,
):
    # The other direction: a cap read as a constant *lower* than configured
    # would refuse this, and the test above alone would not notice.
    acct, searcher = narrow_archive
    _grant(db_conn, api_user.id, acct)
    r = _post(db_dsn, searcher, api_token, {"query": "zebra", "snippet_chars": 300})
    assert r.status_code == 200, r.text
    assert r.json()["results"]


def test_run_search_refuses_before_touching_the_searcher() -> None:
    from unittest.mock import MagicMock

    searcher = MagicMock()
    searcher.config = SearchConfig()
    with pytest.raises(ValidationFailed):
        run_search(searcher=searcher, free_text="x", filters={}, limit=5,
                   allowed_account_ids=[1], user_id=1, fields=["bogus"])
    searcher.search.assert_not_called()
