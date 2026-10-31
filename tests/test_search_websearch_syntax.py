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

from localmail.api.search import run_search
from localmail.config import SearchConfig
from localmail.db import open_pool
from localmail.search.argument_errors import SearchArgumentRefused
from localmail.search.embed_worker import run_embed_worker_once
from localmail.search.query import ParsedQuery, SearchFilters, parse_query
from localmail.search.rewriter import RewriteResult
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


@pytest.mark.parametrize("query", [
    '"payment overdue" OR bill -acme', 'a -"b c" "d"',
    "Order confirmation - Qantas", "it's late", 'foo"bar baz"', "a OR",
])
def test_free_text_reparses_to_itself(query):
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
    ("OR", ""),
    ("a OR", "a"),
    # A double dash is a double negation to Postgres, i.e. a required word.
    ("--acme", "--acme"),
])
def test_semantic_text_drops_the_operators(query, semantic):
    assert parse_query(query).semantic_text == semantic


@pytest.mark.parametrize(("query", "excluded"), [
    ("invoice -acme", "-acme"),
    ('invoice -"big co" -acme', '-"big co" -acme'),
    ("invoice", ""),
    ('"-acme"', ""),
    ("--acme", ""),
    # Inside an OR chain the exclusion is not message-wide: (a & !b) | c.
    ("a -b OR c", ""),
    ("a OR -b", ""),
    ("a -b OR c -d", "-d"),
    # No word: its tsquery is empty, and @@ an empty tsquery excludes all.
    ('a -"', ""),
    ("a -!", ""),
])
def test_excluded_text_is_what_every_path_may_exclude(query, excluded):
    assert parse_query(query).excluded_text == excluded


# --- retrieval -------------------------------------------------------------

class _Reranker:
    name = "r"; model = "r"

    def __init__(self):
        self.queries: list[str] = []

    def rerank(self, query, candidates):
        self.queries.append(query)
        return [1.0 - i * 0.001 for i, _ in enumerate(candidates)]


class _Rewriter:
    name = "w"; model = "w"

    def rewrite(self, free_text):
        return RewriteResult(rewritten_text="invoice", expansion_terms=["Anthropic"],
                             extracted_filters=SearchFilters())


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
    # "for" comes long before "bill": a snippet built from the free text
    # would centre on the "or" in "for" (#419).
    "bill": ("Electricity bill",
             "Thanks for your custom. " + "Filler words here. " * 20
             + "Your bill for March."),
    "overdue": ("Payment overdue", "Your payment is overdue."),
    "split": ("Overdue notice", "No payment received yet."),
    "anthropic": ("Anthropic invoice", "Invoice from Anthropic."),
    "qantas": ("Order confirmation - Qantas", "Thanks for flying."),
}


def _seed(db_conn) -> dict[str, int]:
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
    return ids


@pytest.fixture
def searcher(db_dsn, db_conn):
    ids = _seed(db_conn)
    pool = open_pool(db_dsn)
    embeddings = _E()
    s = Searcher(pool=pool, cfg=SearchConfig(), embeddings=embeddings, reranker=None)
    try:
        yield s, ids, embeddings
    finally:
        pool.close()


@pytest.fixture
def embedded(db_dsn, db_conn):
    """Chunks embedded, so the vector arms and chunk FTS contribute too: the
    plain fixture's rank-path results come from the message arm alone."""
    ids = _seed(db_conn)
    run_embed_worker_once(db_conn, SearchConfig(), _E())
    pool = open_pool(db_dsn)
    reranker = _Reranker()
    s = Searcher(pool=pool, cfg=SearchConfig(), embeddings=_E(),
                 reranker=reranker, rewriter=_Rewriter())
    try:
        yield s, ids, reranker
    finally:
        pool.close()


def _names(ids, page):
    by_id = {v: k for k, v in ids.items()}
    return {by_id[r.message_id] for r in page.results}


def _hits(s, ids, query, sort, **kw):
    page = s.search(query, allowed_account_ids=None, page_size=50, user_id=1,
                    sort=sort, use_cache=False, **kw)
    return _names(ids, page)


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


def test_an_all_exclusions_query_is_served_by_the_date_walk(searcher):
    """Every row scores 0 against a pure negation, so the rank path's
    ``ORDER BY score LIMIT`` would keep an arbitrary tie-set; and embedding
    "" ranks by distance to nothing. Nothing to rank means the date walk."""
    s, ids, embeddings = searcher
    page = s.search("-anthropic", allowed_account_ids=None, page_size=50,
                    user_id=1, use_cache=False)
    assert page.sort_applied == "date"
    assert page.rankable is False
    assert _names(ids, page) == set(_MESSAGES) - {"anthropic"}
    assert embeddings.queries == []


def test_ranking_an_all_exclusions_query_is_refused(searcher):
    s, _, _ = searcher
    with pytest.raises(SearchArgumentRefused, match="excluded words alone"):
        s.search("-anthropic", allowed_account_ids=None, sort="rank",
                 use_cache=False)


@pytest.mark.parametrize("sort", ["date", "rank"])
def test_an_exclusion_holds_when_every_arm_contributes(embedded, sort):
    """The vector arms match no words, so on the rank path only a
    message-level exclusion keeps the excluded message out."""
    s, ids, _ = embedded
    assert _hits(s, ids, "invoice -anthropic", sort) == (
        {"inv"} if sort == "date" else set(_MESSAGES) - {"anthropic"}
    )


@pytest.mark.parametrize("sort", ["date", "rank"])
def test_a_smart_synonym_cannot_bring_an_excluded_word_back(embedded, sort):
    s, ids, _ = embedded
    assert "anthropic" not in _hits(s, ids, "invoice -anthropic", sort, smart=True)


def test_the_reranker_sees_the_query_without_its_operators(embedded):
    """Both call sites: the first page, and the pool grown for a later one."""
    s, _, reranker = embedded
    query = 'invoice OR "electricity bill" -anthropic'
    first = s.search(query, allowed_account_ids=None, page_size=2, user_id=1)
    s.grow_pool(first.search_token, 8, user_id=1)
    assert reranker.queries == ["invoice electricity bill"] * 2


def test_the_snippet_centres_on_a_term_not_an_operator(searcher):
    s, ids, _ = searcher
    page = s.search("invoice OR bill", allowed_account_ids=None, user_id=1,
                    sort="rank", use_cache=False, snippet_chars=60)
    snippet = next(r.snippet for r in page.results if r.message_id == ids["bill"])
    assert "bill" in snippet.lower()


def test_the_syntax_survives_the_api_boundary(searcher):
    """``run_search`` composes the ACL into the query before the Searcher
    parses it; the operators must come through that composition intact."""
    s, ids, _ = searcher
    body = run_search(searcher=s, free_text="invoice -anthropic", filters={},
                      limit=50, allowed_account_ids=[1], user_id=1, sort="date")
    by_id = {str(v): k for k, v in ids.items()}
    assert {by_id[h["message_id"]] for h in body["results"]} == {"inv"}


def test_a_synonym_is_excluded_at_the_message_arm_too(db_conn):
    """The lexical arm's OR-chain lets a synonym match past ``!word``; the
    arm's message-level exclusion is what removes it."""
    from localmail.search.arms import arm_bm25_messages
    ids = _seed(db_conn)
    parsed = ParsedQuery(free_text="invoice -anthropic",
                         expansion_terms=["Anthropic"])
    hits = arm_bm25_messages(db_conn, parsed, SearchConfig(), limit=50)
    assert ids["anthropic"] not in {h.message_id for h in hits}
