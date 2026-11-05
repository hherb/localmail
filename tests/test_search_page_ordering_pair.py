"""``SearchPage`` and ``PoolMetadata`` refuse ``rank`` beside ``rankable=False``
at construction (#356).

#355 found the pair reachable on the wire and fixed that emitter; #358 made
``search_response`` refuse it. Library and CLI callers read ``SearchPage``
and ``PoolMetadata`` directly, so the guarantee has to hold there too — by
construction, not by every emitter deriving both fields from one string.
One rule, ``sort_axes.ordering_pair_error``, shared by all three.
"""
from __future__ import annotations

import pytest

from localmail.search.query import parse_query
from localmail.search.searcher import PoolMetadata, SearchPage
from localmail.search.sort_axes import ordering_pair_error


def _page(*, sort_applied, rankable) -> SearchPage:
    return SearchPage(
        results=[], page=1, page_size=10, pool_size=0, candidates_per_arm=50,
        has_more_in_pool=False, can_grow_pool=False, search_token=None,
        query=parse_query(""), timing_ms={},
        sort_applied=sort_applied, rankable=rankable,
    )


def _meta(*, sort, rankable) -> PoolMetadata:
    return PoolMetadata(
        candidates_per_arm=50, page_size=10, rerank_pool_size=100,
        pool_size=0, sort=sort, sort_order="desc", rankable=rankable,
    )


@pytest.mark.parametrize(("sort", "rankable"), [
    ("rank", True), ("date", True), ("date", False),
])
def test_every_other_pair_is_accepted(sort, rankable) -> None:
    assert ordering_pair_error(sort=sort, rankable=rankable) is None
    _page(sort_applied=sort, rankable=rankable)
    _meta(sort=sort, rankable=rankable)


def test_the_rule_names_the_pair() -> None:
    message = ordering_pair_error(sort="rank", rankable=False)
    assert message is not None
    assert "rank" in message and "rankable=False" in message


def test_a_search_page_refuses_rank_beside_unrankable() -> None:
    with pytest.raises(ValueError, match="rankable=False"):
        _page(sort_applied="rank", rankable=False)


def test_pool_metadata_refuses_rank_beside_unrankable() -> None:
    with pytest.raises(ValueError, match="rankable=False"):
        _meta(sort="rank", rankable=False)


def test_the_wire_constructor_words_it_the_same_way() -> None:
    # One rule, one wording: the wire refusal is the shared message.
    from localmail.api.search_response import search_response
    from localmail.search.rewrite_status import NOT_REQUESTED

    with pytest.raises(ValueError) as caught:
        search_response(
            results=[], next_cursor=None, took_ms=1.0, sort_applied="rank",
            rankable=False, rewrite_status=NOT_REQUESTED, rewrite_note=None,
            rewrite_note_code=None,
        )
    assert str(caught.value) == ordering_pair_error(sort="rank", rankable=False)
