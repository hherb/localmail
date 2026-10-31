# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The search query accepts web-search syntax: OR, "phrase", -word (#419).

The lexical arms used ``plainto_tsquery``, so every word was a required AND
term: ``invoice OR bill`` needed the word "or", and a quoted phrase was two
unrelated words because the parser dropped the quotes before the arms saw
them. A planner LLM writes boolean queries all the time.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from localmail.config import SearchConfig
from localmail.db import open_pool
from localmail.search.query import parse_query
from localmail.search.searcher import Searcher


# --- parsing ---------------------------------------------------------------

@pytest.mark.parametrize(("query", "free_text"), [
    ("invoice OR bill", "invoice OR bill"),
    ('"payment overdue" now', '"payment overdue" now'),
    ('invoice -"big co"', 'invoice -"big co"'),
    ('"-anthropic"', '"-anthropic"'),
    # A spaced dash is a separator, not an exclusion of the next word.
    ("Order confirmation - Qantas", "Order confirmation Qantas"),
    ("foo -- bar", "foo bar"),
    # Single quotes are apostrophes as often as quotes: no phrase.
    ("it's late payment", "its late payment"),
    ('""', ""),
])
def test_free_text_keeps_the_syntax_websearch_reads(query, free_text):
    assert parse_query(query).free_text == free_text


def test_a_filter_value_is_still_read_without_its_quotes():
    q = parse_query('from:"Anna Schmidt" "Berlin trip"')
    assert q.filters.from_substr == "Anna Schmidt"
    assert q.free_text == '"Berlin trip"'


def test_free_text_reparses_to_itself():
    for query in ('"payment overdue" OR bill -acme', 'a -"b c" "d"'):
        free = parse_query(query).free_text
        assert parse_query(free).free_text == free


@pytest.mark.parametrize(("query", "semantic"), [
    ("invoice OR bill", "invoice bill"),
    ("due or overdue", "due overdue"),
    ('"payment overdue"', "payment overdue"),
    ("invoice -anthropic", "invoice"),
    ('invoice -"big co"', "invoice"),
    ('"black or white"', "black or white"),
    ("-acme", ""),
])
def test_semantic_text_drops_the_operators(query, semantic):
    assert parse_query(query).semantic_text == semantic


# --- retrieval -------------------------------------------------------------

class _E:
    name = "s"; model = "s"; dimension = 768

    def __init__(self):
        self.queries: list[str] = []

    def embed_documents(self, t): return [[1.0] * 768 for _ in t]

    def embed_query(self, t):
        self.queries.append(t)
        return [0.5] * 768

    def health_check(self): pass


_MESSAGES = {
    "inv": ("Your invoice", "Invoice number 7 attached."),
    "bill": ("Electricity bill", "Your bill for March."),
    "overdue": ("Payment overdue", "Your payment is overdue."),
    "split": ("Overdue notice", "No payment received yet."),
    "anthropic": ("Anthropic invoice", "Invoice from Anthropic."),
    "qantas": ("Order confirmation - Qantas", "Thanks for flying."),
}


@pytest.fixture
def searcher(db_dsn, db_conn):
    ids: dict[str, int] = {}
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO accounts (name,email_address,imap_host,auth_method)"
                    " VALUES ('a','a@x','h','password') RETURNING id")
        acct = cur.fetchone()[0]
        for i, (key, (subject, body)) in enumerate(_MESSAGES.items()):
            cur.execute(
                "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
                " body_text, headers, raw_bytes, size_bytes, internal_date)"
                " VALUES (%s,%s,%s,%s,%s,'{}'::jsonb,'r',1,%s) RETURNING id",
                (acct, f"<m{i}>", bytes([i + 1]) * 32, subject, body,
                 datetime(2026, 3, i + 1, tzinfo=timezone.utc)),
            )
            ids[key] = cur.fetchone()[0]
    db_conn.commit()
    pool = open_pool(db_dsn)
    embeddings = _E()
    s = Searcher(pool=pool, cfg=SearchConfig(), embeddings=embeddings, reranker=None)
    try:
        yield s, ids, embeddings
    finally:
        pool.close()


def _hits(s, ids, query, sort):
    page = s.search(query, allowed_account_ids=None, page_size=50, user_id=1,
                    sort=sort, use_cache=False)
    by_id = {v: k for k, v in ids.items()}
    return {by_id[r.message_id] for r in page.results}


@pytest.mark.parametrize("sort", ["date", "rank"])
@pytest.mark.parametrize(("query", "expected"), [
    ("invoice OR bill", {"inv", "bill", "anthropic"}),
    ('"payment overdue"', {"overdue"}),
    ("invoice -anthropic", {"inv"}),
    # Plain queries keep their meaning: every word required.
    ("payment overdue", {"overdue", "split"}),
    ("Order confirmation - Qantas", {"qantas"}),
])
def test_the_syntax_reaches_the_lexical_match(searcher, sort, query, expected):
    s, ids, _ = searcher
    assert _hits(s, ids, query, sort) == expected


def test_the_embedding_sees_the_query_without_its_operators(searcher):
    s, ids, embeddings = searcher
    _hits(s, ids, 'invoice OR "electricity bill" -anthropic', "rank")
    assert embeddings.queries == ["invoice electricity bill"]


def test_an_all_exclusions_query_embeds_nothing(searcher):
    """Embedding "" would rank the vector arms by distance to nothing."""
    s, ids, embeddings = searcher
    assert _hits(s, ids, "-anthropic", "rank") == set(_MESSAGES) - {"anthropic"}
    assert embeddings.queries == []
