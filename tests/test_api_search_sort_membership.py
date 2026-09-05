# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""``run_search`` validates both sort axes itself (#348).

``Searcher.search`` checks membership on ``sort``/``sort_order`` and raises a
plain ``ValueError``, deliberately outside the ``SearchArgumentRefused``
family: a membership error is a **type** error a well-typed caller cannot
make, where every family member is a **cross-argument** error a well-typed
caller makes routinely.

That reasoning is sound and its *premise* was an obligation on transports
rather than a property of the boundary. ``run_search`` type-hints both axes
and checked neither, so the guarantee lived in each caller's schema:

* HTTP (``serve/routes/search.py``) declares ``Literal``s, so pydantic
  answers 422 before ``run_search``.
* MCP (``mcp/server.py``) declares the same, so ``func_metadata`` rejects it.

Both hold today. What did not hold is that a **third** consumer — the
``--sort``/``--sort-order`` flags anticipated in #305, a queue, a new route —
inherits an unhandled 500 with nothing failing at review time, because
``serve/app.py`` registers a handler for ``APIError`` only.

And one half of it was wire-visible already: the empty-ACL short-circuit
returns *before* the Searcher ever validates, so a grant-nothing caller
asking for ``sort="Date"`` was answered **200 with an empty page** —
byte-identical to "you have reached the end of your results". That is the
shape ``resolve_cursor_plan`` and the rank+asc gate are deliberately placed
ahead of that short-circuit to avoid.

The Searcher's own checks stay a plain ``ValueError`` (operator decision,
recorded in ``argument_errors.py``): they now sit behind this gate for every
wire caller, and their remaining audience is library and CLI callers, for
whom a ``ValueError`` on a misspelling is the right answer.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from localmail.api.errors import APIError, ValidationFailed
from localmail.api.search import run_search
from localmail.api.search_cursor import encode_keyset_cursor
from localmail.search.searcher import KeysetCursor

#: Values no declared boundary can spell, but a library caller can.
BAD_SORTS = ["Date", "DATE", "rank ", "relevance", ""]
BAD_ORDERS = ["ASC", "DESC", "ascending", ""]


def _searcher() -> MagicMock:
    """A searcher that fails the test if the refusal did not precede it."""
    searcher = MagicMock()
    searcher.smart_available = False
    searcher.search.side_effect = AssertionError("retrieval must not start")
    return searcher


def _run(**kwargs: Any) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "searcher": _searcher(), "free_text": "invoice", "filters": {},
        "limit": 10, "allowed_account_ids": [1], "user_id": 1,
    }
    return run_search(**{**defaults, **kwargs})


@pytest.mark.parametrize("sort", BAD_SORTS)
def test_an_unknown_sort_is_a_validation_failure_not_a_bare_value_error(
    sort: str,
) -> None:
    """``ValidationFailed`` is an ``APIError``, so it reaches problem+json.

    The assertion that matters is the ``APIError`` one: a plain ``ValueError``
    also "raises", and is exactly the 500 this gate exists to end.
    """
    with pytest.raises(ValidationFailed, match=f"unknown sort {sort!r}") as exc:
        _run(sort=sort)
    assert isinstance(exc.value, APIError)


@pytest.mark.parametrize("sort_order", BAD_ORDERS)
def test_an_unknown_sort_order_is_a_validation_failure(sort_order: str) -> None:
    with pytest.raises(ValidationFailed,
                       match=f"unknown sort_order {sort_order!r}") as exc:
        _run(sort_order=sort_order)
    assert isinstance(exc.value, APIError)


@pytest.mark.parametrize("sort", BAD_SORTS)
def test_an_unknown_sort_is_refused_even_with_an_empty_acl(sort: str) -> None:
    """The wire-visible half: this used to be **200 with an empty page**.

    A contradictory request must not be reported as a completed one, whatever
    the caller was granted — the rule every other gate here already follows.
    """
    with pytest.raises(ValidationFailed, match=f"unknown sort {sort!r}"):
        _run(sort=sort, allowed_account_ids=[])


@pytest.mark.parametrize("sort_order", BAD_ORDERS)
def test_an_unknown_sort_order_is_refused_even_with_an_empty_acl(
    sort_order: str,
) -> None:
    with pytest.raises(ValidationFailed,
                       match=f"unknown sort_order {sort_order!r}"):
        _run(sort_order=sort_order, allowed_account_ids=[])


def test_the_empty_acl_short_circuit_still_answers_a_well_formed_request() -> None:
    """Positive control. A gate that refused everything would satisfy every
    assertion above, including both empty-ACL ones."""
    page = _run(sort="date", sort_order="asc", allowed_account_ids=[])
    assert page["results"] == []
    assert page["next_cursor"] is None


@pytest.mark.parametrize("kwargs", [
    {"sort": "rank"}, {"sort": "date", "sort_order": "asc"},
    {"sort_order": "desc"}, {},
])
def test_stated_and_omitted_valid_axes_still_reach_the_searcher(
    kwargs: dict[str, Any],
) -> None:
    """The other half of the positive control: the gate must not refuse a
    value it is supposed to admit, nor an axis the caller left unstated."""
    with pytest.raises(AssertionError, match="retrieval must not start"):
        _run(**kwargs)


@pytest.mark.parametrize("sort", BAD_SORTS)
def test_an_unknown_sort_outranks_the_cursor_guards(sort: str) -> None:
    """Membership is the more fundamental fault, so it is decided first.

    ``resolve_cursor_plan`` interpolates the offending value into a sentence
    asserting what the *cursor* continues ("this cursor continues a
    date-sorted search ... (got 'Date')"), which sends a caller who made a
    typo to their paging logic. Worse, the sentence is a coincidence of which
    cursor was in hand: the same typo against a rank-built pool cursor is
    reported as a rank/date disagreement, and following that remedy repairs
    the request without the caller ever learning the value was not a value.
    """
    cursor = encode_keyset_cursor(
        KeysetCursor(ts=None, id=5, order="desc", walk="archive"))
    with pytest.raises(ValidationFailed, match=f"unknown sort {sort!r}"):
        _run(sort=sort, cursor=cursor)


@pytest.mark.parametrize("sort_order", BAD_ORDERS)
def test_an_unknown_sort_order_outranks_the_cursor_guards(
    sort_order: str,
) -> None:
    cursor = encode_keyset_cursor(
        KeysetCursor(ts=None, id=5, order="desc", walk="archive"))
    with pytest.raises(ValidationFailed,
                       match=f"unknown sort_order {sort_order!r}"):
        _run(sort_order=sort_order, cursor=cursor)


def test_a_well_formed_axis_contradicting_its_cursor_is_still_a_cursor_problem(
) -> None:
    """The negative control for the precedence above.

    A membership gate placed correctly refuses only values that are not
    values; it must not swallow the cross-argument diagnosis for two
    perfectly well-formed ones, which is the whole of #308.
    """
    cursor = encode_keyset_cursor(
        KeysetCursor(ts=None, id=5, order="desc", walk="archive"))
    with pytest.raises(ValidationFailed, match="cursor: "):
        _run(sort_order="asc", cursor=cursor)
