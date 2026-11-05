"""``SearchPage``, ``PoolMetadata`` and ``search_response`` refuse ``rank``
beside ``rankable=False`` at construction (#356).

#355 found the pair reachable on the wire and fixed that emitter; #358 made
``search_response`` refuse it. Library and CLI callers read ``SearchPage``
directly, and library callers and api/ read ``PoolMetadata`` through
``get_pool_metadata``, so the guarantee has to hold there too — by
construction, not by every emitter deriving both fields from one string.
One rule, ``sort_axes.ordering_pair_error``, shared by all three.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from localmail.api.search_response import search_response
from localmail.search.query import parse_query
from localmail.search.rewrite_status import NOT_REQUESTED
from localmail.search.searcher import PoolMetadata, SearchPage
from localmail.search.sort_axes import ordering_pair_error


def _page(*, sort, rankable) -> SearchPage:
    return SearchPage(
        results=[], page=1, page_size=10, pool_size=0, candidates_per_arm=50,
        has_more_in_pool=False, can_grow_pool=False, search_token=None,
        query=parse_query(""), timing_ms={},
        sort_applied=sort, rankable=rankable,
    )


def _meta(*, sort, rankable) -> PoolMetadata:
    return PoolMetadata(
        candidates_per_arm=50, page_size=10, rerank_pool_size=100,
        pool_size=0, sort=sort, sort_order="desc", rankable=rankable,
    )


def _wire(*, sort, rankable):
    return search_response(
        results=[], next_cursor=None, took_ms=1.0, sort_applied=sort,
        rankable=rankable, rewrite_status=NOT_REQUESTED, rewrite_note=None,
        rewrite_note_code=None,
    )


_HOLDERS = pytest.mark.parametrize(
    "build", [_page, _meta, _wire],
    ids=["SearchPage", "PoolMetadata", "search_response"])


def test_the_rule_words_the_pair_exactly() -> None:
    # The one literal: every other test compares against the function itself,
    # which would survive any rewording.
    assert ordering_pair_error(sort="rank", rankable=False) == (
        "sort='rank' beside rankable=False: resolve_sort cannot return "
        "'rank' for a query with nothing to rank")


@_HOLDERS
@pytest.mark.parametrize(("sort", "rankable"), [
    ("rank", True), ("date", True), ("date", False),
])
def test_every_other_pair_is_accepted(build, sort, rankable) -> None:
    assert ordering_pair_error(sort=sort, rankable=rankable) is None
    build(sort=sort, rankable=rankable)


@_HOLDERS
def test_each_holder_refuses_with_the_shared_message(build) -> None:
    # Equality, not ``match=``: a hand-written copy of the rule containing
    # "rankable=False" would satisfy a substring match.
    with pytest.raises(ValueError) as caught:
        build(sort="rank", rankable=False)
    assert str(caught.value) == ordering_pair_error(sort="rank", rankable=False)


@pytest.mark.parametrize("build", [_page, _meta],
                         ids=["SearchPage", "PoolMetadata"])
@pytest.mark.parametrize("rankable", [MagicMock(), "false", 1, None],
                         ids=["mock", "str", "int", "none"])
def test_only_a_real_true_may_sit_beside_rank(build, rankable) -> None:
    # The dataclasses check no types, so truthiness would wave these through.
    # (``search_response`` refuses a non-bool with a TypeError first.)
    with pytest.raises(ValueError) as caught:
        build(sort="rank", rankable=rankable)
    assert str(caught.value) == ordering_pair_error(sort="rank", rankable=rankable)
    assert f"rankable={rankable!r}" in str(caught.value)
