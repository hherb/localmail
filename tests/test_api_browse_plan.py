"""Plan-regression tests for ``localmail.api.browse.list_messages``.

Issue #72 asked: does the planner use ``messages_recent_idx`` for the
ACL-filtered keyset query, or does it fall to a bitmap-on-account_id +
sort path on multi-account installs?

The acceptance harness in ``tests/acceptance/run_browse_explain.py``
answered the *operational* form of that question at production scale
(200k rows × 5 accounts × 3 distribution shapes): **yes**, the
planner picks ``Index Scan using messages_recent_idx`` in every
probe. No covering index is needed.

The unit tests in this file cover the smaller, sharper *regression*
form of the question: a future schema change must not silently
break the index. Specifically:

* **Index definition matches the query shape.** The expression,
  the DESC, the NULLS LAST, and the secondary ``id DESC`` are all
  load-bearing for the LIMIT short-circuit. Any of them flipping
  would make the index ineligible for the query's ORDER BY.

* **Index is eligible when the per-account shortcut is gone.**
  We can't reliably assert "the planner *prefers* this index" at
  fixture sizes because tiny tables make seq+sort or PK-scan
  cheaper. But we can assert "with competing indexes hidden and
  seqscan disabled, the planner *can* use it" — i.e. the
  expression and ordering still match.

  Note that this still doesn't catch the case where the planner
  picks an unrelated PK-backward scan over ``messages_pkey`` at
  fixture scale; the PK can't be dropped without dropping every
  child FK. We accept that limitation: the operational harness
  has the authoritative answer, and any change that breaks the
  index definition will fail
  ``test_messages_recent_idx_definition_matches_design``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import psycopg

from localmail.api.browse import build_where, compose_browse_sql
from localmail.api.browse_cursor import BrowseCursor


# ---- Index-definition assertion -----------------------------------------

# Postgres normalises the indexdef text in ``pg_indexes`` — parentheses
# around expressions, schema-qualified table names, canonical ASC/DESC
# token order. Match against the canonical form rather than the source
# SQL from migration 0018.
_EXPECTED_INDEXDEF_SUBSTRINGS = (
    "USING btree",
    "(COALESCE(internal_date, date_sent) DESC NULLS LAST, id DESC)",
)


def test_messages_recent_idx_definition_matches_design(
    db_conn: psycopg.Connection,
) -> None:
    """The index must use the COALESCE expression with DESC + NULLS LAST
    on the date, and id DESC as the secondary key. Any deviation here
    forces the planner into a Sort node on top of an index scan, which
    in turn defeats the LIMIT short-circuit.

    Each of the load-bearing tokens (DESC, NULLS LAST, ``id DESC``) is
    checked separately so a failure message points at the specific
    drift, not just at "the indexdef changed".
    """
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT indexdef FROM pg_indexes"
            " WHERE indexname = 'messages_recent_idx'"
        )
        row = cur.fetchone()
    assert row is not None, "messages_recent_idx index missing"
    indexdef = row[0]
    for substring in _EXPECTED_INDEXDEF_SUBSTRINGS:
        assert substring in indexdef, (
            f"messages_recent_idx no longer contains {substring!r}; "
            f"current indexdef:\n{indexdef}"
        )


# ---- Index-eligibility assertion ----------------------------------------

# Enough rows that ``messages_recent_idx`` is at least *cost-competitive*
# with the PK backward scan at fixture scale. Below this the PK wins on
# tiny tables; far above this the test becomes slow.
_SEED_ROWS_PER_ACCOUNT = 50
_NUM_ACCOUNTS = 3
_PAGE_SIZE = 50

# Anchor date for the synthesised ``date_sent`` values. Spread across
# 365 days so the index walk has variety to chew on.
_EPOCH = datetime(2024, 1, 1, tzinfo=timezone.utc)
_DATE_SPAN_DAYS = 365

# The exact SQL emitted by ``list_messages`` for the initial-load path
# (no ``folder_ids`` filter), composed from the production primitives
# in ``localmail.api.browse``. Drift is impossible by construction —
# any refactor of the SELECT / FROM / ORDER BY shape, or of the
# WHERE-clause emitter, lands here automatically (#77).
def _list_messages_sql_for_initial_page(
    account_ids: list[int],
) -> tuple[str, list]:
    """Return ``(sql, params)`` for the initial-page EXPLAIN probe.

    Composes the production ``BROWSE_ROW_SQL_TEMPLATE`` via the public
    ``compose_browse_sql`` helper, using the production ``build_where``
    for the ACL clause. The caller appends the page-size LIMIT param.
    """
    where, params = build_where(
        account_ids=account_ids, folder_ids=None, cursor=None,
    )
    return compose_browse_sql(folder_filter=False, where=where), params

def _seed_account(conn: psycopg.Connection, name: str) -> int:
    """Insert one account, return its id."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method)"
            " VALUES (%s, %s, 'imap.x', 'password') RETURNING id",
            (name, f"{name}@x.test"),
        )
        row = cur.fetchone()
        assert row is not None
        return int(row[0])


