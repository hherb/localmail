# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Plan-regression tests for the date-ordered walk's two ORDER BY spellings.

The whole reason `sort_order` needed **no migration and no new index** is
that ascending is spelled ``ASC NULLS FIRST, id ASC`` — the exact reverse of
``messages_recent_idx`` (``COALESCE(internal_date, date_sent) DESC NULLS
LAST, id DESC``) — and is therefore served by a backward walk of the index
already there. Measured on the live 128,289-message archive:

===============================  ====================  =======  ========
ordering                         plan                  buffers  time
===============================  ====================  =======  ========
``ASC NULLS FIRST, id ASC``      Index Scan Backward         44  0.83 ms
``ASC NULLS LAST, id ASC``       Gather Merge, full sort 33,372    42 ms
``IS NOT NULL`` + ``NULLS LAST`` Gather Merge, full sort 33,372    30 ms
===============================  ====================  =======  ========

The *functional* half of that is already pinned — a ``NULLS LAST`` slip
breaks ``test_searcher_sort_order_walk.py``'s undated-rows-first and
reversal assertions, because the two spellings order undated rows
differently. Nothing pinned the **performance** half, which is the claim
the design's "no new index" decision actually rests on, and a slip there is
invisible: the rows come back correct and the archive full-sorts on every
page.

**What these assert: the planner's choice, not merely eligibility.** Unlike
``test_api_browse_plan.py`` — which has to hide every competing index and
turn off ``enable_seqscan`` to get a verdict at fixture scale — the
ascending spelling wins here on its own at 300 rows, with nothing hidden
and nothing forced. That is the stronger assertion, so it is the one made.
If a future planner stops choosing it at this scale, the fallback is
``test_api_browse_plan.py``'s technique (hide competitors, ``SET LOCAL
enable_seqscan = off``) and the docstrings must then say "eligibility"
rather than "choice"; do not weaken the assertion silently.

The ``NULLS LAST`` spelling is kept as the **negative control** — the role
``--predicate-form pre75`` plays in ``run_browse_explain.py``. Without it
the assertion is close to tautological: ``messages_recent_idx`` is the only
date-ordered index on the table, so "the plan mentions it" would pass for
any ordering the planner could bolt a Sort on top of.

**The ORDER BY is only half of it, and the other half shipped broken.** A
walk also carries a keyset predicate from page 2 on, and the scan-node
assertions above cannot see it: the pre-fix OR-form
(``expr > %s OR (expr = %s AND id > %s)``) keeps ``Index Scan Backward``
and adds no Sort, so it satisfied every one of them while planning the
predicate as a per-tuple ``Filter`` — each page rescanning the index from
the head. Measured mid-walk on the live archive, page ~1250: 62.1 ms /
53,789 buffers / 64,001 rows removed by filter, against 0.57 ms / 46
buffers for the shipped ``ROW(expr, m.id) > ROW(%s, %s)``. Linear in scroll
depth, so it is invisible on page 1 — the only page the table above
measures. ``test_the_ascending_keyset_predicate_composes_an_index_range_bound``
pins it with ``_PRE_FIX_OR_FORM`` as a second negative control, and checks
the two forms return identical rows so the plan really is all that differs.

The ORDER BY under test is composed from the production
``searcher._DATE_ORDER_BY_SQL`` and the keyset predicate from the
production ``searcher._keyset_clause``, so a rewrite of either lands here
automatically — the #77 convention. The SELECT/FROM around them is the one
hand-written part: ``_date_keyset_search`` builds its statement inline and
exposes no emitter, and extracting one is a source change this test is not
the occasion for. It mirrors that method's projection so the plan shape
(heap fetch, not an index-only scan) is the shipped one.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import psycopg

from localmail.search import searcher as searcher_mod
from localmail.search.searcher import KeysetCursor, SortOrder

# Enough rows that the planner has a real choice to make; below ~100 a
# 300-row table is cheap enough to seq-scan whatever the ordering.
_SEED_DATED = 300
_SEED_UNDATED = 3
#: Extra rows sharing the *cursor's own* timestamp (``_EPOCH + 100 days``).
#: Without a tie there, the row comparison and the tiebreaker-less
#: ``expr > ts`` select identical rows, so the fair-control assertion below
#: could not tell them apart — and its docstring claimed it could.
_SEED_TIED_AT_CURSOR = 5
_PAGE_SIZE = 50
_EPOCH = datetime(2024, 1, 1, tzinfo=timezone.utc)

