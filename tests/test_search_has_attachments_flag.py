# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""A hit's ``has_attachments`` describes the message, not the matched chunk (#364).

It was ``attachment_filename is not None``, i.e. "the text that matched came
from an attachment", so a message carrying two PDFs matched through its
subject reported ``false``. That is the evidence in #364's thread: the flag
was true only on hits whose ``matched_arms`` was ``attachment_chunks``.
"""
from __future__ import annotations

from datetime import datetime, timezone

import psycopg
from fastapi.testclient import TestClient

from localmail.api.search import _to_api_result
from localmail.config import SearchConfig
from localmail.db import open_pool
from localmail.search.embed_worker import run_embed_worker_once
from localmail.search.searcher import Searcher, SearchResult
from localmail.serve.app import create_app

_PDF = '[{"filename": "e-ticket.pdf", "sha256": "' + "cd" * 32 + '"}]'


class _Embedder:
    name = "stub"
    model = "stub"
    dimension = 768

    def embed_documents(self, texts):
        return [[1.0 / (i + 1)] * 768 for i, _ in enumerate(texts)]

    def embed_query(self, t):
        return [0.5] * 768

    def health_check(self):
        pass


def _seed(conn: psycopg.Connection, *, malformed: bool) -> tuple[int, dict[str, int]]:
    """Messages that match "Berlin" through their subject and body only.

    No ``attachment_text`` rows are seeded, so no hit can come from an
    attachment chunk. A flag that is true here can only have come from the
    message itself, which is the distinction #364 is about. The malformed
    row is optional because the embed worker's chunking has no business
    being asked about it; the date walk does.
    """
    shapes = [("ticket", _PDF), ("lunch", "[]")]
    if malformed:
        shapes.append(("malformed", "{}"))
    ids: dict[str, int] = {}
    with conn.cursor() as cur:
        cur.execute("INSERT INTO accounts (name, email_address, imap_host, auth_method)"
                    " VALUES ('a', 'a@x', 'h', 'password') RETURNING id")
        row = cur.fetchone()
        assert row is not None
        acct = int(row[0])
        for i, (key, attachments) in enumerate(shapes):
            cur.execute(
                "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
                " body_text, headers, raw_bytes, size_bytes, attachments, internal_date)"
                " VALUES (%s, %s, %s, %s, %s, '{}'::jsonb, 'r', 1, %s::jsonb, %s)"
                " RETURNING id",
                (acct, f"<{key}>", bytes([i + 1]) * 32, f"Berlin {key}",
                 f"Berlin {key} details", attachments,
                 datetime(2026, 3, i + 1, tzinfo=timezone.utc)),
            )
            row = cur.fetchone()
            assert row is not None
            ids[key] = int(row[0])
    conn.commit()
    return acct, ids


def _result(*, has_attachments: bool, attachment_filename: str | None) -> SearchResult:
    return SearchResult(
        message_id=1, account_id=1, rank=1, score=0.5, rrf_score=0.5,
        subject="s", from_addr="a@b", from_name="A",
        date_sent=None, internal_date=None,
        snippet="", snippet_source="body",
        attachment_filename=attachment_filename, has_attachments=has_attachments,
        matched_chunk_id=None, matched_chunk_table="message_chunks",
    )


def test_the_wire_field_is_the_result_field_not_the_snippet_source() -> None:
    assert _to_api_result(
        _result(has_attachments=True, attachment_filename=None))["has_attachments"] is True
    assert _to_api_result(
        _result(has_attachments=False, attachment_filename="x.pdf"))["has_attachments"] is False


def test_the_date_walk_flags_the_message(db_dsn, db_conn) -> None:
    _, ids = _seed(db_conn, malformed=True)
    pool = open_pool(db_dsn)
    try:
        searcher = Searcher(pool=pool, cfg=SearchConfig(), embeddings=None,
                            reranker=None, rewriter=None)
        page = searcher.search("", allowed_account_ids=None)
    finally:
        pool.close()
    assert {r.message_id: r.has_attachments for r in page.results} == {
        ids["ticket"]: True, ids["lunch"]: False, ids["malformed"]: False,
    }


def test_the_hybrid_path_flags_the_message_not_the_matched_chunk(db_dsn, db_conn) -> None:
    _, ids = _seed(db_conn, malformed=False)
    cfg = SearchConfig()
    run_embed_worker_once(db_conn, cfg, _Embedder())
    pool = open_pool(db_dsn)
    try:
        searcher = Searcher(pool=pool, cfg=cfg, embeddings=_Embedder(),
                            reranker=None, rewriter=None)
        page = searcher.search("Berlin", allowed_account_ids=None)
    finally:
        pool.close()
    assert page.sort_applied == "rank"
    by_id = {r.message_id: r for r in page.results}
    assert set(by_id) == {ids["ticket"], ids["lunch"]}
    # The old rule read the matched chunk, and no chunk here is an attachment's.
    assert by_id[ids["ticket"]].matched_chunk_table != "attachment_chunks"
    assert by_id[ids["ticket"]].has_attachments is True
    assert by_id[ids["lunch"]].has_attachments is False


def test_the_flag_reaches_the_wire_through_the_real_route(
    db_dsn, db_conn, api_user, api_token,
) -> None:
    acct, ids = _seed(db_conn, malformed=False)
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO user_accounts (user_id, account_id) VALUES (%s, %s)",
                    (api_user.id, acct))
    db_conn.commit()
    pool = open_pool(db_dsn)
    try:
        searcher = Searcher(pool=pool, cfg=SearchConfig(), embeddings=None,
                            reranker=None, rewriter=None)
        client = TestClient(create_app(db_dsn=db_dsn, searcher=searcher))
        r = client.post("/v1/search", json={"query": "", "filters": {}, "limit": 20},
                        headers={"Authorization": f"Bearer {api_token}"})
    finally:
        pool.close()
    assert r.status_code == 200, r.text
    got = {int(h["message_id"]): h["has_attachments"] for h in r.json()["results"]}
    assert got == {ids["ticket"]: True, ids["lunch"]: False}