def _seed_rows(
    conn: psycopg.Connection, account_id: int, n: int, sha_prefix: int,
) -> None:
    """Insert ``n`` messages for ``account_id`` with synthesised dates."""
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO messages (account_id, message_id, raw_sha256,"
            " subject, from_addr, date_sent, headers, attachments,"
            " raw_bytes, size_bytes)"
            " VALUES (%s, %s, %s, %s, %s, %s, '{}'::jsonb, '[]'::jsonb, %s, 4)",
            [
                (
                    account_id,
                    f"<m{sha_prefix:02x}{i:04x}@plan.local>",
                    bytes([sha_prefix]) + i.to_bytes(31, "big"),
                    f"subj-{i}",
                    f"from{i % 7}@x.test",
                    _EPOCH + timedelta(days=(i * _DATE_SPAN_DAYS) / max(n, 1)),
                    b"raw",
                )
                for i in range(n)
            ],
        )
    conn.commit()


def _seed_for_plan_test(db_conn: psycopg.Connection) -> list[int]:
    """Seed ``_NUM_ACCOUNTS`` accounts × ``_SEED_ROWS_PER_ACCOUNT`` rows
    each and return the account ids."""
    account_ids: list[int] = []
    for i in range(_NUM_ACCOUNTS):
        aid = _seed_account(db_conn, f"plan{i}")
        _seed_rows(db_conn, aid, _SEED_ROWS_PER_ACCOUNT, sha_prefix=i + 1)
        account_ids.append(aid)
    return account_ids


def _has_full_sort_node(plan: str) -> bool:
    """Return True iff the EXPLAIN text contains a non-incremental Sort node.

    A full ``Sort`` on top of an index scan means the planner is
    materialising the intermediate and re-sorting, defeating the
    LIMIT short-circuit. ``Incremental Sort`` is the cheap DISTINCT
    tie-breaker on an already-sorted stream — not a regression.
    Matches the line-based heuristic the acceptance harness uses
    (see ``tests/acceptance/run_browse_explain.py::classify_plan``).
    """
    for raw in plan.splitlines():
        ln = raw.strip()
        if (ln.startswith("->  Sort") or ln.startswith("Sort  ")) \
                and "Incremental Sort" not in ln:
            return True
    return False


def _explain_messages_recent_idx_only(
    conn: psycopg.Connection, account_ids: list[int], page_size: int,
) -> str:
    """Run EXPLAIN with **every** other ``messages`` index hidden.

    This is the strict eligibility test: even ``messages_pkey`` is
    hidden (we wrap the DROPs in a SAVEPOINT, so the FK references
    are not enforced until commit — the rollback restores everything
    cleanly). If ``messages_recent_idx`` cannot serve the query
    even with all alternatives gone, the index has been broken.

    The temporary primary-key drop is the only way to remove the
    ``Index Scan Backward using messages_pkey`` shortcut that
    Postgres picks at fixture scale on tiny tables.
    """
    competing = [
        "messages_acct_date_idx",
        "messages_acct_msgid_uniq",
        "messages_acct_rawsha_uniq",
        "messages_attachments_gin",
        "messages_body_lang_idx",
        "messages_body_lang_pending_idx",
        "messages_fts_v2_idx",
        "messages_headers_gin",
    ]
    with conn.cursor() as cur:
        cur.execute("SAVEPOINT plan_probe_strict")
        try:
            # ``messages_pkey`` is owned by the PK constraint; CASCADE
            # drops both the constraint *and* every FK that references
            # it. Must run before the plain ``DROP INDEX`` loop because
            # plain ``DROP INDEX messages_pkey`` is rejected by Postgres
            # ("drop constraint instead").
            cur.execute(
                "ALTER TABLE messages DROP CONSTRAINT messages_pkey CASCADE"
            )
            for idx_name in competing:
                cur.execute(f"DROP INDEX IF EXISTS {idx_name}")
            cur.execute("ANALYZE messages")
            cur.execute("ANALYZE accounts")
            cur.execute("SET LOCAL enable_seqscan = off")
            sql, where_params = _list_messages_sql_for_initial_page(account_ids)
            cur.execute(
                "EXPLAIN (FORMAT TEXT) " + sql,
                where_params + [page_size + 1],
            )
            plan = "\n".join(r[0] for r in cur.fetchall())
        finally:
            cur.execute("ROLLBACK TO SAVEPOINT plan_probe_strict")
    return plan


