# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The response says whether the query could have been ranked at all (#353).

``sort_applied`` (#345) reports which ordering *ran*. That is exact only for
a caller that states nothing: a stated ``date`` is honoured for every query,
so ``sort_applied == "date"`` cannot distinguish "there was nothing to rank"
from "rank was available and not chosen".

The GUI inferred the first from the second — ``relevanceUnavailable`` read
``applied === "date" && requested === "rank"`` — and #353 is the bill. A
textless search disables **Relevance** and checks **Date**; clicking the
already-checked Date fires no ``change`` event, so the preference was never
recorded, and the obvious fix (record on ``click``) made ``requested`` become
``"date"``, which flipped the inference and **re-enabled Relevance on a query
that genuinely cannot be ranked**.

``rankable`` states the fact directly, so the client stops inferring. It is
stamped on ``SearchPage`` by the branch that produced the rows, beside
``sort_applied`` and for the same reason, and derived at every site from the
query that page was built from — never restated as a literal.

The one site where a derivation would be *wrong* is ``_empty_grown_page``,
which builds ``query=parse_query("")`` as a stand-in for an exhausted pool.
A ``rankable`` computed from ``page.query`` would report that pool
unrankable, so the field is explicit and defaultless rather than a property.
``test_a_pool_exhausted_at_the_cap_reports_the_pool_as_rankable`` is the pin.
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

    The grant is not decoration: ``run_search`` treats **both** ``None`` and
    ``[]`` as grant-nothing and short-circuits before the Searcher, so a
    wire test written with ``None`` would assert the short-circuit's own
    value and never reach ``page.rankable``.
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
# The page reports the query it was built from.
# --------------------------------------------------------------------------

def test_a_query_with_text_is_rankable(searcher):
    page = searcher.search("needle", allowed_account_ids=None, user_id=1)
    assert page.rankable is True


def test_a_textless_query_is_not_rankable(searcher):
    page = searcher.search("", allowed_account_ids=None, user_id=1)
    assert page.rankable is False


def test_a_query_of_only_filter_operators_is_not_rankable(searcher):
    """`subject:dated` is a non-blank *request field* and textless to
    `parse_query` — the shape a client-side test of the raw box calls
    rankable, which is why the client is told rather than left to guess."""
    page = searcher.search("subject:dated", allowed_account_ids=None, user_id=1)
    assert page.rankable is False


def test_a_stated_date_on_a_text_query_stays_rankable(searcher):
    """The discriminating case, and the whole reason this field exists.

    ``sort_applied`` is ``date`` here — indistinguishable on the wire from
    the textless case above — while rank was available the entire time.
    """
    page = searcher.search("needle", allowed_account_ids=None, user_id=1,
                           sort="date")
    assert page.sort_applied == "date"
    assert page.rankable is True


def test_continuing_a_pool_reports_the_pool_s_own_query(searcher):
    first = searcher.search("needle", allowed_account_ids=None, user_id=1,
                            page_size=2)
    assert first.search_token is not None
    nxt = searcher.continue_page(first.search_token, 2, user_id=1)
    assert nxt.rankable is True


def test_growing_a_pool_reports_the_pool_s_own_query(searcher):
    first = searcher.search("needle", allowed_account_ids=None, user_id=1,
                            page_size=2, candidates_per_arm=2)
    assert first.search_token is not None
    grown = searcher.grow_pool(first.search_token, 8, user_id=1)
    assert grown.rankable is True


def test_a_keyset_continuation_of_a_textless_walk_is_not_rankable(searcher):
    first = searcher.search("", allowed_account_ids=None, user_id=1, page_size=2)
    assert first.next_keyset is not None
    nxt = searcher.search("", allowed_account_ids=None, user_id=1, page_size=2,
                          keyset_cursor=first.next_keyset)
    assert nxt.rankable is False


def test_a_keyset_continuation_of_a_text_walk_stays_rankable(searcher):
    """A ``sort="date"`` walk over a query that *does* have text. Its pages
    report ``sort_applied="date"`` throughout and rank was never gone."""
    first = searcher.search("needle", allowed_account_ids=None, user_id=1,
                            page_size=2, sort="date")
    assert first.next_keyset is not None
    nxt = searcher.search("needle", allowed_account_ids=None, user_id=1,
                          page_size=2, sort="date",
                          keyset_cursor=first.next_keyset)
    assert nxt.sort_applied == "date"
    assert nxt.rankable is True


def test_a_cached_pool_reports_itself_rankable(searcher):
    """The invariant, asserted rather than assumed: a pool is only ever
    built on the rank branch, which is unreachable without free text. It is
    what makes ``_empty_grown_page`` able to stand in for one."""
    first = searcher.search("needle", allowed_account_ids=None, user_id=1,
                            page_size=2)
    assert first.search_token is not None
    meta = searcher.get_pool_metadata(first.search_token, user_id=1)
    assert meta is not None
    assert meta.rankable is True


# --------------------------------------------------------------------------
# It reaches the wire, on every branch of `run_search`.
# --------------------------------------------------------------------------

def test_run_search_puts_rankability_on_the_wire(seeded):
    searcher, acct = seeded
    out = run_search(searcher=searcher, free_text="needle", filters={},
                     limit=5, allowed_account_ids=[acct], user_id=1)
    assert out["results"], "fixture stopped seeding; the assertion below is vacuous"
    assert out["rankable"] is True


def test_run_search_reports_a_textless_query_as_unrankable(seeded):
    searcher, acct = seeded
    out = run_search(searcher=searcher, free_text="", filters={},
                     limit=5, allowed_account_ids=[acct], user_id=1)
    assert out["results"]
    assert out["sort_applied"] == "date"
    assert out["rankable"] is False


def test_the_wire_separates_a_chosen_date_from_an_imposed_one(seeded):
    """Both responses carry ``sort_applied="date"``; only ``rankable``
    tells them apart. Asserted side by side because that indistinguishability
    is precisely what #353 was."""
    searcher, acct = seeded
    chosen = run_search(searcher=searcher, free_text="needle", filters={},
                        limit=5, allowed_account_ids=[acct], user_id=1,
                        sort="date")
    imposed = run_search(searcher=searcher, free_text="subject:dated",
                         filters={}, limit=5, allowed_account_ids=[acct],
                         user_id=1)
    assert chosen["sort_applied"] == imposed["sort_applied"] == "date"
    assert chosen["rankable"] is True
    assert imposed["rankable"] is False


def test_a_pool_continuation_reports_rankability_on_the_wire(seeded):
    searcher, acct = seeded
    first = run_search(searcher=searcher, free_text="needle", filters={},
                       limit=2, allowed_account_ids=[acct], user_id=1)
    assert first["next_cursor"] is not None
    nxt = run_search(searcher=searcher, free_text="needle", filters={},
                     limit=2, allowed_account_ids=[acct], user_id=1,
                     cursor=first["next_cursor"])
    assert nxt["rankable"] is True


def test_a_keyset_continuation_reports_rankability_on_the_wire(seeded):
    searcher, acct = seeded
    first = run_search(searcher=searcher, free_text="", filters={},
                       limit=2, allowed_account_ids=[acct], user_id=1)
    assert first["next_cursor"] is not None
    nxt = run_search(searcher=searcher, free_text="", filters={},
                     limit=2, allowed_account_ids=[acct], user_id=1,
                     cursor=first["next_cursor"])
    assert nxt["results"]
    assert nxt["rankable"] is False


def test_the_empty_acl_short_circuit_reports_rankability_too(searcher):
    """Present rather than omitted, for the reason the key exists at all:
    this branch returns ``next_cursor: None``, so a client that inferred
    would have no signal here."""
    out = run_search(searcher=searcher, free_text="needle", filters={},
                     limit=5, allowed_account_ids=[], user_id=1)
    assert out["results"] == []
    assert out["rankable"] is True


def test_the_empty_acl_short_circuit_reports_a_textless_query_as_unrankable(
    searcher,
):
    out = run_search(searcher=searcher, free_text="subject:nothing", filters={},
                     limit=5, allowed_account_ids=[], user_id=1)
    assert out["rankable"] is False


def test_a_pool_exhausted_at_the_cap_reports_the_pool_as_rankable(searcher):
    """The pin for the finding that made this an explicit field.

    ``_empty_grown_page`` builds ``query=parse_query("")``, so a ``rankable``
    derived from ``page.query`` reports ``False`` for a pool that is rankable
    by construction. It must come from the pool's own metadata, exactly as
    ``sort_applied`` does.
    """
    cfg = searcher.config
    searcher._cache.put("tok-capped", {
        "parsed": parse_query("needle"), "hydrated": [], "scores": {},
        "page_size": 5, "candidates_per_arm": cfg.candidates_per_arm_max,
        "rerank_pool_size": 20,
        "user_id": 1, "sort": "rank", "sort_order": "desc",
    })
    out = run_search(searcher=searcher, free_text="needle", filters={},
                     limit=5, allowed_account_ids=[1], user_id=1,
                     cursor=encode_search_cursor(SearchCursor(token="tok-capped",
                                                              page=2)))
    assert out["next_cursor"] is None
    assert out["rankable"] is True


# --------------------------------------------------------------------------
# The field cannot be supplied by omission.
# --------------------------------------------------------------------------

def test_search_page_refuses_to_be_built_without_rankability():
    """No default, for the reason ``sort_applied`` has none: a page that
    could claim ``rankable=True`` by forgetting to write it re-opens #353
    for every branch that forgets."""
    with pytest.raises(TypeError):
        SearchPage(results=[], page=1, page_size=1, pool_size=0,
                   candidates_per_arm=0, has_more_in_pool=False,
                   can_grow_pool=False, search_token=None,
                   query=parse_query(""), timing_ms={}, sort_applied="rank")
