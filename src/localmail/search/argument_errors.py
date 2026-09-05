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

That distinction used to rest on an obligation on *transports* rather than on
a property of the boundary: ``run_search`` type-hinted both axes and checked
neither, so every transport reaching it had to declare them as ``Literal``s or
inherit an unhandled 500 — ``serve/app.py`` registers a handler for
``APIError`` only. HTTP (``serve/routes/search.py``) and MCP
(``mcp/server.py``) both do declare them, so the premise held; what did not
hold is that a **third** consumer inherited the 500 with nothing failing at
review time, and that the empty-ACL short-circuit returned before the Searcher
ever validated — answering ``sort="Date"`` from a grant-nothing caller with a
**200 and an empty page**.

**#348 closed both halves in ``run_search``, and the membership checks stay
outside this family** (operator decision, recorded here because #348 asked for
it). Three reasons, in the order they bind:

* The type-vs-cross-argument line above is unchanged by the fix. ``sort="Date"``
  is unspellable at every declared boundary; ``sort="rank"`` on a textless
  query is spellable at all of them.
* Admitting them would give the family a member **no wire caller can reach**,
  since the boundary now refuses them ahead of the Searcher — claiming an
  audience it does not have, which is the defect ``SortOrderNotApplicable``'s
  docstring was corrected for.
* They would fall under the pre-IO contract below for no gain, and the
  ``_family()``-derived pins would start asserting 400-mapping for two
  exceptions no api boundary maps.

The rule itself is the pure ``sort_axes.sort_membership_error``, shared by
``run_search`` and ``Searcher.search`` so one rule cannot be worded two ways —
and ordered **ahead of every guard here at both layers**, because a value that
is not a value cannot meaningfully contradict a cursor or a query.
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

    #: What the refusal is *about* — a bare noun (``"cursor"``), never the
    #: joined form. It lives on the exception rather than on the branch that
    #: catches it, because it describes the cause, not the request shape:
    #: ``run_search``'s keyset branch used to prefix ``cursor:`` onto
    #: everything it caught, so a ``sort_order`` refusal on a request whose
    #: cursor was fine would have read ``cursor: sort_order='asc' is not
    #: applicable…`` — a category error, and one that was unreachable, which
    #: is exactly when such wording rots (#331). Derived, not written beside
    #: the raise, for the reason ``version_report``'s severity word is (#302).
    #:
    #: The default is empty rather than mandatory, unlike ``VersionSource``'s
    #: forced remedy: a member that forgets to set one loses a word of
    #: context, where a member that inherits a *wrong* one makes a false
    #: claim. This default fails in the harmless direction.
    label: str = ""

    #: The join, owned here and not by the member (#350). It used to live
    #: inside the value (``wire_prefix = "cursor: "``), which made the
    #: correctness of every rendering a property of a member's *spelling*,
    #: invisible at the join site.
    #:
    #: **The exposure was to a new member, not to the existing two** — #350
    #: says "nothing catches it", and that is wrong as written; measured,
    #: dropping the space from a shipped member failed three tests, one of
    #: them a literal ``== "cursor: "``. What nothing checked was the
    #: **form**: a fifth member spelled ``"filter"`` and one spelled
    #: ``"filter: "`` failed the *same two* enumeration tests, which fail
    #: because the member is new and say nothing about its spelling — so the
    #: maintainer who updates them to admit it (as they must) gets no signal
    #: at all. And that member is spelled wrong by default: you have to
    #: notice a trailing space in a sibling to copy it. Owned here, the
    #: missing-space form is unspellable rather than merely untested.
    _SEPARATOR = ": "

    def wire_message(self) -> str:
        """The message a boundary puts on the wire, label and all.

        On the type rather than at each boundary — ``api/search.py`` wrote
        the f-string twice and #305's CLI widening is the third — for the
        reason ``APIError.to_problem`` is: the data being on the type while
        the rule for using it is not is how two consumers come to disagree.

        A blank message renders the **label alone**, never a dangling
        ``"cursor: "``, which reads as a detail withheld. Unreachable today
        (all five raise sites pass a message), and decided here rather than
        left to rot for that very reason — the call
        ``ResolvedVersion.__post_init__`` makes, which *rejects* a blank
        detail; here the refusal is still worth delivering, so the label
        stands alone instead. Tested on ``.strip()`` rather than truthiness
        because ``"   "`` is truthy and would sail through a bare ``or`` into
        the dangling form — ``version_report.unreadable``'s fallback learned
        the same thing.
        """
        detail = str(self)
        if not self.label:
            return detail
        if not detail.strip():
            return self.label
        return f"{self.label}{self._SEPARATOR}{detail}"


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

    label = "cursor"


class KeysetCursorUnusable(SearchArgumentRefused):
    """A ``keyset_cursor`` reached a retrieval branch that will not read it.

    Raised for two distinct shapes: a **text**-walk cursor presented with no
    free text to rebuild its FTS predicate from (#326), and any keyset
    cursor reaching the hybrid pool branch, which does not read one (#308).

    The first of those outranks ``SortNotApplicable`` — a cursor problem is
    the more specific diagnosis, which is the precedence the api boundary
    has always applied and ``Searcher.search`` now applies too (#344).
    """

    label = "cursor"
