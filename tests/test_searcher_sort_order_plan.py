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
from localmail.search.searcher import KeysetCursor

# Enough rows that the planner has a real choice to make; below ~100 a
# 300-row table is cheap enough to seq-scan whatever the ordering.
_SEED_DATED = 300
_SEED_UNDATED = 3
_PAGE_SIZE = 50
_EPOCH = datetime(2024, 1, 1, tzinfo=timezone.utc)

#: The pre-fix spelling, kept verbatim as the negative control. Deliberately
#: a literal and **not** derived from ``_DATE_ORDER_BY_SQL``: it is the thing
#: the shipped constant must not become, so deriving it would make the
#: control track the very change it exists to detect.
_NULLS_LAST_ORDER_BY = (
    "ORDER BY COALESCE(m.internal_date, m.date_sent) ASC NULLS LAST, m.id ASC"
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


def _order_by(order: str) -> str:
    """The production ORDER BY for ``order`` — never a copy of it."""
    return searcher_mod._DATE_ORDER_BY_SQL[order]


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
    keyset = KeysetCursor(ts=_EPOCH + timedelta(days=100), id=101)
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
