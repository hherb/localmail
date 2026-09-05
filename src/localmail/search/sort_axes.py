# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The two ordering axes a search request states, and how they resolve.

``sort`` picks *what* orders the results; ``sort_order`` picks *which way*
that ordering runs. They are orthogonal — adding ``date_asc``-style members
to ``sort`` was rejected because a third ordering criterion would double the
enum again.

**Both defaults live beside the type they range over**, which is #312's
rule: ``Searcher.search`` and ``api.search_cursor`` each resolve an
unstated value, and two layers resolving "unstated" from two literals is
the drift itself. Since #324 the resolution is more than a default —
``resolve_sort`` reads the query too — which makes co-locating it here
load-bearing rather than tidy: a layer that resolved from ``DEFAULT_SORT``
alone would disagree with the branch that serves the request.

They sit in their own module rather than in ``searcher.py`` because
``date_keyset.py`` — which ``searcher.py`` imports — needs ``SortOrder``
at runtime for its ORDER BY completeness check, and defining it in both
places is the same drift one level down. The co-location argument is
unchanged; only the address is. ``searcher.py`` imports these names, so
``from localmail.search.searcher import SortMode`` keeps resolving.
"""
from __future__ import annotations

from typing import Literal, get_args

from localmail.search.keyset_walk import walk_for_text

SortMode = Literal["rank", "date"]

#: The sort a caller gets when it states none *and* the query has free text
#: to rank against. ``resolve_sort`` is the whole rule; this is one arm of
#: it, kept named because it is also the wire-documented default.
DEFAULT_SORT: SortMode = "rank"

#: The sort a query with no free text is served by, whatever the caller
#: states. Not a policy choice: the hybrid pool has nothing to rank against
#: — the lexical arms early-return with no terms, and the vector arms rank
#: by distance to the embedding of the empty string — so the date-ordered
#: keyset walk is the only branch that can answer such a query at all.
TEXTLESS_SORT: SortMode = "date"

SortOrder = Literal["asc", "desc"]

#: The direction a caller gets when it states none.
DEFAULT_SORT_ORDER: SortOrder = "desc"


def resolve_sort(*, requested: SortMode | None, free_text: str) -> SortMode:
    """The sort that will actually serve this request.

    ``free_text`` must already be ``parse_query(...).free_text``: the filter
    operators are lifted out by then, so ``subject:invoice`` — a non-empty
    *request field* — correctly reads as textless. The classification is
    ``keyset_walk.walk_for_text``, the same one authority the cursor stamp
    asks. The retrieval branch no longer asks it directly: it tests the sort
    this function resolved, so it cannot disagree with the prediction rather
    than merely happening to agree with it.

    A textless query resolves to ``TEXTLESS_SORT`` **whatever was
    requested**, including a ``"rank"`` that ``sort_applicability_error``
    refuses. That is deliberate: this function answers "what will run", not
    "what is allowed". Raising here instead would put the refusal's wording
    inside a resolver called from two layers that need two different
    exception types (``ValidationFailed`` at the api boundary, a named
    ``ValueError`` inside the Searcher) — the split
    ``keyset_walk.keyset_walk_error`` already avoids.
    """
    if walk_for_text(free_text) == "archive":
        return TEXTLESS_SORT
    return DEFAULT_SORT if requested is None else requested


def sort_applicability_error(
    *, requested: SortMode | None, free_text: str,
) -> str | None:
    """Why a *stated* ``sort`` will not be honoured for this query, or ``None``.

    Shaped like ``keyset_walk.keyset_walk_error`` and
    ``account_names.account_name_error``: a message, or ``None``, with the
    caller deciding what an error *is* — so the wording cannot drift between
    the api boundary and the Searcher.

    Only a stated sort is judged. An **unstated** one is not a claim the
    server can contradict, and refusing it would break the documented way to
    page (omit ``sort``, let the cursor carry the ordering) as well as every
    filter-only search the GUI issues; ``resolve_sort`` answers those with
    the branch that runs.
    """
    if requested is None or walk_for_text(free_text) == "text":
        return None
    if requested == TEXTLESS_SORT:
        return None
    return (
        f"sort={requested!r} is not applicable to a query with no free text; "
        f"pass sort={TEXTLESS_SORT!r} or omit sort. Relevance has nothing to "
        "rank against here — filter operators alone (subject:, from:, lang:, "
        "has:attachment) leave no terms — so such a query is always answered "
        "date-ordered."
    )


def sort_membership_error(
    *, sort: str | None, sort_order: str | None,
) -> str | None:
    """Why either axis is not a value of its own type, or ``None``.

    The vocabulary check, and the most fundamental of the three rules here:
    ``resolve_sort`` says what a value *means* and
    ``sort_applicability_error`` says whether this query can serve it, and
    both are nonsense questions about a value that is not one. So callers
    ask this first — see the precedence note on each of them.

    ``None`` means "unstated, nothing to check" for either axis, so a caller
    passes whichever it actually means: ``Searcher.search`` checks ``sort``
    as *stated* (since #324 a textless query resolves to ``TEXTLESS_SORT``
    whatever arrived, so a misspelling would be swallowed on exactly the
    branch that used to swallow it) and ``sort_order`` as *resolved* (the
    resolution is either the caller's own value or a module constant).

    Shaped like its two siblings — a message, or ``None`` — so the caller
    decides what an error *is*: ``ValidationFailed`` at the api boundary
    (#348), a plain ``ValueError`` inside the Searcher. That split is why
    the rule is stated once here rather than inlined at each: the two
    layers must not word one rule differently, which is what
    ``run_search`` inheriting an unhandled 500 amounted to.

    Deliberately **not** a ``SearchArgumentRefused``: a membership error is
    a *type* error a well-typed caller cannot make, where every member of
    that family is a *cross-argument* error a well-typed caller makes
    routinely. See ``argument_errors``' module docstring, which records the
    decision this function's existence prompted.
    """
    if sort is not None and sort not in get_args(SortMode):
        return (f"unknown sort {sort!r}; expected one of "
                f"{sorted(get_args(SortMode))}")
    if sort_order is not None and sort_order not in get_args(SortOrder):
        return (f"unknown sort_order {sort_order!r}; expected one of "
                f"{sorted(get_args(SortOrder))}")
    return None
