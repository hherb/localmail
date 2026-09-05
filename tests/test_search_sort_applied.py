# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The response says which ordering actually ran (#345).

``sort`` is a *request*, and since #324 the server resolves it from the
query rather than honouring it verbatim: a query with no free text — blank,
or only filter operators — has nothing for the hybrid pool to rank, so it is
served date-ordered whatever was asked for. Nothing on the wire said so, and
the GUI's only rendering of ``sort`` is a radio bound to the request — so a
textless search showed **Relevance** checked over date-ordered rows, and
clicking Relevance re-ran the search and changed nothing.

``sort_applied`` closes that by reporting the resolution. It is stamped on
``SearchPage`` by the branch that produced the rows, the way ``next_keyset``
is, so no layer above can supply an ordering the walk did not use.

The alternative — inferring it client-side from the returned cursor's
prefix — was measured against the live archive and rejected: a textless
search matching fewer rows than the page size returns ``next_cursor: None``,
so there is no signal at all on exactly the narrow-filter case a user reaches
deliberately. It would also put a fourth copy of ``_KEYSET_PREFIXES`` in a
language that cannot import it.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from localmail.api.search import run_search
from localmail.api.search_cursor import SearchCursor, encode_search_cursor
from localmail.config import SearchConfig
from localmail.db import open_pool
from localmail.search.query import parse_query
from localmail.search.searcher import SearchPage, Searcher


class _E:
    name = "s"; model = "s"; dimension = 768
    def embed_documents(self, t): return [[1.0] * 768 for _ in t]
    def embed_query(self, t): return [0.5] * 768
    def health_check(self): pass


def _seed(conn, *, n=6):
    """``n`` dated messages sharing the token ``needle``."""
    with conn.cursor() as cur:
        cur.execute("INSERT INTO accounts (name,email_address,imap_host,auth_method)"
                    " VALUES ('a','a@x','h','password') RETURNING id")
        acct = int(cur.fetchone()[0])
        for i in range(n):
            cur.execute(
                "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
                " body_text, headers, raw_bytes, size_bytes, internal_date)"
                " VALUES (%s,%s,%s,%s,%s,'{}'::jsonb,'r',1,%s)",
                (acct, f"<d{i}>", bytes([i + 1]) * 32, f"dated {i} needle",
                 "body needle", datetime(2026, 1, i + 1, tzinfo=timezone.utc)),
            )
    conn.commit()
    return acct


@pytest.fixture()
def seeded(db_dsn, db_conn):
    """(searcher, account_id).

    The account id is not decoration. ``run_search`` treats **both** ``None``
    and ``[]`` as a grant-nothing ACL and short-circuits before the Searcher
    is reached — so a wire test written with ``allowed_account_ids=None``
    asserts the short-circuit's own value and never touches
    ``page.sort_applied`` at all. Every wire test below that expects rows
    therefore passes a real grant, and the two short-circuit tests pass
    ``[]`` on purpose.
    """
    acct = _seed(db_conn)
    pool = open_pool(db_dsn)
    try:
        yield (Searcher(pool=pool, embeddings=_E(), reranker=None,
                        cfg=SearchConfig(reranker_enabled=False)),
               acct)
    finally:
        pool.close()


@pytest.fixture()
def searcher(seeded):
    return seeded[0]


# --------------------------------------------------------------------------
# The page reports the branch that produced it.
# --------------------------------------------------------------------------

def test_a_query_with_text_reports_rank(searcher):
    page = searcher.search("needle", allowed_account_ids=None, user_id=1)
    assert page.sort_applied == "rank"


def test_a_textless_query_reports_date_even_though_none_was_stated(searcher):
    """The defect #345 is about: the caller stated nothing, meaning `rank`
    to every client that renders the request, and `date` is what ran."""
    page = searcher.search("", allowed_account_ids=None, user_id=1)
    assert page.sort_applied == "date"


def test_a_query_of_only_filter_operators_reports_date(searcher):
    """`subject:dated` is a non-blank *request field* and textless to
    `parse_query`. A client-side test of the raw box calls this rankable."""
    page = searcher.search("subject:dated", allowed_account_ids=None, user_id=1)
    assert page.sort_applied == "date"


def test_a_stated_date_with_text_reports_date(searcher):
    page = searcher.search("needle", allowed_account_ids=None, user_id=1,
                           sort="date")
    assert page.sort_applied == "date"


def test_continuing_a_pool_reports_the_pool_s_own_sort(searcher):
    """A continuation page must not report the default; it reports what the
    cached pool was built with, which is what `PoolMetadata.sort` records."""
    first = searcher.search("needle", allowed_account_ids=None, user_id=1,
                            page_size=2)
    assert first.search_token is not None
    nxt = searcher.continue_page(first.search_token, 2, user_id=1)
    assert nxt.sort_applied == first.sort_applied == "rank"


def test_growing_a_pool_reports_the_pool_s_own_sort(searcher):
    first = searcher.search("needle", allowed_account_ids=None, user_id=1,
                            page_size=2, candidates_per_arm=2)
    assert first.search_token is not None
    grown = searcher.grow_pool(first.search_token, 8, user_id=1)
    assert grown.sort_applied == "rank"


def test_a_keyset_continuation_reports_date(searcher):
    """The date walk's own continuation, which carries no pool at all."""
    first = searcher.search("", allowed_account_ids=None, user_id=1, page_size=2)
    assert first.next_keyset is not None
    nxt = searcher.search("", allowed_account_ids=None, user_id=1, page_size=2,
                          keyset_cursor=first.next_keyset)
    assert nxt.sort_applied == "date"


# --------------------------------------------------------------------------
# It reaches the wire, on every branch of `run_search`.
# --------------------------------------------------------------------------

