# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""``sort_order`` at the api boundary: threading, refusal, and cursor minting."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from localmail.api.errors import ValidationFailed
from localmail.api.search import run_search
from localmail.api.search_cursor import encode_keyset_cursor, keyset_order
from localmail.search.searcher import KeysetCursor, PoolMetadata

_KS = KeysetCursor(ts=datetime(2026, 5, 21, tzinfo=timezone.utc), id=100,
                   order="desc")
#: The same position, ascending. A fake standing in for an ascending walk
#: has to return *this*: the direction now travels on the cursor the
#: Searcher produces, so a fake returning the descending one is modelling a
#: descending walk however the request was phrased.
_KS_ASC = replace(_KS, order="asc")


def _page(*, token=None, next_keyset=None):
    p = MagicMock()
    p.results = []
    p.search_token = token
    p.pool_size = 0
    p.page_size = 2
    p.page = 1
    p.has_more_in_pool = False
    p.can_grow_pool = False
    p.candidates_per_arm = 50
    p.timing_ms = {"total": 1.0}
    p.next_keyset = next_keyset
    p.rewrite_status = "not_requested"
    p.rewrite_note = None
    p.rewrite_note_code = None
    return p


def _searcher(page=None):
    s = MagicMock()
    s.config.candidates_per_arm = 50
    s.config.candidates_per_arm_max = 800
    s.smart_available = False
    s.search.return_value = page or _page()
    return s


def test_a_stated_order_reaches_the_searcher() -> None:
    s = _searcher()
    run_search(searcher=s, free_text="invoice", filters={}, limit=2,
               allowed_account_ids=[1], user_id=99, sort="date",
               sort_order="asc")
    _, kwargs = s.search.call_args
    assert kwargs.get("sort_order") == "asc"


def test_an_unstated_order_reaches_the_searcher_as_desc() -> None:
    """Resolved at this boundary, from the one shared default."""
    s = _searcher()
    run_search(searcher=s, free_text="invoice", filters={}, limit=2,
               allowed_account_ids=[1], user_id=99, sort="date")
    _, kwargs = s.search.call_args
    assert kwargs.get("sort_order") == "desc"


def test_rank_with_ascending_is_a_validation_error_not_a_search() -> None:
    s = _searcher()
    with pytest.raises(ValidationFailed, match="sort_order"):
        run_search(searcher=s, free_text="invoice", filters={}, limit=2,
                   allowed_account_ids=[1], user_id=99, sort="rank",
                   sort_order="asc")
    s.search.assert_not_called()


def test_rank_with_ascending_is_refused_even_with_an_empty_acl() -> None:
    """Validation precedes the empty-ACL short-circuit.

    That branch answers with an empty page, byte-identical to "you have
    reached the end" — so a grant-nothing caller would be told a
    contradictory request had succeeded and was complete.
    """
    s = _searcher()
    with pytest.raises(ValidationFailed):
        run_search(searcher=s, free_text="invoice", filters={}, limit=2,
                   allowed_account_ids=[], user_id=99, sort="rank",
                   sort_order="asc")


def test_an_ascending_page_mints_an_ascending_cursor() -> None:
    s = _searcher(_page(next_keyset=_KS_ASC))
    out = run_search(searcher=s, free_text="invoice", filters={}, limit=2,
                     allowed_account_ids=[1], user_id=99, sort="date",
                     sort_order="asc")
    assert keyset_order(out["next_cursor"]) == "asc", (
        "an ascending walk minted a descending cursor: the next page would "
        "silently reverse"
    )


def test_a_descending_page_mints_a_descending_cursor() -> None:
    s = _searcher(_page(next_keyset=_KS))
    out = run_search(searcher=s, free_text="invoice", filters={}, limit=2,
                     allowed_account_ids=[1], user_id=99, sort="date")
    assert keyset_order(out["next_cursor"]) == "desc"


def test_an_ascending_cursor_alone_continues_ascending() -> None:
    """The documented round trip, end to end through run_search."""
    s = _searcher()
    raw = encode_keyset_cursor(replace(_KS, order="asc"))
    run_search(searcher=s, free_text="invoice", filters={}, limit=2,
               allowed_account_ids=[1], user_id=99, cursor=raw)
    _, kwargs = s.search.call_args
    assert kwargs.get("sort") == "date"
    assert kwargs.get("sort_order") == "asc"
    assert kwargs.get("keyset_cursor") == _KS_ASC


def test_a_stated_order_contradicting_the_cursor_is_a_400() -> None:
    s = _searcher()
    raw = encode_keyset_cursor(replace(_KS, order="asc"))
    with pytest.raises(ValidationFailed, match="sort_order"):
        run_search(searcher=s, free_text="invoice", filters={}, limit=2,
                   allowed_account_ids=[1], user_id=99, sort_order="desc",
                   cursor=raw)
    s.search.assert_not_called()


def test_a_pool_cursor_with_an_ascending_order_is_refused() -> None:
    """A pool cursor cannot be carried into an ascending walk.

    The refusal this exercises is the rank+asc guard, **not** the pool
    mismatch check: with no ``sort`` stated the plan resolves to rank, so
    the request is turned away before ``get_pool_metadata`` is called and
    the metadata below is never read. That shadowing is structural — a
    stated order of "asc" needs ``sort="date"`` to clear the rank guard,
    and the pool guard's *sort* half then rejects that against a rank-built
    pool. ``reject_pool_sort_mismatch``'s order half is therefore
    unreachable from here and is tested directly in
    ``test_api_search_cursor_direction.py``.
    """
    s = _searcher()
    s.get_pool_metadata.return_value = PoolMetadata(
        candidates_per_arm=50, page_size=2, rerank_pool_size=100, pool_size=10,
        sort="rank", sort_order="desc",
    )
    with pytest.raises(ValidationFailed, match="sort_order"):
        run_search(searcher=s, free_text="invoice", filters={}, limit=2,
                   allowed_account_ids=[1], user_id=99, sort_order="asc",
                   cursor="tok-1:2")
    s.get_pool_metadata.assert_not_called()


