# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""A blank query paginates, in both directions.

The blank-query branch used to return ``search_token=None``,
``has_more_in_pool=False`` and ``next_keyset=None``, so its next_cursor was
always null: one page, then nothing. That is exactly the branch "show me my
oldest mail" lands on, which made ascending order close to useless.

``_list_recent_messages`` was ``_lexical_date_search`` minus the FTS
predicate — same SELECT list, same ORDER BY, same filter composition — so
the two are one helper now and the blank branch inherits the keyset walk.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from localmail.config import SearchConfig
from localmail.db import open_pool
from localmail.search.searcher import Searcher


class _E:
    name = "s"; model = "s"; dimension = 768
    def embed_documents(self, t): return [[1.0] * 768 for _ in t]
    def embed_query(self, t): return [0.5] * 768
    def health_check(self): pass


def _seed(conn, n=7):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO accounts (name,email_address,imap_host,auth_method)"
                    " VALUES ('a','a@x','h','password') RETURNING id")
        acct = cur.fetchone()[0]
        for i in range(n):
            cur.execute(
                "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
                " body_text, headers, raw_bytes, size_bytes, internal_date)"
                " VALUES (%s,%s,%s,%s,%s,'{}'::jsonb,'r',1,%s)",
                (acct, f"<m{i}>", bytes([i + 1]) * 32, f"Subject {i}", "body",
                 datetime(2026, 3, i + 1, tzinfo=timezone.utc)),
            )
    conn.commit()


def _seed_two_accounts(conn, n=5):
    """Two accounts whose messages interleave in time.

    Interleaved on purpose: a walk that mixed the filter's parameters with
    the keyset's would still return `n` rows in date order, just the wrong
    ones. Returns (account_id, message_ids-oldest-first) for the first.
    """
    ids: list[int] = []
    with conn.cursor() as cur:
        accts = []
        for name in ("a", "b"):
            cur.execute("INSERT INTO accounts (name,email_address,imap_host,"
                        "auth_method) VALUES (%s,%s,'h','password') RETURNING id",
                        (name, f"{name}@x"))
            accts.append(cur.fetchone()[0])
        for i in range(n):
            for slot, acct in enumerate(accts):
                cur.execute(
                    "INSERT INTO messages (account_id, message_id, raw_sha256,"
                    " subject, body_text, headers, raw_bytes, size_bytes,"
                    " internal_date)"
                    " VALUES (%s,%s,%s,%s,'body','{}'::jsonb,'r',1,%s) RETURNING id",
                    (acct, f"<m{i}-{slot}>", bytes([i * 2 + slot + 1]) * 32,
                     f"Subject {i}-{slot}",
                     datetime(2026, 3, i * 2 + slot + 1, tzinfo=timezone.utc)),
                )
                if acct == accts[0]:
                    ids.append(cur.fetchone()[0])
                else:
                    cur.fetchone()
    conn.commit()
    return accts[0], ids


def _walk(searcher, *, order, page_size=3, query=""):
    ids: list[int] = []
    cursor = None
    for _ in range(50):
        page = searcher.search(query, allowed_account_ids=None, page_size=page_size,
                               user_id=1, sort="date", sort_order=order,
                               keyset_cursor=cursor)
        ids.extend(r.message_id for r in page.results)
        if page.next_keyset is None:
            return ids
        cursor = page.next_keyset
    raise AssertionError("walk did not terminate")


def test_a_blank_query_emits_a_cursor_when_more_remain(db_dsn, db_conn):
    _seed(db_conn, n=7)
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=SearchConfig(), embeddings=_E(), reranker=None)
        page = s.search("", allowed_account_ids=None, page_size=3, user_id=1,
                        sort="date")
    finally:
        pool.close()
    assert len(page.results) == 3
    assert page.next_keyset is not None, (
        "the blank-query branch minted no cursor: 'show me my oldest mail' "
        "returns one page and stops"
    )


def test_a_blank_query_walk_covers_every_row_once_descending(db_dsn, db_conn):
    _seed(db_conn, n=7)
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=SearchConfig(), embeddings=_E(), reranker=None)
        ids = _walk(s, order="desc")
    finally:
        pool.close()
    assert len(ids) == len(set(ids)) == 7


def test_a_blank_query_walk_ascending_is_the_reverse(db_dsn, db_conn):
    _seed(db_conn, n=7)
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=SearchConfig(), embeddings=_E(), reranker=None)
        desc = _walk(s, order="desc")
        asc = _walk(s, order="asc")
    finally:
        pool.close()
    assert asc == list(reversed(desc))


def test_the_last_page_reports_no_cursor(db_dsn, db_conn):
    """A walk that never ends is worse than one that never starts."""
    _seed(db_conn, n=4)
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=SearchConfig(), embeddings=_E(), reranker=None)
        page = s.search("", allowed_account_ids=None, page_size=10, user_id=1,
                        sort="date")
    finally:
        pool.close()
    assert len(page.results) == 4
    assert page.next_keyset is None


@pytest.mark.parametrize("order", ["asc", "desc"])
def test_a_filtered_blank_query_walk_covers_exactly_the_filtered_rows(
    db_dsn, db_conn, order,
):
    """A filtered blank query, walked — the composition nothing else covers.

    The blank branch now composes three parameter groups into one
    statement: the filter clause, the keyset predicate it gained with
    pagination, and the LIMIT. Each must be bound in the order the SQL
    names them, and no sibling here exercises two of them at once — the
    unfiltered walks bind the keyset half only, the unpaginated cases
    neither — so a mis-ordered ``params.extend`` shows up on this shape
    and no other. Correct by inspection today; the point is that it stays
    that way.

    The two accounts interleave in time, so the walk cannot pass by
    returning page-sized batches of whatever happens to be next.
    """
    acct, ids = _seed_two_accounts(db_conn, n=5)
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=SearchConfig(), embeddings=_E(), reranker=None)
        walked = _walk(s, order=order, page_size=2, query=f"account_id:{acct}")
    finally:
        pool.close()
    expected = ids if order == "asc" else list(reversed(ids))
    assert walked == expected