#: The pre-fix spelling, kept verbatim as the negative control. Deliberately
#: a literal and **not** derived from ``_DATE_ORDER_BY_SQL``: it is the thing
#: the shipped constant must not become, so deriving it would make the
#: control track the very change it exists to detect.
_NULLS_LAST_ORDER_BY = (
    "ORDER BY COALESCE(m.internal_date, m.date_sent) ASC NULLS LAST, m.id ASC"
)

#: The pre-fix ascending *keyset predicate*, kept verbatim as the second
#: negative control — the ORDER BY above is only half of what decides the
#: plan. Semantically identical to the shipped row comparison; it differs
#: only in that Postgres will not compose it into an index range bound.
#: A literal for the same reason ``_NULLS_LAST_ORDER_BY`` is one.
_PRE_FIX_OR_FORM = (
    " AND (COALESCE(m.internal_date, m.date_sent) > %s"
    " OR (COALESCE(m.internal_date, m.date_sent) = %s AND m.id > %s)) "
)

#: The pre-#323 *descending* keyset predicate, kept verbatim as this
#: file's third negative control. It carries a third disjunct the ascending
#: form never needed — ``OR expr IS NULL``, admitting the NULLS-LAST undated
#: tail that sits *ahead* of a descending cursor — and that disjunct is why
#: the descending half could not simply copy #322's row comparison. The
#: shipped fix drops it and reaches the tail through a second top-up query
#: instead, exactly as ``browse.py`` does for #75.
_PRE323_DESC_OR_FORM = (
    " AND (COALESCE(m.internal_date, m.date_sent) < %s"
    " OR (COALESCE(m.internal_date, m.date_sent) = %s AND m.id < %s)"
    " OR COALESCE(m.internal_date, m.date_sent) IS NULL) "
)

#: ``_date_keyset_search``'s projection. Kept faithful so the plan is the
#: shipped one — a bare ``SELECT m.id`` would let Postgres consider an
#: index-only scan the real query can never have.
_PROJECTION = (
    "SELECT m.id, m.account_id, m.subject, m.from_addr, m.from_name,"
    " m.date_sent, m.internal_date"
    "  FROM messages m"
    " WHERE "
)

_BACKWARD_SCAN = "Index Scan Backward using messages_recent_idx"
_FORWARD_SCAN = "Index Scan using messages_recent_idx"


