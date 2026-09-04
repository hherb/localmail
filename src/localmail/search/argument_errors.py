# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The search arguments the Searcher refuses, as one family (#344).

``Searcher.search`` validates what the caller *stated* before it opens a
connection, and every such refusal has the same meaning to every transport:
**the caller asked for something that cannot be served; answer 400**. They
were four unrelated ``ValueError`` subclasses, so ``api/search.py`` had to
enumerate them by name — in two different tuples on two branches, each
carrying a comment arguing which members were unreachable there.

That is the shape this codebase repeatedly names as a defect: a rule
enforced by everyone remembering rather than by construction. A fifth guard
added without widening a tuple is an operator-facing **500**, because
``serve.app`` registers a handler for ``APIError`` only — and it is not
hypothetical, since #342 shipped with ``SortNotApplicable`` missing from the
keyset tuple. Its safety rested on ``KEYSET_SORT is TEXTLESS_SORT``, an
aliasing decision made in a different module — the two have never been
independently declared literals, since #342 introduced the alias and the
hole in the same commit.

``SearchArgumentRefused`` is the family, and both boundaries catch *it*. The
named subclasses stay, because the point is not to collapse the diagnoses —
each still says a different thing to the caller — but to let api/ catch
precisely this family without also catching the ``ValueError`` psycopg,
``datetime`` and the embedding backends raise, which would relabel a real
outage as a caller error.

They live here rather than in ``searcher.py`` for the reason ``sort_axes``
and ``keyset_walk`` do: the family is the contract *between* the Searcher
and every boundary that maps it, it is what a new guard must join, and
stating that rule needs somewhere to state it. ``searcher.py`` re-exports the
four, so ``from localmail.search.searcher import KeysetCursorUnusable`` keeps
resolving; the base is new and has no legacy path, so boundaries import it
from here.

**Not every refusal belongs here.** ``Searcher.search``'s membership checks
on ``sort``/``sort_order`` raise a plain ``ValueError`` on purpose, and the
line between them and the family is not *audience* — both are raised at
library callers, as ``SortNotApplicable`` below says of itself. It is that a
membership error is a **type** error a well-typed caller cannot make, where
every member here is a **cross-argument** error a perfectly well-typed caller
makes routinely: ``sort="Date"`` is unspellable at every declared boundary,
while ``sort="rank"`` on a textless query is spellable at all of them.