def test_a_stated_order_reaches_the_pool_guard_and_is_checked_against_it() -> None:
    """``run_search`` must hand the caller's stated order to the pool guard.

    The test above proves only that the *ascending* case never gets there,
    so nothing drove ``_check_pool_sort``'s ``requested_sort_order``
    argument end to end: every other pool-cursor test states no order at
    all, which makes ``_reject_order_mismatch`` a no-op by construction.
    Dropping that argument — passing ``None`` where the caller's value
    belongs — therefore left the whole suite green while silently
    un-checking one half of the pool guard.

    ``sort_order="desc"`` is the only stated order that reaches the guard
    (``"asc"`` is shadowed by the rank+asc refusal), so the pool it is
    checked against has to be an ascending one for the comparison to have
    an answer. Such a pool cannot arise in production — which is the point:
    a mocked one is the only way to observe that the argument arrived.
    """
    s = _searcher()
    s.get_pool_metadata.return_value = PoolMetadata(
        candidates_per_arm=50, page_size=2, rerank_pool_size=100, pool_size=10,
        sort="rank", sort_order="asc",
    )
    with pytest.raises(ValidationFailed, match="sort_order"):
        run_search(searcher=s, free_text="invoice", filters={}, limit=2,
                   allowed_account_ids=[1], user_id=99, sort_order="desc",
                   cursor="tok-1:2")
    s.get_pool_metadata.assert_called_once()


def test_an_order_matching_the_pool_continues_instead_of_refusing() -> None:
    """The other half: agreeing with the pool is not an error.

    Guards are only as trustworthy as their negative case. Without this,
    ``_check_pool_sort`` could reject every stated order and the test above
    would still pass.
    """
    s = _searcher()
    s.get_pool_metadata.return_value = PoolMetadata(
        candidates_per_arm=50, page_size=2, rerank_pool_size=100, pool_size=10,
        sort="rank", sort_order="desc",
    )
    out = run_search(searcher=s, free_text="invoice", filters={}, limit=2,
                     allowed_account_ids=[1], user_id=99, sort_order="desc",
                     cursor="tok-1:2")
    s.get_pool_metadata.assert_called_once()
    s.continue_page.assert_called_once()
    assert out["results"] == []


def test_an_ascending_search_pages_end_to_end_through_run_search() -> None:
    """The headline round trip: fresh ascending page, then its cursor alone.

    Everything else here checks one leg. This is the shape a client
    actually pages in — take ``next_cursor``, send it back, state nothing
    else — and the one the missing direction used to break: the unstated
    ``sort_order`` resolved to "desc" and page 2 walked back the way it
    came, which looks like a continuation until the results repeat.
    """
    s = _searcher(_page(next_keyset=_KS_ASC))
    first = run_search(searcher=s, free_text="invoice", filters={}, limit=2,
                       allowed_account_ids=[1], user_id=99, sort="date",
                       sort_order="asc")
    assert keyset_order(first["next_cursor"]) == "asc"

    second_ks = KeysetCursor(ts=datetime(2026, 5, 22, tzinfo=timezone.utc),
                             id=200, order="asc")
    s.search.return_value = _page(next_keyset=second_ks)
    second = run_search(searcher=s, free_text="invoice", filters={}, limit=2,
                        allowed_account_ids=[1], user_id=99,
                        cursor=first["next_cursor"])

    _, kwargs = s.search.call_args
    assert kwargs.get("sort") == "date"
    assert kwargs.get("sort_order") == "asc", (
        "the cursor was the only statement about ordering and it was ignored: "
        "page 2 walks back over page 1"
    )
    assert kwargs.get("keyset_cursor") == _KS_ASC
    assert keyset_order(second["next_cursor"]) == "asc", (
        "the walk continued ascending but minted a descending cursor, so "
        "page 3 would reverse"
    )


def test_the_fresh_rank_asc_refusal_names_the_remedy_that_works() -> None:
    """No cursor was sent, so the remedy must not talk about one.

    This is the commonest way to reach the refusal — ``sort_order="asc"``
    with no ``sort``, which resolves to rank. Telling that caller to "run a
    fresh search" and that "a cursor cannot be carried over" describes a
    request they did not make; the fix is one field. The wording is
    deliberately the same as ``Searcher.search``'s own guard on the same
    condition: two guards for one condition disagreeing about the remedy is
    the drift this pins.
    """
    s = _searcher()
    with pytest.raises(ValidationFailed) as exc:
        run_search(searcher=s, free_text="invoice", filters={}, limit=2,
                   allowed_account_ids=[1], user_id=99, sort_order="asc")
    message = str(exc.value)
    assert "pass sort='date' for oldest-first" in message, message
    assert "cursor" not in message, message


def test_the_pool_cursor_rank_asc_refusal_says_the_cursor_cannot_carry_over() -> None:
    """With a pool cursor in hand the short remedy is actively wrong.

    ``sort='date'`` alongside a rank-built pool cursor is a *different*
    400, so this caller's fix is to start over rather than to add a field.
    """
    s = _searcher()
    with pytest.raises(ValidationFailed) as exc:
        run_search(searcher=s, free_text="invoice", filters={}, limit=2,
                   allowed_account_ids=[1], user_id=99, sort_order="asc",
                   cursor="tok-1:2")
    message = str(exc.value)
    assert "cannot be carried over" in message, message
