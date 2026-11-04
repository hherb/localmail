"""The /v1/search response has one constructor, and it refuses a fake (#358).

``run_search`` returned ``dict[str, Any]`` from two hand-written ``return``
literals plus the empty-ACL dict, so a new wire field was a bare string key
and a ``MagicMock`` page with the attribute unset *serialised* (as ``{}`` or
``[]``, depending on the path) instead of raising. #345 and #353 both shipped
that garbage with the suite green. ``search_response`` is now the only way
the response is built, and it checks the values a fake launders.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from typing import get_type_hints
from unittest.mock import MagicMock

import pytest

from localmail.api import search as api_search
from localmail.api.search_response import SearchResponse, search_response
from localmail.search.rewrite_status import (
    APPLIED,
    FAILED,
    NOT_REQUESTED,
    UNAVAILABLE,
)


def _ok(**over):
    kwargs = dict(results=[], next_cursor=None, took_ms=1.5,
                  sort_applied="date", rankable=False,
                  rewrite_status=NOT_REQUESTED, rewrite_note=None,
                  rewrite_note_code=None)
    kwargs.update(over)
    return search_response(**kwargs)


def test_the_response_carries_exactly_the_declared_keys():
    assert set(_ok()) == set(get_type_hints(SearchResponse))


def test_total_estimate_is_always_null():
    # #175: never 0, on every path.
    assert _ok()["total_estimate"] is None


@pytest.mark.parametrize("status,skipped", [
    (APPLIED, False), (NOT_REQUESTED, False),
    (UNAVAILABLE, True), (FAILED, True),
])
def test_rewrite_skipped_is_derived_from_the_status(status, skipped):
    assert _ok(rewrite_status=status)["rewrite_skipped"] is skipped


def test_a_valid_rank_response_is_accepted():
    assert _ok(sort_applied="rank", rankable=True)["sort_applied"] == "rank"


@pytest.mark.parametrize("field", ["sort_applied", "rankable", "took_ms"])
def test_an_unset_mock_attribute_is_refused(field):
    with pytest.raises(TypeError, match=field):
        _ok(**{field: MagicMock()})


@pytest.mark.parametrize("value", ["Date", "", None])
def test_an_unknown_ordering_is_refused(value):
    with pytest.raises(TypeError, match="sort_applied"):
        _ok(sort_applied=value)


@pytest.mark.parametrize("value", [1, 0, "false", None])
def test_rankable_must_be_a_real_bool(value):
    with pytest.raises(TypeError, match="rankable"):
        _ok(rankable=value)


def test_took_ms_may_not_be_a_bool():
    with pytest.raises(TypeError, match="took_ms"):
        _ok(took_ms=True)


def test_rank_beside_unrankable_is_refused():
    # The pair #355 found reachable on the wire (#356's wire half): a
    # checked AND disabled Relevance radio in the GUI.
    with pytest.raises(ValueError, match="rankable"):
        _ok(sort_applied="rank", rankable=False)


def test_every_run_search_return_goes_through_the_constructor():
    """Structural: a dict literal returned beside the constructor is the
    drift this module exists to end, and nothing at runtime would see it."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(api_search.run_search)))
    returns = [n for n in ast.walk(tree) if isinstance(n, ast.Return)]
    assert returns, "run_search has no return statement?"
    for ret in returns:
        assert (isinstance(ret.value, ast.Call)
                and isinstance(ret.value.func, ast.Name)
                and ret.value.func.id == "search_response"), ast.unparse(ret)


def test_run_search_refuses_a_page_without_its_ordering_fields():
    """End to end through the real function: the MagicMock trap now raises
    instead of reaching the wire."""
    page = MagicMock()
    page.results = []
    page.timing_ms = {"total": 2.0}
    page.next_keyset = None
    page.rewrite_status = NOT_REQUESTED
    page.rewrite_note = None
    page.rewrite_note_code = None
    page.token = None
    searcher = MagicMock()
    searcher.search.return_value = page
    searcher.smart_available = False
    with pytest.raises(TypeError, match="sort_applied"):
        api_search.run_search(searcher=searcher, free_text="invoice",
                              filters={}, limit=10, allowed_account_ids=[1],
                              user_id=1)