def test_run_search_puts_the_applied_sort_on_the_wire(seeded):
    searcher, acct = seeded
    out = run_search(searcher=searcher, free_text="needle", filters={},
                     limit=5, allowed_account_ids=[acct], user_id=1)
    assert out["results"], "fixture must return rows or this asserts nothing"
    assert out["sort_applied"] == "rank"


def test_run_search_reports_date_for_a_textless_query(seeded):
    searcher, acct = seeded
    out = run_search(searcher=searcher, free_text="", filters={},
                     limit=5, allowed_account_ids=[acct], user_id=1)
    assert out["results"], "fixture must return rows or this asserts nothing"
    assert out["sort_applied"] == "date"


def test_a_short_final_page_still_reports_its_ordering(seeded):
    """The case that rules out inferring from the cursor: the request is
    served, matches nothing, and comes back with no cursor to read."""
    searcher, acct = seeded
    out = run_search(searcher=searcher, free_text="subject:zzznomatch",
                     filters={}, limit=5, allowed_account_ids=[acct], user_id=1)
    assert out["results"] == []
    assert out["next_cursor"] is None
    assert out["sort_applied"] == "date"


def test_a_pool_continuation_reports_the_pool_s_sort_on_the_wire(seeded):
    searcher, acct = seeded
    first = run_search(searcher=searcher, free_text="needle", filters={},
                       limit=2, allowed_account_ids=[acct], user_id=1)
    assert first["next_cursor"] is not None
    nxt = run_search(searcher=searcher, free_text="needle", filters={},
                     limit=2, allowed_account_ids=[acct], user_id=1,
                     cursor=first["next_cursor"])
    assert nxt["sort_applied"] == "rank"


def test_a_keyset_continuation_reports_date_on_the_wire(seeded):
    searcher, acct = seeded
    first = run_search(searcher=searcher, free_text="", filters={},
                       limit=2, allowed_account_ids=[acct], user_id=1)
    assert first["next_cursor"] is not None
    nxt = run_search(searcher=searcher, free_text="", filters={},
                     limit=2, allowed_account_ids=[acct], user_id=1,
                     cursor=first["next_cursor"])
    assert nxt["sort_applied"] == "date"


# --------------------------------------------------------------------------
# The empty-ACL short-circuit, which never reaches the Searcher.
# --------------------------------------------------------------------------

def test_the_empty_acl_short_circuit_reports_an_ordering_too(searcher):
    """That branch answers with an empty page and `next_cursor: None` — so
    it carries no signal an inference could read, and must state the
    ordering rather than omit the key."""
    out = run_search(searcher=searcher, free_text="", filters={},
                     limit=5, allowed_account_ids=[], user_id=1)
    assert out["results"] == []
    assert out["next_cursor"] is None
    assert out["sort_applied"] == "date"


def test_the_empty_acl_short_circuit_reports_rank_for_a_text_query(searcher):
    """Positive control: it resolves the request, it does not hardcode."""
    out = run_search(searcher=searcher, free_text="needle", filters={},
                     limit=5, allowed_account_ids=[], user_id=1)
    assert out["sort_applied"] == "rank"


# --------------------------------------------------------------------------
# The pool path reads the pool, rather than restating "pool ⟹ rank".
# --------------------------------------------------------------------------

def test_continue_page_reads_the_pool_s_sort_rather_than_assuming_rank(searcher):
    """The rank assertions above cannot catch a hardcoded ``"rank"``.

    A pool cursor is only ever minted by the hybrid branch today, so every
    end-to-end pool page is rank-ordered and a report that ignored the entry
    would agree with all of them. Only a date-built pool separates the two —
    the technique
    ``test_searcher_pool_metadata.py::test_pool_metadata_reports_the_sort_the_pool_was_actually_built_with``
    uses, and for the same reason: encoding "pool ⟹ rank" in a reader makes
    a future dispatch change silently wrong.
    """
    searcher._cache.put("tok-date", {
        "parsed": parse_query("needle"), "hydrated": [], "scores": {},
        "page_size": 5, "candidates_per_arm": 50, "rerank_pool_size": 20,
        "user_id": None, "sort": "date", "sort_order": "desc",
    })
    assert searcher.continue_page("tok-date", 1).sort_applied == "date"


def test_a_pool_exhausted_at_the_cap_reports_the_pool_s_sort(searcher):
    """``_empty_grown_page`` stands in for a page ``continue_page`` would
    have served, so it must report that pool's ordering and not a default."""
    cfg = searcher.config
    searcher._cache.put("tok-capped", {
        "parsed": parse_query("needle"), "hydrated": [], "scores": {},
        "page_size": 5, "candidates_per_arm": cfg.candidates_per_arm_max,
        "rerank_pool_size": 20,
        "user_id": 1, "sort": "date", "sort_order": "desc",
    })
    out = run_search(searcher=searcher, free_text="needle", filters={},
                     limit=5, allowed_account_ids=[1], user_id=1,
                     cursor=encode_search_cursor(SearchCursor(token="tok-capped",
                                                              page=2)))
    assert out["next_cursor"] is None
    assert out["sort_applied"] == "date"


# --------------------------------------------------------------------------
# The field cannot be supplied by omission.
# --------------------------------------------------------------------------

def test_search_page_refuses_to_be_built_without_an_applied_sort():
    """No default, for the reason ``KeysetCursor.order`` has none: a page
    that could claim ``rank`` by forgetting to write it is #345 itself."""
    with pytest.raises(TypeError):
        SearchPage(results=[], page=1, page_size=1, pool_size=0,
                   candidates_per_arm=0, has_more_in_pool=False,
                   can_grow_pool=False, search_token=None,
                   query=parse_query(""), timing_ms={})
