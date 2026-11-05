# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The page-cache value is a checked object, not a bag (#328).

The pool cache used to hold a ``dict[str, Any]`` written by two hand-matched
literals and read by subscript in three readers, so a ``put`` that forgot a
key surfaced as a ``KeyError`` in a different method, at continuation time.
#426 showed the cost: it added two keys and had to add them at both writers
by hand. A ``PoolEntry`` with no defaults moves that failure to the ``put``,
and ``PageCache``'s ``value_type`` refuses a raw dict there too, because CI
runs no mypy (#285) and a type hint alone would guard nothing.
"""

from __future__ import annotations

import dataclasses

import pytest

from localmail.config import SearchConfig
from localmail.search.page_cache import PageCache
from localmail.search.pool_entry import PoolEntry
from localmail.search.query import parse_query
from localmail.search.searcher import Searcher


def _fields() -> dict[str, object]:
    return {
        "parsed": parse_query("invoice"), "hydrated": [], "scores": [],
        "candidates_per_arm": 50, "rerank_pool_size": 20, "page_size": 5,
        "user_id": None, "sort": "rank", "sort_order": "desc",
        "first_page": 1, "excluded_ids": frozenset(),
    }


def test_the_fixture_names_every_field():
    # Without this, a field added to PoolEntry but not to _fields() would
    # make every construction below fail for the wrong reason.
    assert set(_fields()) == {f.name for f in dataclasses.fields(PoolEntry)}
    PoolEntry(**_fields())


@pytest.mark.parametrize("name", [f.name for f in dataclasses.fields(PoolEntry)])
def test_every_field_is_required(name):
    kwargs = _fields()
    del kwargs[name]
    with pytest.raises(TypeError, match=name):
        PoolEntry(**kwargs)


def test_an_entry_is_frozen():
    entry = PoolEntry(**_fields())
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.sort = "date"  # type: ignore[misc]


def test_the_cache_refuses_a_value_of_the_wrong_type_at_put():
    cache: PageCache[PoolEntry] = PageCache(maxsize=2, ttl_s=60,
                                            value_type=PoolEntry)
    with pytest.raises(TypeError, match="PoolEntry"):
        cache.put("tok", dict(_fields()))  # type: ignore[arg-type]
    cache.put("tok", PoolEntry(**_fields()))
    assert cache.get("tok").page_size == 5


def test_the_searcher_s_cache_holds_pool_entries_only():
    # The wiring half: a PageCache that checks nothing would pass the test
    # above and still let a raw dict into the Searcher's cache.
    s = Searcher(pool=None, cfg=SearchConfig(), embeddings=None,  # type: ignore[arg-type]
                 reranker=None, rewriter=None)
    with pytest.raises(TypeError, match="PoolEntry"):
        s._cache.put("tok", dict(_fields()))
