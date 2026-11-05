"""The /v1/search response: one type, one constructor (#358).

``run_search`` used to return ``dict[str, Any]`` written out by hand at its
two return sites, so the wire contract had no type anywhere and a new field
was a bare string key. Worse, a test fake standing in for a ``SearchPage``
with an attribute left unset *serialised* rather than raising: a
``MagicMock`` attribute renders as ``{}`` or ``[]`` depending on the path,
never as an error. #345 (``sort_applied``) and #353 (``rankable``) both put
that garbage on the wire with the suite green.

So every response is built here. ``SearchResponse`` is the contract (HTTP and
MCP both hand it out verbatim), and ``search_response`` checks every scalar
field, so a fake that leaves one unset fails in every test that drives
``run_search`` rather than only in the ones that remember to assert on it.
The ``results`` list is not checked: each hit is a dict built by
``_to_api_result``, and a fake *hit* is ``_assert_wire_hit_fields``' job.

The derived fields are decided once here too: ``total_estimate`` is always
``None`` (#175, never ``0``) and ``rewrite_skipped`` follows the status
(#176), so no return site can disagree with another about either.
"""

from __future__ import annotations

from typing import Any, TypedDict, get_args

from localmail.search.rewrite_status import (
    RewriteNoteCode,
    RewriteStatus,
    rewrite_skipped_for_status,
)
from localmail.search.sort_axes import SortMode, ordering_pair_error

_SORT_MODES = get_args(SortMode)
_REWRITE_STATUSES = get_args(RewriteStatus)
_REWRITE_NOTE_CODES = get_args(RewriteNoteCode)


class SearchResponse(TypedDict):
    """One /v1/search (and MCP ``search``) response."""

    results: list[dict[str, Any]]
    next_cursor: str | None
    total_estimate: None
    took_ms: float
    sort_applied: SortMode
    rankable: bool
    rewrite_skipped: bool
    rewrite_status: RewriteStatus
    rewrite_note: str | None
    rewrite_note_code: RewriteNoteCode | None


def _require_optional_str(name: str, value: Any) -> None:
    if value is not None and not isinstance(value, str):
        raise TypeError(f"{name} must be a str or None, got {value!r}")


def search_response(
    *,
    results: list[dict[str, Any]],
    next_cursor: str | None,
    took_ms: float,
    sort_applied: SortMode,
    rankable: bool,
    rewrite_status: RewriteStatus,
    rewrite_note: str | None,
    rewrite_note_code: RewriteNoteCode | None,
) -> SearchResponse:
    """Build the response, refusing a value no real page can carry.

    Raises ``TypeError`` for an ordering, rewrite status or note code outside
    its ``Literal``, a ``rankable`` that is not exactly a ``bool`` (``1`` and
    ``"false"`` are the coercions a client would misread), a ``took_ms`` that
    is not a number, or a ``next_cursor`` / ``rewrite_note`` that is neither a
    string nor ``None``. Raises ``ValueError`` for ``sort_applied="rank"``
    beside ``rankable=False``: ranking ran on a query with nothing to rank,
    which the GUI renders as a checked *and* disabled Relevance radio (#355
    found it reachable; this is the wire half of #356). A raise here is a bug
    in whatever produced the page, so it is loud rather than repaired.
    """
    if sort_applied not in _SORT_MODES:
        raise TypeError(f"sort_applied must be one of {_SORT_MODES}, "
                        f"got {sort_applied!r}")
    if type(rankable) is not bool:
        raise TypeError(f"rankable must be a bool, got {rankable!r}")
    if isinstance(took_ms, bool) or not isinstance(took_ms, (int, float)):
        raise TypeError(f"took_ms must be a number, got {took_ms!r}")
    if rewrite_status not in _REWRITE_STATUSES:
        raise TypeError(f"rewrite_status must be one of {_REWRITE_STATUSES}, "
                        f"got {rewrite_status!r}")
    if rewrite_note_code is not None and rewrite_note_code not in _REWRITE_NOTE_CODES:
        raise TypeError(f"rewrite_note_code must be one of "
                        f"{_REWRITE_NOTE_CODES} or None, got {rewrite_note_code!r}")
    _require_optional_str("rewrite_note", rewrite_note)
    _require_optional_str("next_cursor", next_cursor)
    pair_error = ordering_pair_error(sort=sort_applied, rankable=rankable)
    if pair_error is not None:
        raise ValueError(pair_error)
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