def _seed(conn: psycopg.Connection) -> int:
    """Seed one account with dated and undated messages; return its id.

    The undated rows are what the two spellings disagree about, so the
    fixture carries a few even though the plan shape does not turn on them.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method)"
            " VALUES ('plan', 'plan@x.test', 'imap.x', 'password') RETURNING id"
        )
        row = cur.fetchone()
        assert row is not None
        account_id = int(row[0])
        cur.executemany(
            "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
            " body_text, date_sent, headers, attachments, raw_bytes, size_bytes)"
            " VALUES (%s, %s, %s, %s, 'needle body', %s, '{}'::jsonb,"
            " '[]'::jsonb, 'r', 4)",
            [
                (account_id, f"<m{i}@plan.local>", i.to_bytes(32, "big"),
                 f"subj-{i} needle", _EPOCH + timedelta(days=i))
                for i in range(_SEED_DATED)
            ],
        )
        cur.executemany(
            "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
            " body_text, date_sent, headers, attachments, raw_bytes, size_bytes)"
            " VALUES (%s, %s, %s, %s, 'needle body', %s, '{}'::jsonb,"
            " '[]'::jsonb, 'r', 4)",
            [
                (account_id, f"<tie{k}@plan.local>",
                 (20_000 + k).to_bytes(32, "big"), f"tie-{k} needle",
                 _EPOCH + timedelta(days=100))
                for k in range(_SEED_TIED_AT_CURSOR)
            ],
        )
        cur.executemany(
            "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
            " body_text, headers, attachments, raw_bytes, size_bytes)"
            " VALUES (%s, %s, %s, %s, 'needle body', '{}'::jsonb,"
            " '[]'::jsonb, 'r', 4)",
            [
                (account_id, f"<u{j}@plan.local>",
                 (10_000 + j).to_bytes(32, "big"), f"undated-{j} needle")
                for j in range(_SEED_UNDATED)
            ],
        )
        cur.execute("ANALYZE messages")
    conn.commit()
    return account_id


def _explain(conn: psycopg.Connection, sql: str, params: list) -> str:
    with conn.cursor() as cur:
        cur.execute("EXPLAIN (FORMAT TEXT) " + sql, params)
        return "\n".join(r[0] for r in cur.fetchall())


def _has_full_sort_node(plan: str) -> bool:
    """True iff the plan contains a non-incremental ``Sort``.

    Same line-based heuristic as ``test_api_browse_plan.py`` and the
    ``run_browse_explain.py`` harness — a full Sort on top of a scan means
    the whole result set is materialised before the LIMIT can short-circuit,
    which is exactly the 33,372-buffer plan the table above measures.
    """
    for raw in plan.splitlines():
        line = raw.strip()
        if (line.startswith("->  Sort") or line.startswith("Sort  ")) \
                and "Incremental Sort" not in line:
            return True
    return False


def _order_by(order: SortOrder) -> str:
    """The production ORDER BY for ``order`` — never a copy of it.

    Typed ``SortOrder``, not ``str``: widening it here would give back
    exactly the wrong-literal slip ``_DATE_ORDER_BY_SQL``'s own comment
    says its key type exists to refuse.
    """
    return searcher_mod._DATE_ORDER_BY_SQL[order]


#: EXPLAIN renders the expression without the ``m.`` qualifier.
_DATE_EXPR_IN_PLAN = "COALESCE(internal_date, date_sent)"


def _plan_line_mentioning(plan: str, node: str) -> str | None:
    """The first ``node:`` line naming the date expression, if any.

    Matched on the node label rather than by parsing the plan tree: this
    file only ever asks a yes/no question about one predicate, and the
    label plus the expression is enough to answer it unambiguously.
    """
    for raw in plan.splitlines():
        line = raw.strip()
        if line.startswith(node + ":") and _DATE_EXPR_IN_PLAN in line:
            return line
    return None


def _index_cond_mentions_the_date_expr(plan: str) -> bool:
    return _plan_line_mentioning(plan, "Index Cond") is not None


def _filter_mentions_the_date_expr(plan: str) -> bool:
    return _plan_line_mentioning(plan, "Filter") is not None


# ---- The blank-query walk (branch 2) -------------------------------------


def test_the_shipped_ascending_order_by_plans_as_a_backward_index_scan(
    db_conn: psycopg.Connection,
) -> None:
    """``ASC NULLS FIRST, id ASC`` walks ``messages_recent_idx`` backwards.

    This is the assertion the "no migration and no new index" decision
    rests on. The negative control below is what makes it mean something.
    """
    _seed(db_conn)
    plan = _explain(
        db_conn,
        _PROJECTION + "TRUE " + _order_by("asc") + " LIMIT %s",
        [_PAGE_SIZE + 1],
    )
    assert _BACKWARD_SCAN in plan, plan
    assert not _has_full_sort_node(plan), plan


def test_the_nulls_last_spelling_does_not_get_the_backward_scan(
    db_conn: psycopg.Connection,
) -> None:
    """The negative control: the pre-fix spelling full-sorts instead.

    Postgres will not treat ``NULLS LAST`` as equivalent to the index's
    ordering — and, per the design's measurements, an ``IS NOT NULL``
    restriction does not rescue it either. Without this test the positive
    assertion above passes for any ordering, since ``messages_recent_idx``
    is the only date-ordered index on the table.
    """
    _seed(db_conn)
    plan = _explain(
        db_conn,
        _PROJECTION + "TRUE " + _NULLS_LAST_ORDER_BY + " LIMIT %s",
        [_PAGE_SIZE + 1],
    )
    assert _BACKWARD_SCAN not in plan, plan
    assert _has_full_sort_node(plan), plan


def test_the_shipped_descending_order_by_still_plans_as_a_forward_scan(
    db_conn: psycopg.Connection,
) -> None:
    """The unchanged direction, asserted so the probe itself is trustworthy.

    It also guards the other way a reader might "tidy" the constant:
    normalising both directions onto one spelling would break exactly one
    of the two assertions in this file, whichever way it was normalised.
    """
    _seed(db_conn)
    plan = _explain(
        db_conn,
        _PROJECTION + "TRUE " + _order_by("desc") + " LIMIT %s",
        [_PAGE_SIZE + 1],
    )
    assert _FORWARD_SCAN in plan, plan
    assert _BACKWARD_SCAN not in plan, plan
    assert not _has_full_sort_node(plan), plan


# ---- The lexical walk (branch 1) and the keyset continuation -------------


def test_the_ascending_fts_restricted_form_keeps_the_backward_scan(
    db_conn: psycopg.Connection,
) -> None:
    """The lexical branch's real shape: FTS as a per-tuple filter.

    The search path already walks ``messages_recent_idx`` with the FTS
    match applied per tuple — the same arrangement #72 documents for the
    ACL filter on browse — and the backward walk serves ascending
    identically. Asserted separately from the blank-query form because
    that equivalence is a claim the design makes, not something the
    blank-query plan can show.
    """
    _seed(db_conn)
    fts = "m.fts_v2 @@ plainto_tsquery('simple', %s) "
    plan = _explain(
        db_conn, _PROJECTION + fts + _order_by("asc") + " LIMIT %s",
        ["needle", _PAGE_SIZE + 1],
    )
    control = _explain(
        db_conn, _PROJECTION + fts + _NULLS_LAST_ORDER_BY + " LIMIT %s",
        ["needle", _PAGE_SIZE + 1],
    )
    assert _BACKWARD_SCAN in plan, plan
    assert not _has_full_sort_node(plan), plan
    assert _BACKWARD_SCAN not in control, control
    assert _has_full_sort_node(control), control


def test_the_ascending_keyset_predicate_keeps_the_backward_scan(
    db_conn: psycopg.Connection,
) -> None:
    """A mid-walk continuation page, with the account filter beside it.

    Page 1's plan is not evidence about page 2's: the keyset predicate is
    what the walk actually carries from the second page on, and #75 is this
    codebase's precedent for a cursor predicate that changes the plan on
    the browse path. The predicate comes from the production
    ``_keyset_clause`` so a rewrite of it lands here.
    """
    account_id = _seed(db_conn)
    keyset = KeysetCursor(ts=_EPOCH + timedelta(days=100), id=101, order="desc")
    clause, clause_params = searcher_mod._keyset_clause(keyset, "asc")
    where = "TRUE " + clause + " AND m.account_id = ANY(%s) "
    params = clause_params + [[account_id], _PAGE_SIZE + 1]
    plan = _explain(db_conn, _PROJECTION + where + _order_by("asc") + " LIMIT %s",
                    list(params))
    control = _explain(db_conn,
                       _PROJECTION + where + _NULLS_LAST_ORDER_BY + " LIMIT %s",
                       list(params))
    assert _BACKWARD_SCAN in plan, plan
    assert not _has_full_sort_node(plan), plan
    assert _BACKWARD_SCAN not in control, control
    assert _has_full_sort_node(control), control


def test_the_ascending_keyset_predicate_composes_an_index_range_bound(
    db_conn: psycopg.Connection,
) -> None:
    """The keyset predicate must be an ``Index Cond``, never a ``Filter``.

    The scan-node assertion above is **not** enough, and shipped that way
    once: the OR-form predicate
    (``expr > %s OR (expr = %s AND id > %s)``) is semantically identical to
    the row comparison, keeps ``Index Scan Backward``, and adds no Sort —
    so every assertion in the test above passes for it. What it loses is
    the range bound: the walk restarts at the head of the index on every
    page and discards each preceding row as a per-tuple ``Filter``.
    Measured mid-walk on the live 128k archive, page ~1250: 62.1 ms and
    53,789 buffers with 64,001 rows removed by filter, against 0.57 ms and
    46 buffers. Linear in scroll depth, hence invisible on page 1 — which
    is where this feature's "no new index" measurements were taken.

    ``_PRE_FIX_OR_FORM`` is the second negative control, playing the role
    ``--predicate-form pre75`` plays in ``run_browse_explain.py``. A
    literal, deliberately: it is the thing ``_keyset_clause`` must not
    become, so deriving it would make the control track the very change it
    exists to detect.

    Asserted structurally rather than by row counts so it holds at any
    scale — at fixture size both forms are fast, and the whole point is
    that the difference does not show up in a timing until production
    depth.
    """
    account_id = _seed(db_conn)
    keyset = KeysetCursor(ts=_EPOCH + timedelta(days=100), id=101, order="desc")
    clause, clause_params = searcher_mod._keyset_clause(keyset, "asc")
    shipped = _explain(
        db_conn,
        _PROJECTION + "TRUE " + clause + " AND m.account_id = ANY(%s) "
        + _order_by("asc") + " LIMIT %s",
        [*clause_params, [account_id], _PAGE_SIZE + 1],
    )
    control = _explain(
        db_conn,
        _PROJECTION + "TRUE " + _PRE_FIX_OR_FORM + " AND m.account_id = ANY(%s) "
        + _order_by("asc") + " LIMIT %s",
        [keyset.ts, keyset.ts, keyset.id, [account_id], _PAGE_SIZE + 1],
    )
    assert _index_cond_mentions_the_date_expr(shipped), shipped
    assert not _filter_mentions_the_date_expr(shipped), (
        "the ascending keyset predicate degraded to a per-tuple Filter: every "
        "continuation page now rescans the index from the head\n" + shipped
    )
    # The control proves the assertion above can fail.
    assert not _index_cond_mentions_the_date_expr(control), control
    assert _filter_mentions_the_date_expr(control), control

    # And that the control is a fair one: the two forms must be
    # *semantically* identical, so the plan really is the only thing
    # separating them. Without this the test would also pass for a row
    # comparison that composes a beautiful index bound over the wrong rows
    # — transposed operands, a dropped tiebreaker — which is a correctness
    # bug wearing this test's approval.
    #
    # The tiebreaker half of that claim only holds because `_seed` puts
    # `_SEED_TIED_AT_CURSOR` rows at the cursor's own timestamp. With every
    # date distinct — as every other fixture in this suite has them —
    # `ROW(expr, id) > ROW(ts, id)` and a tiebreaker-less `expr > ts`
    # select exactly the same rows, and this assertion is blind to the
    # difference. The claim was false as originally written.
    def _ids(where: str, params: list) -> list[int]:
        with db_conn.cursor() as cur:
            cur.execute(
                _PROJECTION + where + _order_by("asc") + " LIMIT %s", params,
            )
            return [r[0] for r in cur.fetchall()]

    assert _ids("TRUE " + clause + " AND m.account_id = ANY(%s) ",
                [*clause_params, [account_id], _PAGE_SIZE + 1]) == \
        _ids("TRUE " + _PRE_FIX_OR_FORM + " AND m.account_id = ANY(%s) ",
             [keyset.ts, keyset.ts, keyset.id, [account_id], _PAGE_SIZE + 1])


def _undated_ids(conn: psycopg.Connection, account_id: int) -> set[int]:
    """The seeded rows with no usable date, which the two forms disagree about."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM messages WHERE account_id = %s"
            " AND COALESCE(internal_date, date_sent) IS NULL",
            [account_id],
        )
        return {int(r[0]) for r in cur.fetchall()}


