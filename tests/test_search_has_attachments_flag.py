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
import pytest
from fastapi.testclient import TestClient

from localmail.api.search import _to_api_result, run_search
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


def _seed(conn: psycopg.Connection) -> tuple[int, dict[str, int]]:
    """Messages that match "Berlin" through their subject and body only.

    No ``attachment_text`` rows are seeded, so no hit can come from an
    attachment chunk. A flag that is true here can only have come from the
    message itself, which is the distinction #364 is about.

    The malformed row (``attachments = '{}'``) is seeded for **every** test,
    the hybrid ones included. It used to be left out of those on the grounds
    that chunking has no business seeing it, which was measured false in
    #366's review, and leaving it out left ``_hydrate``'s guard unpinned:
    both the bare ``jsonb_array_length`` and a restated ``<> '[]'`` rule
    survived the whole suite there. Every rule agrees on ``[]`` and on a
    one-PDF array, so only this row can tell them apart.
    """
    shapes = [("ticket", _PDF), ("lunch", "[]"), ("malformed", "{}")]
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
    _, ids = _seed(db_conn)
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
    _, ids = _seed(db_conn)
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
    assert set(by_id) == {ids["ticket"], ids["lunch"], ids["malformed"]}
    # The old rule read the matched chunk, and no chunk here is an attachment's.
    assert by_id[ids["ticket"]].matched_chunk_table != "attachment_chunks"
    assert by_id[ids["ticket"]].has_attachments is True
    assert by_id[ids["lunch"]].has_attachments is False
    assert by_id[ids["malformed"]].has_attachments is False


def test_the_flag_reaches_the_wire_through_the_real_route(
    db_dsn, db_conn, api_user, api_token,
) -> None:
    acct, ids = _seed(db_conn)
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
    assert got == {ids["ticket"]: True, ids["lunch"]: False, ids["malformed"]: False}


@pytest.mark.parametrize("free_text", ["", "Berlin"])
def test_the_filter_selects_exactly_the_flagged_hits(db_dsn, db_conn, free_text) -> None:
    """One rule, checked by behaviour: the filter and the flag cannot disagree.

    ``""`` drives the date walk and ``"Berlin"`` the hybrid pool, so both
    places a hit is built are covered.
    """
    acct, _ = _seed(db_conn)
    cfg = SearchConfig()
    run_embed_worker_once(db_conn, cfg, _Embedder())
    pool = open_pool(db_dsn)
    try:
        searcher = Searcher(pool=pool, cfg=cfg, embeddings=_Embedder(),
                            reranker=None, rewriter=None)

        def flags(filters: dict) -> dict[str, bool]:
            page = run_search(searcher=searcher, free_text=free_text, filters=filters,
                              limit=50, allowed_account_ids=[acct], user_id=1)
            return {h["message_id"]: h["has_attachments"] for h in page["results"]}

        everything = flags({})
        with_attachments = flags({"has_attachment": True})
        without_attachments = flags({"has_attachment": False})
    finally:
        pool.close()
    assert with_attachments and without_attachments, (
        "both halves must be non-empty, or this proves nothing", everything)
    assert set(with_attachments) == {m for m, flag in everything.items() if flag}
    assert set(without_attachments) == {m for m, flag in everything.items() if not flag}


def test_a_message_deleted_before_hydration_reports_no_attachments() -> None:
    """``_hydrate`` reads each hit's row with ``msgs.get(id, {})``, so a
    message deleted between retrieval and hydration arrives as ``{}``.
    Subscripting the flag there raised ``KeyError`` — a 500 — where every
    other field has a default; the guard is what stops it. Dropping such a
    ghost hit instead is #372, which will replace this pin.
    """
    from unittest.mock import MagicMock

    from localmail.search.query import parse_query
    from localmail.search.searcher import FusedHit

    ghost = {
        "fused": FusedHit(message_id=7, best_chunk_id=None, best_chunk_table="message",
                          rrf_score=0.5, contributing_arms=[0]),
        "msg": {},
        "snippet_source_text": "",
    }
    searcher = Searcher(pool=MagicMock(), cfg=SearchConfig(), embeddings=None,
                        reranker=None, rewriter=None)
    [result] = searcher._build_results([ghost], parse_query("x"), [0.5],
                                       page=1, page_size=10, snippet_width=200)
    assert result.has_attachments is False
