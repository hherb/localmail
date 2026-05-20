"""Paginated message browse — service layer.

Mirrors the shape of the /v1/changes payload but supports keyset pagination
into the past instead of forward incremental polling. The wire cursor is
opaque (see browse_cursor.py); the ACL filter applies at the SQL boundary.
"""
from __future__ import annotations

from typing import Any

import psycopg

from localmail.api.browse_cursor import (
    BrowseCursor, decode_browse_cursor, encode_browse_cursor,
)


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
    where, params = _build_where(
        account_ids=effective_account_ids,
        folder_ids=folder_ids,
        cursor=parsed_cursor,
    )
    join = "JOIN message_labels ml ON ml.message_id = m.id " if folder_ids else ""

    # COALESCE(internal_date, date_sent) must appear in the SELECT list when
    # DISTINCT is used (Postgres requires ORDER BY expressions to be in the
    # select list for DISTINCT queries).  We alias it as sort_ts and use it
    # only for ordering; the caller still receives the raw date columns.
    sql = f"""
        SELECT DISTINCT m.id, m.subject, m.from_addr, m.from_name, m.date_sent,
                        m.internal_date, m.account_id, a.name,
                        COALESCE(m.internal_date, m.date_sent) AS sort_ts
          FROM messages m
          JOIN accounts a ON a.id = m.account_id
          {join}
         WHERE {where}
         ORDER BY sort_ts DESC NULLS LAST, m.id DESC
         LIMIT %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, params + [fetch_limit])
        rows = cur.fetchall()

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
        last = page_rows[-1]
        _, _, _, _, last_date_sent, last_internal_date, _, _, _ = last
        last_mid = last[0]
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


def _build_where(
    *,
    account_ids: list[int],
    folder_ids: list[int] | None,
    cursor: BrowseCursor | None,
) -> tuple[str, list[Any]]:
    clauses = ["m.account_id = ANY(%s)"]
    params: list[Any] = [account_ids]
    if folder_ids:
        clauses.append("ml.mailbox_id = ANY(%s)")
        params.append(folder_ids)
    if cursor is not None:
        if cursor.ts is None:
            # Already in the NULL-date tail.
            clauses.append(
                "COALESCE(m.internal_date, m.date_sent) IS NULL AND m.id < %s"
            )
            params.append(cursor.id)
        else:
            # Still in the dated portion: tuple keyset, plus NULLs are
            # already strictly "later" in NULLS-LAST order.
            clauses.append(
                "(COALESCE(m.internal_date, m.date_sent) < %s "
                " OR (COALESCE(m.internal_date, m.date_sent) = %s AND m.id < %s) "
                " OR COALESCE(m.internal_date, m.date_sent) IS NULL)"
            )
            params.extend([cursor.ts, cursor.ts, cursor.id])
    return " AND ".join(clauses), params
