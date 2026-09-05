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
* Since #348 the boundary refuses them ahead of the Searcher, so family
  membership would be **inert at both catch sites** — the catch could never
  see one. That is the same class of defect ``SortOrderNotApplicable``'s
  docstring was corrected for (#331), an audience claim a docstring cannot
  check for itself; note the correction there ran the *other* way, restoring
  an audience the wording denied.
* They would fall under the pre-IO contract below for no gain.

The rule itself is the pure ``sort_axes.sort_membership_error``, shared by
``run_search`` and ``Searcher.search`` so one rule cannot be worded two ways —
and ordered **ahead of every guard here at both layers**, because a value that
is not a value cannot meaningfully contradict a cursor or a query.
"""
from __future__ import annotations


#: Quoted by ``reject_malformed_label``'s message so the remedy shows the
#: shape it wants. A real member's label rather than an invented one, so it
#: cannot describe a spelling the tree does not actually use.
FAMILY_LABEL_EXAMPLE = "cursor"


def reject_malformed_label(name: str, label: str) -> str:
    """Return ``label``, or raise if it is not a bare noun.

    A **module-level function rather than an inline check** so the rule can
    be driven against a label no member has — the ``reject_empty_diagnostic``
    arrangement (#291). There the indirection was forced, enum machinery
    replacing ``__new__`` after class creation; here ``__init_subclass__`` is
    reachable from a test by writing a subclass, so this is belt-and-braces
    and the naming stays consistent rather than novel.

    Only a **stated** label is judged: the empty default is deliberate and
    #350 argued it correctly — a member that forgets a label loses a word of
    context, where one that inherits a *wrong* one makes a false claim. That
    argument defends the default, not the absence of a check on a value
    someone did write, and the second has no harmless direction: no member
    legitimately wants ``"filter:"``, ``"cursor: "`` or ``"  "``.
    """
    if not label:
        return label
    separator = SearchArgumentRefused._SEPARATOR
    if (label != label.strip() or separator in label
            or not label[-1].isalnum()):
        raise ValueError(
            f"{name}.label must be a bare noun naming the subject "
            f"({FAMILY_LABEL_EXAMPLE!r}), not the joined form: the type owns "
            f"the separator {separator!r}; got {label!r}"
        )
    return label


class SearchArgumentRefused(ValueError):
    """A caller-stated search argument the Searcher will not honour.

    Every member maps to HTTP 400 at the api boundary. Subclass this rather
    than ``ValueError`` for any new guard over a *stated* argument, and the
    boundaries need no edit.

    **A member must be raised before any IO** — part of the contract, not an
    accident of where the current guards sit, and the reason the boundaries
    may catch the whole family at once. Both catch sites wrap the entire
    ``searcher.search(...)`` call — DB, embedding, reranking, and the smart
    rewrite's LLM round trip — so a member raised after retrieval began would
    have a genuine backend failure relabelled a caller 400, which is this
    family's own purpose inverted. **A refusal detectable only after
    retrieval does not belong in this family**; give it its own type and map
    it deliberately.

    Pinned by ``tests/test_searcher_guards_precede_io.py``, which provokes
    every raise site for real and asserts that the pool, the embedding
    backend and the rewriter are all untouched. A member with no provocation
    fails that file rather than being silently skipped — the reverse
    cross-check ``_pool_leaks.pool_constructor_calls`` and
    ``_harness_lock.acceptance_coverage_error`` exist for.

    **The contract was already false when #349 filed it as safe.** That issue
    reports all five raise sites verified pre-IO; the verification used a
    ``pool.connection`` tripwire only, and the #308 hybrid-branch guard sat
    *below* the smart rewrite — pre-pool, post-LLM — so a caller on the smart
    path paid a full round trip to be told their cursor was unusable. Found
    by writing the pin rather than by reading the code, which is the argument
    for deriving it from the type. The guard is hoisted; see its comment in
    ``searcher.py`` for why the move is exactly equivalent.

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
    #: notice a trailing space in a sibling to copy it.
    #:
    #: **Ownership is enforced, not merely declared** (#350 review). #350's
    #: own wording here claimed the missing-space form was "unspellable
    #: rather than merely untested"; it was spellable two ways, both against
    #: a green suite — ``label = "filter:"`` (``": " not in "filter:"``, so
    #: the form assertions pass) and a subclass declaring its own
    #: ``_SEPARATOR`` (those assertions read the *base's*, so the override
    #: defeats rule and test together). One underscore is a convention, not
    #: ownership. ``__init_subclass__`` below is what makes the claim true.
    _SEPARATOR = ": "

    def __init_subclass__(cls, **kwargs: object) -> None:
        """Enforce at class creation what #350 claimed and did not check.

        Two rules, and the second is the one that made the ownership claim
        false. ``label`` must be a bare noun — ``"filter:"`` rendered
        ``filter:: …`` and passed both form assertions in
        ``test_search_argument_errors.py``, since ``": " not in "filter:"``.
        And a subclass may not declare its own ``_SEPARATOR``: both of those
        assertions read the **base's** value, so an override defeated the
        rule and its test together, restoring the exact pre-#350 form.

        A ``TypeError`` for the override and a ``ValueError`` for the label,
        because they are different faults: the first is a subclass reaching
        for something the type owns, the second a bad value. Raised at class
        creation rather than at render time, so a member that gets it wrong
        cannot be imported — the ``VersionSource`` call (#291), which this
        family already cites for its *empty*-payload rule.
        """
        super().__init_subclass__(**kwargs)  # type: ignore[arg-type]
        if "_SEPARATOR" in cls.__dict__:
            raise TypeError(
                f"{cls.__name__} declares its own separator; "
                f"{SearchArgumentRefused.__name__} owns the join so every "
                "member renders alike — state only `label`"
            )
        reject_malformed_label(cls.__name__, cls.label)

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
        stands alone instead. Blankness is judged on ``.strip()`` rather than
        truthiness because ``"   "`` is truthy and would sail through a bare
        ``or`` into the dangling form — ``version_report.unreadable``'s
        fallback learned the same thing.

        **The result is never empty** (#350 review). An *unlabelled* member
        with a blank message returned ``""``, so ``run_search`` raised
        ``ValidationFailed("")`` and the wire carried problem+json with an
        empty ``detail`` — the failure this method exists to avoid, one
        branch over and worse, since the dangling form at least named a
        subject. It falls back to the class name, which says which guard
        fired. ``label`` needs no ``.strip()`` of its own: since #350's
        review ``__init_subclass__`` admits only ``""`` or a stripped noun.
        """
        detail = str(self)
        if not detail.strip():
            # Never an empty string: `run_search` would raise
            # `ValidationFailed("")` and the wire would carry problem+json
            # with an empty `detail` — strictly worse than the dangling
            # `"cursor: "` this method exists to avoid, which at least named
            # a subject. A labelled member still renders its label alone,
            # which is #350's decision and is not revisited here; only the
            # unlabelled case, which had no floor at all, gains one.
            return self.label or type(self).__name__
        if not self.label:
            return detail
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
