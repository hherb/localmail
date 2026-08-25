# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The date-ordered keyset walk's SQL rules — pure, no IO.

``Searcher._date_keyset_search`` is the only caller. Everything that
decides *how* that walk is spelled lives here: the ordering per direction,
the cursor predicate, the top-up predicate that reaches the undated block,
and the one row-emitting template all of them are composed into.

Extracted from ``searcher.py`` when #323 made the walk a two-query
operation. That is the #77 convention applied one module over: a second
inline copy of the SELECT/FROM/ORDER BY is exactly the duplicate
``api.browse.compose_browse_sql`` exists to prevent on the browse path,
and this walk now needs the same shape twice in the same response.

**The two directions are not mirror images in shape, only in effect**, and
the asymmetry is load-bearing rather than an oversight — see
``keyset_clause``.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, get_args

from localmail.search.sort_axes import SortOrder

if TYPE_CHECKING:  # pragma: no cover - typing only
    from localmail.search.searcher import KeysetCursor

#: The sort key, matching ``messages_recent_idx``'s indexed expression
#: exactly. The index is on ``COALESCE(internal_date, date_sent)``; any
#: divergence here — a swapped argument order, an added ``::timestamptz``
#: — costs the index and full-sorts the archive on every page.
DATE_EXPR_SQL = "COALESCE(m.internal_date, m.date_sent)"

#: One ORDER BY per direction, written exactly once.
#:
#: ``ASC NULLS FIRST`` because that is the exact reverse of
#: ``messages_recent_idx`` (``… DESC NULLS LAST, id DESC``) and is served
#: by a backward index scan. Measured on the live 128k archive: 44 buffers
#: against 33,372 for the ``ASC NULLS LAST`` spelling, which full-sorts —
#: and an ``IS NOT NULL`` restriction does not rescue it. Do not
#: "normalise" these to NULLS LAST.
#:
#: Keyed on ``SortOrder`` rather than ``str`` so mypy refuses a wrong
#: literal at the call site. That is the static half only, and CI runs no
#: mypy step — so the value mypy cannot see (a library caller passing
#: ``"ASC"``) is caught at runtime instead, by the explicit membership check
#: against *this* table in **both** ``compose_date_keyset_sql`` and
#: ``keyset_clause``. Each is independently reachable — page 1 has no cursor
#: and so never calls ``keyset_clause`` — which is why the check is written
#: twice rather than being a duplicate one of them could drop. It used to be
#: caught by the
#: ``KeyError`` from the lookup, which worked only because the lookup
#: happened to be assembled after the predicate: ``"ASC"`` fell through the
#: ``== "desc"`` test into the *ascending* predicate first, and nothing but
#: statement order stopped that pairing serving a walk in the direction
#: nobody asked for.
DATE_ORDER_BY_SQL: dict[SortOrder, str] = {
    "desc": (f"ORDER BY {DATE_EXPR_SQL} DESC NULLS LAST, m.id DESC"),
    "asc": (f"ORDER BY {DATE_EXPR_SQL} ASC NULLS FIRST, m.id ASC"),
}

#: Every direction must have an ORDER BY, checked at import.
#:
#: mypy checks the *lookup* against the Literal; nothing checked that the
#: table covers it, so adding a third direction type-checked, imported, and
#: failed at runtime on the first query using it. CI runs only ``pytest``
#: (no mypy step, no ruff step), which makes the static half of that
#: reasoning unenforced in practice — so the check is a runtime one, in the
#: ``reject_empty_diagnostic`` / ``reject_empty_wire_name`` shape: a
#: mistake that cannot reach a query is better than one that reaches it
#: loudly.
#: Both differences are reported. ``!=`` also fires when the table carries an
#: *extra* key — a typo, or a row left behind by a rename — and a message
#: naming only what is missing printed an empty list under the word
#: "missing" for exactly that case.
if set(DATE_ORDER_BY_SQL) != set(get_args(SortOrder)):
    raise RuntimeError(
        "every SortOrder needs an ORDER BY: missing="
        f"{sorted(set(get_args(SortOrder)) - set(DATE_ORDER_BY_SQL))} "
        f"unexpected={sorted(set(DATE_ORDER_BY_SQL) - set(get_args(SortOrder)))}"
    )
#: Coverage is not content: a copy-paste giving both directions the same
#: ORDER BY passes the check above, and the emitter's own test cannot catch
#: it (it asserts ``DATE_ORDER_BY_SQL[order] in sql``, derived from this
#: table). The plan test would, at the cost of a database.
if len(set(DATE_ORDER_BY_SQL.values())) != len(DATE_ORDER_BY_SQL):
    raise RuntimeError("two SortOrders share one ORDER BY")