def test_the_descending_keyset_predicate_composes_an_index_range_bound(
    db_conn: psycopg.Connection,
) -> None:
    """#323: the descending half must be an ``Index Cond`` too.

    #322 fixed the ascending predicate and left this one as the OR-form,
    which is precisely the trap CLAUDE.md's #75 entry documents for the
    browse path — *"Do NOT rewrite the predicate as the OR-form even though
    it's semantically equivalent"* — reintroduced on the search path in
    newly written code. Measured mid-walk on the live 128,306-message
    archive at page ~1250: **62.7 ms / 53,789 buffers / 64,001 rows removed
    by filter**, against 0.57 ms / 46 buffers for the row comparison.
    Linear in scroll depth, so it is invisible on page 1 and unbounded on
    exactly the deep scroll ``_date_keyset_search``'s own docstring offers
    as the reason the branch exists.

    Structural rather than a timing, for the reason the ascending twin is:
    at fixture scale both forms are fast, and being invisible until
    production depth is the whole defect.
    """
    account_id = _seed(db_conn)
    keyset = KeysetCursor(ts=_EPOCH + timedelta(days=100), id=101, order="desc")
    clause, clause_params = searcher_mod._keyset_clause(keyset, "desc")
    shipped = _explain(
        db_conn,
        _PROJECTION + "TRUE " + clause + " AND m.account_id = ANY(%s) "
        + _order_by("desc") + " LIMIT %s",
        [*clause_params, [account_id], _PAGE_SIZE + 1],
    )
    control = _explain(
        db_conn,
        _PROJECTION + "TRUE " + _PRE323_DESC_OR_FORM
        + " AND m.account_id = ANY(%s) " + _order_by("desc") + " LIMIT %s",
        [keyset.ts, keyset.ts, keyset.id, [account_id], _PAGE_SIZE + 1],
    )
    assert _index_cond_mentions_the_date_expr(shipped), shipped
    assert not _filter_mentions_the_date_expr(shipped), (
        "the descending keyset predicate degraded to a per-tuple Filter: "
        "every continuation page now rescans the index from the head\n"
        + shipped
    )
    # The control proves the assertion above can fail.
    assert not _index_cond_mentions_the_date_expr(control), control
    assert _filter_mentions_the_date_expr(control), control


