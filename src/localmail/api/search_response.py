"""The /v1/search response: one type, one constructor (#358).

``run_search`` used to return ``dict[str, Any]`` written out by hand at each
of its return sites, so the wire contract had no type anywhere and a new
field was a bare string key. Worse, a test fake standing in for a
``SearchPage`` with an attribute left unset *serialised* rather than raising:
a ``MagicMock`` attribute renders as ``{}`` or ``[]`` depending on the path,
never as an error. #345 (``sort_applied``) and #353 (``rankable``) both put
that garbage on the wire with the suite green.

So every response is built here. ``SearchResponse`` is the contract (HTTP and
MCP both hand it out verbatim), and ``search_response`` checks the values a
fake launders, so the trap fails in every test that drives ``run_search``
rather than only in the ones that remember to assert on it. The ``results``
list is not checked: each hit is built by ``_to_api_result`` from real
attributes, which is where ``_assert_wire_hit_fields`` already looks.

The derived fields are decided once here too: ``total_estimate`` is always
``None`` (#175, never ``0``) and ``rewrite_skipped`` follows the status
(#176), so no return site can disagree with another about either.
"""

from __future__ import annotations

from typing import Any, TypedDict, get_args

from localmail.search.rewrite_status import rewrite_skipped_for_status
from localmail.search.sort_axes import SortMode

_SORT_MODES = get_args(SortMode)


class SearchResponse(TypedDict):
    """One /v1/search (and MCP ``search``) response."""

    results: list[dict[str, Any]]
    next_cursor: str | None
    total_estimate: None
    took_ms: float
    sort_applied: SortMode
    rankable: bool
    rewrite_skipped: bool
    rewrite_status: str
    rewrite_note: str | None
    rewrite_note_code: str | None


def search_response(
    *,
    results: list[dict[str, Any]],
    next_cursor: str | None,
    took_ms: float,
    sort_applied: SortMode,
    rankable: bool,
    rewrite_status: str,
    rewrite_note: str | None,
    rewrite_note_code: str | None,
) -> SearchResponse:
    """Build the response, refusing a value no real page can carry.

    Raises ``TypeError`` for an ordering that is not a ``SortMode``, a
    ``rankable`` that is not exactly a ``bool`` (``1`` and ``"false"`` are
    the coercions a client would misread), or a ``took_ms`` that is not a
    number. Raises ``ValueError`` for ``sort_applied="rank"`` beside
    ``rankable=False``: ranking ran on a query with nothing to rank, which
    the GUI renders as a checked *and* disabled Relevance radio (#355 found
    it reachable; this is the wire half of #356). A raise here is a bug in
    whatever produced the page, so it is loud rather than repaired.
    """
    if sort_applied not in _SORT_MODES:
        raise TypeError(f"sort_applied must be one of {_SORT_MODES}, "
                        f"got {sort_applied!r}")
    if type(rankable) is not bool:
        raise TypeError(f"rankable must be a bool, got {rankable!r}")
    if isinstance(took_ms, bool) or not isinstance(took_ms, (int, float)):
        raise TypeError(f"took_ms must be a number, got {took_ms!r}")
    if sort_applied == "rank" and not rankable:
        raise ValueError("sort_applied='rank' beside rankable=False: "
                         "nothing could have been ranked")
    return {
        "results": results,
        "next_cursor": next_cursor,
        "total_estimate": None,
        "took_ms": float(took_ms),
        "sort_applied": sort_applied,
        "rankable": rankable,
        "rewrite_skipped": rewrite_skipped_for_status(rewrite_status),
        "rewrite_status": rewrite_status,
        "rewrite_note": rewrite_note,
        "rewrite_note_code": rewrite_note_code,
    }