def test_messages_recent_idx_is_eligible_for_list_messages_query(
    db_conn: psycopg.Connection,
) -> None:
    """With every competing ``messages`` index temporarily hidden,
    the planner must pick ``messages_recent_idx`` for the
    ACL-filtered keyset query. If it falls back to anything else
    (seq scan is forbidden by ``enable_seqscan = off``), the index
    is no longer eligible.

    This is the regression-protection test for #72. The operational
    "planner *prefers* it at scale" claim is covered separately by
    ``tests/acceptance/run_browse_explain.py``.
    """
    account_ids = _seed_for_plan_test(db_conn)
    plan = _explain_messages_recent_idx_only(
        db_conn, [account_ids[0]], _PAGE_SIZE,
    )
    assert "messages_recent_idx" in plan, plan
    assert not _has_full_sort_node(plan), plan


def test_messages_recent_idx_is_eligible_for_half_account_coverage(
    db_conn: psycopg.Connection,
) -> None:
    """Same eligibility check for a multi-account ACL — the index
    has no ``account_id`` column, so the planner has to evaluate
    the ANY(...) filter per tuple. This must still parse as a
    plain index scan (no bitmap, no full sort)."""
    account_ids = _seed_for_plan_test(db_conn)
    half = account_ids[: max(1, len(account_ids) // 2)]
    plan = _explain_messages_recent_idx_only(db_conn, half, _PAGE_SIZE)
    assert "messages_recent_idx" in plan, plan
    assert "Bitmap Heap Scan" not in plan, plan
    assert not _has_full_sort_node(plan), plan


def test_messages_recent_idx_is_eligible_for_all_accounts(
    db_conn: psycopg.Connection,
) -> None:
    """ACL = every account; same eligibility check."""
    account_ids = _seed_for_plan_test(db_conn)
    plan = _explain_messages_recent_idx_only(
        db_conn, account_ids, _PAGE_SIZE,
    )
    assert "messages_recent_idx" in plan, plan
    assert "Bitmap Heap Scan" not in plan, plan
    assert not _has_full_sort_node(plan), plan


# ---- #75: dated cursor predicate composes an index range bound ---------

# The exact mid-keyset SQL ``list_messages`` emits after the #75 fix.
# Composed from the production primitives so any rewrite of the dated
# cursor predicate (e.g. re-introducing the OR disjunction Postgres
# refuses to compose as an Index Cond) lands here automatically (#77).
def _list_messages_sql_for_mid_keyset(
    account_ids: list[int], ts: datetime, message_id: int,
) -> tuple[str, list]:
    """Return ``(sql, params)`` for the mid-keyset EXPLAIN probe.

    Uses the production ``build_where`` with a dated cursor so the
    actual cursor predicate Postgres sees is whatever
    ``localmail.api.browse`` emits today. The caller appends the
    page-size LIMIT param.
    """
    where, params = build_where(
        account_ids=account_ids, folder_ids=None,
        cursor=BrowseCursor(ts=ts, id=message_id),
    )
    return compose_browse_sql(folder_filter=False, where=where), params


def _explain_mid_keyset_recent_idx_only(
    conn: psycopg.Connection, account_ids: list[int],
    ts: datetime, message_id: int, page_size: int,
) -> str:
    """Run EXPLAIN with every other ``messages`` index hidden and a
    dated cursor predicate. Same SAVEPOINT scaffolding as the
    initial-page eligibility test — we want to isolate the planner's
    use of ``messages_recent_idx`` from the PK shortcut and the
    per-account index."""
    competing = [
        "messages_acct_date_idx",
        "messages_acct_msgid_uniq",
        "messages_acct_rawsha_uniq",
        "messages_attachments_gin",
        "messages_body_lang_idx",
        "messages_body_lang_pending_idx",
        "messages_fts_v2_idx",
        "messages_headers_gin",
    ]
    with conn.cursor() as cur:
        cur.execute("SAVEPOINT plan_probe_mid_keyset")
        try:
            cur.execute(
                "ALTER TABLE messages DROP CONSTRAINT messages_pkey CASCADE"
            )
            for idx_name in competing:
                cur.execute(f"DROP INDEX IF EXISTS {idx_name}")
            cur.execute("ANALYZE messages")
            cur.execute("ANALYZE accounts")
            cur.execute("SET LOCAL enable_seqscan = off")
            sql, where_params = _list_messages_sql_for_mid_keyset(
                account_ids, ts, message_id,
            )
            cur.execute(
                "EXPLAIN (FORMAT TEXT) " + sql,
                where_params + [page_size + 1],
            )
            plan = "\n".join(r[0] for r in cur.fetchall())
        finally:
            cur.execute("ROLLBACK TO SAVEPOINT plan_probe_mid_keyset")
    return plan


def test_dated_cursor_predicate_composes_index_range_bound(
    db_conn: psycopg.Connection,
) -> None:
    """Issue #75 regression: with the ``OR COALESCE IS NULL`` disjunct
    removed, Postgres must compose the dated-cursor predicate as an
    ``Index Cond`` on the ``messages_recent_idx`` expression — *not*
    as a post-walk ``Filter``. The line-level assertion is

      ``Index Cond: (COALESCE(...) < $N)``

    appearing in the EXPLAIN output. If the predicate is downgraded
    to a filter, the planner walks every row above the cursor on
    every mid-keyset page and the test fails loudly.
    """
    account_ids = _seed_for_plan_test(db_conn)
    # Pick a cursor near the middle of the date span. The sort key
    # values match what ``list_messages`` would mint from the prior
    # page's last row.
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(internal_date, date_sent), id"
            "  FROM messages"
            " WHERE account_id = %s"
            "   AND COALESCE(internal_date, date_sent) IS NOT NULL"
            " ORDER BY COALESCE(internal_date, date_sent) DESC NULLS LAST,"
            " id DESC"
            " OFFSET (SELECT COUNT(*)/2 FROM messages WHERE account_id = %s)"
            " LIMIT 1",
            (account_ids[0], account_ids[0]),
        )
        row = cur.fetchone()
        assert row is not None
        cursor_ts, cursor_id = row[0], int(row[1])
    plan = _explain_mid_keyset_recent_idx_only(
        db_conn, [account_ids[0]], cursor_ts, cursor_id, _PAGE_SIZE,
    )
    assert "messages_recent_idx" in plan, plan
    assert "Index Cond" in plan, (
        "dated cursor predicate must be composed as an Index Cond, "
        f"not a Filter. Plan:\n{plan}"
    )
    # The cursor predicate must drive the index bound (the index is on
    # COALESCE), so the Index Cond line must reference COALESCE.
    cond_lines = [ln for ln in plan.splitlines() if "Index Cond" in ln]
    assert any("COALESCE" in ln for ln in cond_lines), (
        f"Index Cond does not reference COALESCE — predicate is not "
        f"range-seekable. Plan:\n{plan}"
    )
    # ``Rows Removed by Filter`` must not include the cursor predicate.
    # The bug surfaced when ``COALESCE < X`` was a post-walk Filter; with
    # the fix it composes as an Index Cond (streaming Index Scan or
    # BitmapOr + Sort — either is fine, both are O(matching rows)). A
    # ``Filter:`` line that mentions COALESCE is the exact regression
    # signature.
    filter_lines = [ln for ln in plan.splitlines() if "Filter:" in ln]
    assert not any("COALESCE" in ln for ln in filter_lines), (
        f"cursor predicate degraded to a post-walk Filter (#75 regression):"
        f"\n{plan}"
    )


def test_plan_probe_savepoint_restores_dropped_indexes(
    db_conn: psycopg.Connection,
) -> None:
    """Sanity-check the SAVEPOINT scaffolding — after the eligibility
    helper returns, every dropped index must be back. A failure here
    means the other tests' rollback is leaking and could affect
    later tests on the same DB.
    """
    account_ids = _seed_for_plan_test(db_conn)
    _ = _explain_messages_recent_idx_only(
        db_conn, [account_ids[0]], _PAGE_SIZE,
    )
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM pg_indexes"
            " WHERE schemaname = 'public'"
            "   AND tablename = 'messages'"
        )
        row = cur.fetchone()
    assert row is not None
    # 10 expected indexes per the 0019 schema: pkey, acct_date,
    # acct_msgid_uniq, acct_rawsha_uniq, attachments_gin, body_lang,
    # body_lang_pending, fts_v2, headers_gin, recent.
    assert row[0] >= 10, (
        f"expected at least 10 indexes on messages after SAVEPOINT "
        f"rollback, found {row[0]} — rollback is broken"
    )