def test_the_descending_predicate_drops_only_the_undated_tail(
    db_conn: psycopg.Connection,
) -> None:
    """The fairness half: it must lose the undated rows and nothing else.

    Without this, the plan test above would also pass for a row comparison
    that composes a beautiful index bound over the *wrong* rows —
    transposed operands, or a dropped ``id`` tiebreaker — which is a
    correctness bug wearing that test's approval. The tiebreaker half only
    bites because ``_seed`` puts ``_SEED_TIED_AT_CURSOR`` rows at the
    cursor's own timestamp.

    The undated rows are subtracted explicitly rather than relying on the
    ``LIMIT`` never reaching the NULLS-LAST tail. It does not reach it at
    this fixture size, but that is an accident of the row count, and a
    fairness assertion resting on an accident is not one. Those rows are
    reached by ``_date_keyset_search``'s top-up query instead — pinned
    behaviourally in ``test_searcher_sort_order_walk.py``.
    """
    account_id = _seed(db_conn)
    undated = _undated_ids(db_conn, account_id)
    assert undated, "fixture seeds no undated rows; the assertion is vacuous"
    keyset = KeysetCursor(ts=_EPOCH + timedelta(days=100), id=101, order="desc")
    clause, clause_params = searcher_mod._keyset_clause(keyset, "desc")
    # Past every seeded row, so both forms run to exhaustion and the
    # comparison covers the tail rather than stopping short of it.
    unbounded = _SEED_DATED + _SEED_TIED_AT_CURSOR + _SEED_UNDATED + 1

    def _ids(where: str, params: list) -> list[int]:
        with db_conn.cursor() as cur:
            cur.execute(
                _PROJECTION + where + _order_by("desc") + " LIMIT %s", params,
            )
            return [int(r[0]) for r in cur.fetchall()]

    shipped_ids = _ids(
        "TRUE " + clause + " AND m.account_id = ANY(%s) ",
        [*clause_params, [account_id], unbounded],
    )
    control_ids = _ids(
        "TRUE " + _PRE323_DESC_OR_FORM + " AND m.account_id = ANY(%s) ",
        [keyset.ts, keyset.ts, keyset.id, [account_id], unbounded],
    )
    assert set(control_ids) & undated, (
        "the pre-fix form no longer admits the undated tail; the "
        "subtraction below has nothing to subtract"
    )
    assert shipped_ids == [i for i in control_ids if i not in undated]