That distinction rests on an obligation on *transports*, not on a property of
the value: ``run_search`` type-hints both axes and checks neither at runtime,
so every transport reaching it must declare them as ``Literal``s or inherit a
500 with nothing failing at review time. HTTP (``serve/routes/search.py``) and
MCP (``mcp/server.py``) both do; #348 tracks moving the check into
``run_search`` so a third consumer cannot forget, and revisits whether these
two then join the family.
"""
from __future__ import annotations


class SearchArgumentRefused(ValueError):
    """A caller-stated search argument the Searcher will not honour.

    Every member maps to HTTP 400 at the api boundary. Subclass this rather
    than ``ValueError`` for any new guard over a *stated* argument, and the
    boundaries need no edit.

    **A member must be raised before any IO**, and that is a contract rather
    than an accident of where the five current guards sit. Both catch sites
    wrap the whole ``searcher.search(...)`` call — DB, embedding, reranking —
    so a member raised after retrieval began would have a genuine backend
    failure relabelled a caller 400, which is this family's own purpose
    inverted. A refusal detectable only after retrieval does not belong here.
    #349 tracks pinning that the way the 400-mapping is pinned: derived from
    the type, not hand-written per member.

    A ``ValueError`` subclass so nothing that already catches one changes
    behaviour; narrower than ``ValueError`` so a boundary catching this
    family does not also swallow a genuine backend failure.
    """

    #: What the refusal is *about*, prepended by the api boundary to the
    #: message it puts on the wire (#331). It lives on the exception rather
    #: than on the branch that catches it, because it describes the cause,
    #: not the request shape: ``run_search``'s keyset branch used to prefix
    #: ``cursor:`` onto everything it caught, so a ``sort_order`` refusal on
    #: a request whose cursor was fine would have read
    #: ``cursor: sort_order='asc' is not applicable…`` — a category error,
    #: and one that was unreachable, which is exactly when such wording
    #: rots. Derived, not written beside the raise, for the reason
    #: ``version_report``'s severity word is (#302).
    #:
    #: The default is empty rather than mandatory, unlike ``VersionSource``'s
    #: forced remedy: a member that forgets to set one loses a word of
    #: context, where a member that inherits a *wrong* one makes a false
    #: claim. This default fails in the harmless direction.
    #:
    #: **The ``": "`` separator lives in the value, which this type does not
    #: own** — ``wire_prefix = "cursor"`` renders ``cursorthis cursor…`` and
    #: nothing checks the shape, since the tests pin the two literals rather
    #: than the form. #350 tracks moving the join onto the type (a ``label``
    #: plus an owned separator, the ``APIError.to_problem`` shape), which also
    #: settles what a member with no message should render.
    wire_prefix: str = ""


class SortNotApplicable(SearchArgumentRefused):
    """A stated ``sort`` cannot be served by the query it arrived with (#324).

    Today that is ``sort="rank"`` on a query with no free text: the hybrid
    pool has nothing to rank against, so the date-ordered walk has always
    answered such a query — silently, until #322 gave that walk a cursor
    recording ``date`` and the drop became a contradiction the caller could
    see one page later.

    **Its audience is library callers**, plus every wire caller whose query
    the two layers read differently. ``api.run_search`` refuses the same
    shape at its own boundary, ahead of the empty-ACL short-circuit — but
    the two guards read *different strings* (the raw request field there,
    the ACL-composed query here), so this is not merely a backstop; see
    ``run_search``'s catch, which maps it to a 400.

    The CLI is **not** in that audience today: ``localmail search`` has no
    ``--sort`` option and passes none, so this cannot be raised from it. If
    one is ever added, note that ``cli.py``'s ``search`` catches only
    ``RuntimeError``, so this would traceback rather than exit cleanly.
    """


class SortOrderNotApplicable(SearchArgumentRefused):
    """``sort_order="asc"`` was asked for on a sort that cannot serve it.

    Its audience is library callers **and** the api/ layer, and the second
    half of that is a correction (#331). ``run_search`` does refuse rank+asc
    at its own gate first, so the catch beyond it reads like a backstop —
    but since #324 the two ends judge *different strings* (the raw request
    field at the gate, the ACL-composed query in the Searcher), and
    ``parse_query`` is not compositional across an unbalanced quote: ``'"'``
    is textless to the gate and text once an ``account_id:`` token joins it,
    so a ``sort_order="asc"`` the gate cleared against its resolved ``date``
    meets a resolved ``rank`` here. The catch is live.

    The CLI is **not** in that audience today, exactly as
    ``SortNotApplicable`` above is not: ``localmail search`` has no
    ``--sort-order`` option and passes none, so ``effective_order`` is always
    ``DEFAULT_SORT_ORDER`` and rank+asc cannot hold. If one is ever added,
    note that ``cli.py``'s ``search`` catches only ``RuntimeError``, so this
    would traceback rather than exit cleanly — widen that catch to
    ``SearchArgumentRefused``, never to bare ``ValueError``. Tracked in
    **#305** with the rest of the ``cli.py`` work (#331, where it began, is
    closed).
    """


class KeysetOrderMismatch(SearchArgumentRefused):
    """A stated ``sort_order`` contradicts the direction its cursor carries.

    Refused rather than resolved either way, because both resolutions are
    silent: honouring the stated order walks the cursor's position in a
    direction it was not minted for, and honouring the cursor ignores a
    parameter the caller wrote down (#308, #312).
    """

    wire_prefix = "cursor: "


class KeysetCursorUnusable(SearchArgumentRefused):
    """A ``keyset_cursor`` reached a retrieval branch that will not read it.

    Raised for two distinct shapes: a **text**-walk cursor presented with no
    free text to rebuild its FTS predicate from (#326), and any keyset
    cursor reaching the hybrid pool branch, which does not read one (#308).

    The first of those outranks ``SortNotApplicable`` — a cursor problem is
    the more specific diagnosis, which is the precedence the api boundary
    has always applied and ``Searcher.search`` now applies too (#344).
    """

    wire_prefix = "cursor: "
