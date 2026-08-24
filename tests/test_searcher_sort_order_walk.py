# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Ascending date order is the exact reverse of descending, and pages.

Ascending is spelled ``ASC NULLS FIRST, id ASC`` because that is the exact
reverse of ``messages_recent_idx`` and is therefore served by a backward
index scan. The ``NULLS LAST`` spelling full-sorts the table; an
``IS NOT NULL`` restriction does not rescue it. Both were measured on the
live 128k archive before this was written.

Undated rows therefore sort *first* ascending, which is what makes
``asc == reversed(desc)`` hold as an invariant.
"""
from __future__ import annotations

from datetime import datetime, timezone

from localmail.config import SearchConfig
from localmail.db import open_pool
from localmail.search.searcher import Searcher


class _E:
    name = "s"; model = "s"; dimension = 768
    def embed_documents(self, t): return [[1.0] * 768 for _ in t]
    def embed_query(self, t): return [0.5] * 768
    def health_check(self): pass


def _seed(conn, *, n=7, undated=2):
    """n dated messages plus `undated` with no usable date at all."""
    with conn.cursor() as cur:
        cur.execute("INSERT INTO accounts (name,email_address,imap_host,auth_method)"
                    " VALUES ('a','a@x','h','password') RETURNING id")
        acct = cur.fetchone()[0]
        for i in range(n):
            cur.execute(
                "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
                " body_text, headers, raw_bytes, size_bytes, internal_date)"
                " VALUES (%s,%s,%s,%s,%s,'{}'::jsonb,'r',1,%s)",
                (acct, f"<d{i}>", bytes([i + 1]) * 32, f"dated {i} needle",
                 "body needle", datetime(2026, 1, i + 1, tzinfo=timezone.utc)),
            )
        for j in range(undated):
            cur.execute(
                "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
                " body_text, headers, raw_bytes, size_bytes)"
                " VALUES (%s,%s,%s,%s,%s,'{}'::jsonb,'r',1)",
                (acct, f"<u{j}>", bytes([200 + j]) * 32, f"undated {j} needle",
                 "body needle"),
            )
    conn.commit()


def _all_pages(searcher, *, order, query="needle", page_size=3):
    """Walk every page, returning the flat list of message ids."""
    ids: list[int] = []
    cursor = None
    for _ in range(50):  # generous bound; the walk must terminate
        page = searcher.search(query, allowed_account_ids=None,
                               page_size=page_size, user_id=1, sort="date",
                               sort_order=order, keyset_cursor=cursor)
        ids.extend(r.message_id for r in page.results)
        if page.next_keyset is None:
            return ids
        cursor = page.next_keyset
    raise AssertionError("walk did not terminate")


def test_ascending_is_exactly_reversed_descending(db_dsn, db_conn):
    """The whole ordering, undated rows included — not just the dated head."""
    _seed(db_conn)
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=SearchConfig(), embeddings=_E(), reranker=None)
        desc = _all_pages(s, order="desc")
        asc = _all_pages(s, order="asc")
    finally:
        pool.close()
    assert len(desc) == 9
    assert asc == list(reversed(desc))


def test_undated_rows_sort_first_ascending(db_dsn, db_conn):
    """NULLS FIRST is not incidental — it is what the backward scan requires."""
    _seed(db_conn, n=7, undated=2)
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=SearchConfig(), embeddings=_E(), reranker=None)
        page = s.search("needle", allowed_account_ids=None, page_size=2,
                        user_id=1, sort="date", sort_order="asc")
    finally:
        pool.close()
    subjects = [r.subject for r in page.results]
    assert all(s_.startswith("undated") for s_ in subjects), subjects


def test_ascending_pages_do_not_overlap_or_skip(db_dsn, db_conn):
    _seed(db_conn)
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=SearchConfig(), embeddings=_E(), reranker=None)
        ids = _all_pages(s, order="asc", page_size=2)
    finally:
        pool.close()
    assert len(ids) == len(set(ids)) == 9


def test_descending_is_unchanged_when_order_is_unstated(db_dsn, db_conn):
    """The default path must be byte-identical to today's behaviour."""
    _seed(db_conn)
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=SearchConfig(), embeddings=_E(), reranker=None)
        stated = _all_pages(s, order="desc")
        unstated_page = s.search("needle", allowed_account_ids=None, page_size=3,
                                 user_id=1, sort="date")
    finally:
        pool.close()
    assert [r.message_id for r in unstated_page.results] == stated[:3]