# ---- The index the whole arrangement depends on -------------------------


def test_messages_recent_idx_is_the_exact_reverse_of_the_ascending_order_by(
    db_conn: psycopg.Connection,
) -> None:
    """The reversal claim, checked against the live index definition.

    ``test_api_browse_plan.py`` pins the same index for the descending
    browse path. It is repeated here from the other direction because the
    ascending spelling's correctness is *defined* by being that index
    reversed: DESC/ASC and NULLS LAST/NULLS FIRST must be flipped on the
    date key and the ``id`` tiebreaker alike. Flip only one and the plan
    assertions above are the failure an operator sees, with no line saying
    why.
    """
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT indexdef FROM pg_indexes"
            " WHERE indexname = 'messages_recent_idx'"
        )
        row = cur.fetchone()
    assert row is not None, "messages_recent_idx index missing"
    indexdef = row[0]
    assert "(COALESCE(internal_date, date_sent) DESC NULLS LAST, id DESC)" \
        in indexdef, indexdef
    ascending = _order_by("asc")
    for token in ("ASC NULLS FIRST", "m.id ASC"):
        assert token in ascending, ascending
    assert "NULLS LAST" not in ascending, (
        "the ascending ORDER BY was normalised to NULLS LAST: it no longer "
        "reverses messages_recent_idx and every page full-sorts the archive"
    )
