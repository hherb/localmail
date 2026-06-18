# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Paginated message browse — service layer.

Mirrors the shape of the /v1/changes payload but supports keyset pagination
into the past instead of forward incremental polling. The wire cursor is
opaque (see browse_cursor.py); the ACL filter applies at the SQL boundary.

The keyset sort order is
``COALESCE(internal_date, date_sent) DESC NULLS LAST, id DESC``, served by
the ``messages_recent_idx`` expression index. Rows with both date columns
NULL sit in the NULLS-LAST tail and are paginated by id alone after the
dated portion is exhausted (the cursor flips to its ``ts=None`` flavour;
see ``browse_cursor.BrowseCursor``).

#75: the dated-cursor predicate must NOT include an ``OR COALESCE IS NULL``
disjunct. That form prevents Postgres from composing an index range bound,
forcing the planner to walk every row above the cursor on every mid-keyset
page. NULL-tail rows are reached via a second top-up query when the dated
path is exhausted — see ``list_messages``.

#77: ``BROWSE_ROW_SQL_TEMPLATE``, ``compose_browse_sql``, and ``build_where``
are public so the eligibility tests in ``tests/test_api_browse_plan.py`` and
the EXPLAIN harness in ``tests/acceptance/run_browse_explain.py`` can compose
the production SQL shape directly. Any refactor of the SELECT / FROM /
ORDER BY shape goes through this module — there is no duplicate inline SQL
elsewhere to drift against.

#85: folder filtering uses an ``EXISTS (SELECT 1 FROM message_labels …)``
semi-join, not a ``JOIN message_labels`` + ``SELECT DISTINCT`` shape.
Benchmark on 200k rows × broad folder showed the EXISTS variant uses ~45-50%
fewer buffer hits per page (no row multiplication = no Unique/Sort tax) and
replaces a 3-node ``Nested Loop + Incremental Sort + Unique`` chain with a
single ``Nested Loop Semi Join``. The semi-join also short-circuits the
labels lookup as soon as a matching row is found per outer message, so the
index searches on ``message_labels_pkey`` scale with returned-row count
rather than with all-matched-label count.
"""
from __future__ import annotations

from typing import Any

import psycopg

from localmail.api.browse_cursor import (
    BrowseCursor, decode_browse_cursor, encode_browse_cursor,
)


# Public so the eligibility tests in ``tests/test_api_browse_plan.py`` and
# the EXPLAIN harness in ``tests/acceptance/run_browse_explain.py`` can
# compose the same SQL the production path emits, instead of duplicating
# it inline (#77). The single format placeholder is ``{where}`` — the
# WHERE-clause body returned by ``build_where``.
#
# Any consumer that wants the actual SQL string for a given query shape
# should call ``compose_browse_sql(where=...)`` rather than ``.format()``-ing
# this constant directly. Keep the COALESCE expression and the ``sort_ts``
# alias in sync with the index definition in migration ``0018``; the LIMIT
# short-circuit through ``messages_recent_idx`` depends on both matching
# exactly.
#
# No DISTINCT and no ``message_labels`` JOIN: folder filtering moves into
# a ``WHERE EXISTS (SELECT 1 FROM message_labels …)`` clause emitted by
# ``build_where``. EXISTS guarantees no row multiplication, so DISTINCT is
# unnecessary on either branch (#85).
BROWSE_ROW_SQL_TEMPLATE = """
    SELECT m.id, m.subject, m.from_addr, m.from_name, m.date_sent,
           m.internal_date, m.account_id, a.name,
           COALESCE(m.internal_date, m.date_sent) AS sort_ts
      FROM messages m
      JOIN accounts a ON a.id = m.account_id
     WHERE {where}
     ORDER BY sort_ts DESC NULLS LAST, m.id DESC
     LIMIT %s