#: The top-up predicate: the undated block, with no cursor bound (#323).
#:
#: A bare constant rather than a mode flag on ``keyset_clause``.
#: ``api.browse.build_where`` spells the equivalent as
#: ``null_tail_only=True`` and has to ``raise`` when a cursor is passed
#: alongside it; here the top-up simply uses this *instead of* a cursor
#: predicate, so the same invariant costs no runtime guard. It carries no
#: parameters, which is what makes that substitution safe.
UNDATED_TAIL_ONLY_SQL = f" AND {DATE_EXPR_SQL} IS NULL "

#: The one row-emitting shape, shared by the cursor query and the top-up.
#:
#: Two placeholders: ``{where}`` is the whole WHERE body (match clause,
#: cursor or top-up predicate, structured filters) and ``{order_by}`` comes
#: from ``DATE_ORDER_BY_SQL``. The projection is deliberately not an
#: index-only one — the walk needs the header columns, and a narrower
#: SELECT would let the planner consider a plan the real query can never
#: have, which is what ``tests/test_searcher_sort_order_plan.py`` mirrors
#: it for.
ROW_SQL_TEMPLATE = """
            SELECT m.id, m.account_id, m.subject, m.from_addr, m.from_name,
                   m.date_sent, m.internal_date
              FROM messages m
             WHERE {where}
             {order_by}
             LIMIT %s
"""


def compose_date_keyset_sql(*, where: str, order: SortOrder) -> str:
    """Compose one page-fetching statement for ``order`` and ``where``.

    ``where`` is the WHERE-clause body — typically the match clause with
    ``keyset_clause``'s (or ``UNDATED_TAIL_ONLY_SQL``'s) fragment and the
    structured filters appended. Callers pass ``limit`` as the final
    query parameter.
    """
    if order not in DATE_ORDER_BY_SQL:
        raise ValueError(
            f"unknown sort_order {order!r}; expected one of "
            f"{sorted(DATE_ORDER_BY_SQL)}"
        )
    return ROW_SQL_TEMPLATE.format(where=where, order_by=DATE_ORDER_BY_SQL[order])


def keyset_clause(keyset: KeysetCursor) -> tuple[str, list[Any]]:
    """The ``AND …`` fragment placing the walk strictly after ``keyset``.

    The direction is read off ``keyset``, never passed beside it. It used to
    be a second parameter, which re-admitted the exact pairing that putting
    ``order`` on the cursor was meant to make unrepresentable — a descending
    position walked with the ascending predicate. Production was correct only
    because ``Searcher.search`` sets ``effective_order = keyset_cursor.order``
    whenever a cursor exists, i.e. by one caller's discipline, which is the
    standard ``encode_keyset_cursor`` was already raised above. The redundancy
    was total: this is only ever reached with a cursor in hand, and that
    cursor is the only thing that can say which way its position runs.

    Both directions' dated predicates are SQL **row comparisons**, which is
    the only spelling Postgres composes into an ``Index Cond`` on
    ``messages_recent_idx``. The OR-form (``expr < %s OR (expr = %s AND
    id < %s)``) is semantically identical and plans as a per-tuple
    ``Filter``: the walk restarts at the head of the index on every page
    and discards everything before the cursor. Measured **descending**
    mid-walk on the live 128,324-message archive at offset 64,000:
    **70.383 ms and 54,230 buffers with 64,001 rows removed by filter,
    against 0.040 ms and 48 buffers** for the row comparison. (Ascending's
    own #322 run, on a 128,306-message archive, was 62.1 ms / 53,789
    buffers against 0.57 ms / 46 — quoted separately because it is a
    separate run. This docstring used to blend the two, taking the
    milliseconds and buffers from different measurements, in the comment
    whose job is to stop a revert to the OR-form.) The cost is linear in
    scroll depth, so it is invisible on
    page 1 — where this feature's "no new index" measurements were taken —
    and grows without bound on exactly the deep scroll the keyset walk
    exists to serve. This is the trap #75 documents for the browse path; do
    **not** "simplify" either direction back to the OR-form.

    **What the two directions do not share is the undated block**, and that
    asymmetry is why descending took a second fix (#323) after ascending
    (#322). Under ``ASC NULLS FIRST`` the undated rows sit *behind* an
    ascending cursor and must drop out, which ``ROW(NULL, id) > ROW(…)``
    does on its own by evaluating to NULL. Under ``DESC NULLS LAST`` they
    sit *ahead* of a descending cursor and must be admitted — so the
    descending form carried ``OR expr IS NULL`` for exactly that, and that
    disjunct is what denied it the range bound. It is gone; the rows it
    admitted are reached by a second top-up query instead
    (``UNDATED_TAIL_ONLY_SQL``, gated by ``needs_undated_top_up``), which
    is the shape ``api.browse.list_messages`` has used for #75 since before
    this walk existed.

    The ``ts is None`` branches need no top-up in either direction, but
    **they are not the same shape and do not plan alike** — an earlier
    wording claimed one mechanism for both, and the planner contradicts it
    on each half:

    * **Descending** is ``expr IS NULL AND m.id < %s``, and both conjuncts
      land in the ``Index Cond``. The ``id`` comparison is *not* residual —
      it is the index's second column, bounded like the first.
    * **Ascending** is the OR-form ``(expr IS NULL AND id > %s) OR expr IS
      NOT NULL``, which must admit every dated row and therefore gets **no
      index bound at all**: it plans as a ``Filter`` over a backward index
      scan. That is deliberate and does not carry #323's cost — the rows it
      discards are the undated ones already behind the cursor, so its
      residual is bounded by the size of the undated block rather than by
      archive size, and it is only paid while the walk is still inside that
      block. Splitting it into two phases the way descending is split would
      buy nothing and add a second transition to get right.

    Neither branch has a plan test (``test_searcher_sort_order_plan.py``
    covers dated cursors only), so this paragraph is their only record —
    which is why the mechanism is stated per direction rather than shared.
    """
    expr = DATE_EXPR_SQL
    order = keyset.order
    # Named explicitly rather than left to an ``else``. The two lookups are
    # independently reachable: an ``order`` that is not exactly "desc" used
    # to fall through into the *ascending* predicate here and was only
    # stopped by ``DATE_ORDER_BY_SQL``'s KeyError when the SQL string was
    # assembled afterwards. That ordering is an accident of one function's
    # statement order — hoist the ORDER BY out and the guard is gone — and
    # the KeyError it raised carried the bare message ``'DESC'``, naming
    # neither the field nor the search. Still reachable now that the value
    # comes off the cursor: ``KeysetCursor.order`` is a ``Literal`` mypy
    # checks and CI runs no mypy step, so a library caller can construct one
    # carrying ``"DESC"``.
    if order not in DATE_ORDER_BY_SQL:
        raise ValueError(
            f"unknown sort_order {order!r}; expected one of "
            f"{sorted(DATE_ORDER_BY_SQL)}"
        )
    if order == "desc":
        if keyset.ts is None:
            # Already in the NULLS-LAST tail: paginate by id alone.
            return f" AND {expr} IS NULL AND m.id < %s ", [keyset.id]
        return (
            f" AND ROW({expr}, m.id) < ROW(%s, %s) ",
            [keyset.ts, keyset.id],
        )
    if keyset.ts is None:
        # Still in the undated head: the rest of it, then every dated row.
        # An OR-form, and deliberately left as one: its residual is bounded
        # by the number of *undated* rows rather than by archive size, so
        # it does not carry #323's scaling cost. Splitting it into two
        # phases the way descending is split would buy nothing and add a
        # second transition to get right.
        return (
            f" AND (({expr} IS NULL AND m.id > %s) OR {expr} IS NOT NULL) ",
            [keyset.id],
        )
    return (
        f" AND ROW({expr}, m.id) > ROW(%s, %s) ",
        [keyset.ts, keyset.id],
    )


def needs_undated_top_up(
    *,
    keyset: KeysetCursor | None,
    rows_returned: int,
    fetch_limit: int,
) -> bool:
    """Whether this page must be topped up from the undated block (#323).

    True only for a **descending dated** continuation that came back short.
    Each conjunct earns its place:

    * ``keyset is not None`` — page 1 carries no cursor predicate at all,
      so the index walk streams into the undated tail by itself.
    * ``keyset.order == "desc"`` — ascending puts the undated block at the
      head of the walk, where ``keyset_clause``'s own predicate already
      reaches it. Read off the cursor rather than taken beside it, like
      ``keyset_clause``'s: the direction is only ever consulted once a
      cursor is in hand to supply it, so a second source for it could only
      ever disagree.
    * ``keyset.ts is not None`` — a cursor already inside the undated block
      is being paginated by id within it; topping up would re-emit rows the
      caller has seen.
    * ``rows_returned < fetch_limit`` — a full page has no slots to fill,
      and the next page's cursor will reach the tail in its turn.

    Separated from the query so the rule is testable without a database,
    and so the reasoning above sits with the condition rather than inside
    a method that is mostly SQL assembly.

    **The two statements are two snapshots.** Under READ COMMITTED the dated
    page and its top-up are read separately, so a row inserted into the
    undated block between them can be missed or repeated at that one
    boundary. ``api.browse.list_messages`` has had exactly this property
    since #75; it is noted rather than fixed because closing it means a
    repeatable-read transaction around every page.
    """
    return (
        keyset is not None
        and keyset.order == "desc"
        and keyset.ts is not None
        and rows_returned < fetch_limit
    )