"""


def compose_browse_sql(*, where: str) -> str:
    """Compose the final browse SELECT string for a given WHERE clause.

    ``where`` is the WHERE-clause body, typically what ``build_where``
    returned. Folder filtering is expressed inside ``where`` as an
    ``EXISTS`` subquery — there is no JOIN-shape switch any more (#85).
    """
    return BROWSE_ROW_SQL_TEMPLATE.format(where=where)


def list_messages(
    conn: psycopg.Connection,
    *,
    allowed_account_ids: list[int],
    account_ids: list[int] | None = None,
    folder_ids: list[int] | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Return one keyset page of messages and a `next_cursor` for the next one.

    Ordering: ``COALESCE(internal_date, date_sent) DESC NULLS LAST, id DESC``.
    Uses the ``messages_recent_idx`` expression index.

    ACL: ``allowed_account_ids`` is the *authoritative* ACL list. Caller's
    ``account_ids`` filter is intersected against it before the query runs;
    an empty intersection short-circuits to an empty page.

    ``cursor`` is the opaque token returned as ``next_cursor`` on the
    previous page; ``None`` for the initial page. Malformed cursors raise
    ``ValidationFailed`` so the HTTP layer emits a 400.
    """
    if not allowed_account_ids:
        return {"messages": [], "next_cursor": None}

    effective_account_ids = _intersect_account_ids(allowed_account_ids, account_ids)
    if not effective_account_ids:
        return {"messages": [], "next_cursor": None}

    parsed_cursor = decode_browse_cursor(cursor) if cursor is not None else None

    # Fetch one extra row to detect "more pages remain" without a COUNT.
    fetch_limit = limit + 1
    rows = _fetch_rows(
        conn,
        account_ids=effective_account_ids,
        folder_ids=folder_ids,
        cursor=parsed_cursor,
        limit=fetch_limit,
    )

    # Dated path exhausted past this cursor: top up from the NULL-tail in
    # the same response. The dated predicate (#75) excludes NULL rows so
    # the user would otherwise see a short page when a full one was
    # available, and the dated→NULL transition would require an extra
    # round-trip.
    if (parsed_cursor is not None
            and parsed_cursor.ts is not None
            and len(rows) < fetch_limit):
        remaining = fetch_limit - len(rows)
        rows = rows + _fetch_rows(
            conn,
            account_ids=effective_account_ids,
            folder_ids=folder_ids,
            cursor=None,
            limit=remaining,
            null_tail_only=True,
        )

    has_more = len(rows) > limit
    page_rows = rows[:limit]
    # Wire `date` is COALESCE(internal_date, date_sent) — i.e. the sort key
    # itself. Returning a different column than the one we sort by makes the
    # displayed dates look out of order whenever the two differ.
    messages = [
        {
            "message_id": str(mid),
            "subject": subject,
            "from": {"address": from_addr, "name": from_name},
            "date": (internal_date or date_sent).isoformat()
                    if (internal_date or date_sent) else None,
            "account": {"id": str(account_id), "name": account_name},
        }
        for (mid, subject, from_addr, from_name, date_sent, internal_date,
             account_id, account_name, _sort_ts) in page_rows
    ]
    next_cursor: str | None = None
    if has_more and page_rows:
        (last_mid, _, _, _, last_date_sent, last_internal_date,
         _, _, _) = page_rows[-1]
        keyset_ts = last_internal_date or last_date_sent
        next_cursor = encode_browse_cursor(
            BrowseCursor(ts=keyset_ts, id=int(last_mid))
        )
    return {"messages": messages, "next_cursor": next_cursor}


def _intersect_account_ids(
    allowed: list[int], requested: list[int] | None,
) -> list[int]:
    if not requested:
        return list(allowed)
    return sorted(set(allowed) & set(requested))


def _fetch_rows(
    conn: psycopg.Connection,
    *,
    account_ids: list[int],
    folder_ids: list[int] | None,
    cursor: BrowseCursor | None,
    limit: int,
    null_tail_only: bool = False,
) -> list[tuple[Any, ...]]:
    """Execute one row-fetching query and return the raw rows.

    ``null_tail_only`` is the top-up path: used by ``list_messages`` after
    a dated cursor exhausts the dated portion, to fill the remaining slots
    of the same response from the NULL-tail.
    """
    where, params = build_where(
        account_ids=account_ids,
        folder_ids=folder_ids,
        cursor=cursor,
        null_tail_only=null_tail_only,
    )
    sql = compose_browse_sql(where=where)
    with conn.cursor() as cur:
        cur.execute(sql, params + [limit])
        return list(cur.fetchall())


def build_where(
    *,
    account_ids: list[int],
    folder_ids: list[int] | None,
    cursor: BrowseCursor | None,
    null_tail_only: bool = False,
) -> tuple[str, list[Any]]:
    """Compose the WHERE clause + params for one row-fetching query.

    Four modes:

    1. ``cursor is None, null_tail_only is False`` — initial page. WHERE is
       just the ACL (+ optional folder) filter, so the index walk streams
       dated rows first and NULL rows in the NULLS-LAST tail via LIMIT.
    2. ``cursor.ts is not None`` — dated keyset. Range-seekable predicate;
       deliberately excludes NULL rows so Postgres can compose the cursor
       bound as an Index Cond (#75). NULL-tail rows are reached via mode 4.
    3. ``cursor.ts is None`` — NULL-tail keyset. ``IS NULL AND id < %s``;
       walks the NULL-tail strictly by descending id.
    4. ``null_tail_only is True, cursor is None`` — NULL-tail top-up after
       a dated cursor exhausted the dated portion. ``IS NULL`` with no id
       lower bound; ordered by id DESC via the shared ORDER BY.
    """
    clauses = ["m.account_id = ANY(%s)"]
    params: list[Any] = [account_ids]
    if folder_ids:
        # EXISTS semi-join — no row multiplication, no DISTINCT needed (#85).
        # Postgres turns this into a Nested Loop Semi Join that short-circuits
        # the labels scan on the first matching row per outer message; the
        # DISTINCT+JOIN shape it replaces had a 3-node Sort+Unique chain
        # above the same Nested Loop, sorting on every projected column.
        clauses.append(
            "EXISTS (SELECT 1 FROM message_labels ml"
            " WHERE ml.message_id = m.id"
            " AND ml.mailbox_id = ANY(%s))"
        )
        params.append(folder_ids)
    if null_tail_only:
        if cursor is not None:
            # ValueError (not assert) so the invariant holds under `python -O`.
            raise ValueError(
                "null_tail_only is only used by the top-up step; "
                "cursor must be None"
            )
        clauses.append("COALESCE(m.internal_date, m.date_sent) IS NULL")
    elif cursor is not None:
        if cursor.ts is None:
            clauses.append("COALESCE(m.internal_date, m.date_sent) IS NULL")
            clauses.append("m.id < %s")
            params.append(cursor.id)
        else:
            # Range-seekable dated keyset via SQL row comparison. Postgres
            # composes ``ROW(expr, id) < ROW(X, Y)`` as an Index Cond on
            # ``messages_recent_idx``, so the scan starts AT the cursor
            # and only emits matching rows — no per-tuple Filter, no
            # rows walked above the cursor. Equivalent to the explicit
            # disjunction ``expr < X OR (expr = X AND id < Y)``, but the
            # disjunction form degrades to a post-walk Filter at scale
            # (#75; the planner refuses to decompose mixed-column ORs
            # into an index range bound when an Index Scan alternative
            # is on the table).
            #
            # NULL rows are excluded naturally: ROW(NULL, _) < ROW(X, Y)
            # evaluates to UNKNOWN. They are reached via the top-up
            # mode above when the dated portion is exhausted.
            clauses.append(
                "ROW(COALESCE(m.internal_date, m.date_sent), m.id) < ROW(%s, %s)"
            )
            params.extend([cursor.ts, cursor.id])
    return " AND ".join(clauses), params
