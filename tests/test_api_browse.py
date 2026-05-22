"""Tests for localmail.api.browse.list_messages."""
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
import pytest

from localmail.api.browse import _build_where, list_messages
from localmail.api.browse_cursor import BrowseCursor, decode_browse_cursor
from localmail.api.errors import ValidationFailed


def _ensure_account(conn: psycopg.Connection, name: str = "a") -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method) "
            "VALUES (%s, %s, 'imap.x', 'password') RETURNING id",
            (name, f"{name}@y.test"),
        )
        row = cur.fetchone(); assert row is not None
        return int(row[0])


def _seed(
    conn: psycopg.Connection, *,
    account_id: int,
    suffix: str,
    internal_date: datetime | None = None,
    date_sent: datetime | None = None,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO messages (account_id, message_id, subject, raw_bytes,
                                     raw_sha256, size_bytes, headers, attachments,
                                     date_sent, internal_date, date_received)
               VALUES (%s, %s, 's', 'r', %s, 1, '{}'::jsonb, '[]'::jsonb,
                       %s, %s, now()) RETURNING id""",
            (account_id, f"<{suffix}@x>", bytes.fromhex(suffix * 32),
             date_sent, internal_date),
        )
        row = cur.fetchone(); assert row is not None
        return int(row[0])


def test_initial_page_returns_messages_in_recent_first_order(db_conn) -> None:
    aid = _ensure_account(db_conn)
    now = datetime.now(timezone.utc)
    m_old = _seed(db_conn, account_id=aid, suffix="aa",
                  internal_date=now - timedelta(days=2))
    m_mid = _seed(db_conn, account_id=aid, suffix="bb",
                  internal_date=now - timedelta(days=1))
    m_new = _seed(db_conn, account_id=aid, suffix="cc",
                  internal_date=now)
    db_conn.commit()

    out = list_messages(db_conn, allowed_account_ids=[aid], limit=10)
    ids = [int(m["message_id"]) for m in out["messages"]]
    assert ids == [m_new, m_mid, m_old]
    assert out["next_cursor"] is None  # pool exhausted, only 3 rows


def test_cursor_round_trip_paginates_strictly_older(db_conn) -> None:
    aid = _ensure_account(db_conn)
    now = datetime.now(timezone.utc)
    ids = [
        _seed(db_conn, account_id=aid, suffix=f"{i:02x}" * 1,
              internal_date=now - timedelta(hours=i))
        for i in range(5)
    ]
    # ids[0] is the newest (i=0), ids[4] is the oldest (i=4).
    db_conn.commit()

    page1 = list_messages(db_conn, allowed_account_ids=[aid], limit=2)
    assert [int(m["message_id"]) for m in page1["messages"]] == [ids[0], ids[1]]
    assert page1["next_cursor"] is not None

    page2 = list_messages(db_conn, allowed_account_ids=[aid], limit=2,
                          cursor=page1["next_cursor"])
    assert [int(m["message_id"]) for m in page2["messages"]] == [ids[2], ids[3]]
    assert page2["next_cursor"] is not None

    page3 = list_messages(db_conn, allowed_account_ids=[aid], limit=2,
                          cursor=page2["next_cursor"])
    assert [int(m["message_id"]) for m in page3["messages"]] == [ids[4]]
    assert page3["next_cursor"] is None


def test_wire_date_reflects_internal_date_when_set(db_conn) -> None:
    """The wire `date` field must match the sort key — i.e.
    ``COALESCE(internal_date, date_sent)``. Showing only the header
    ``Date:`` value while sorting by INTERNALDATE makes the displayed
    dates look out of order whenever the two differ.
    """
    aid = _ensure_account(db_conn)
    header_date = datetime(2022, 1, 1, tzinfo=timezone.utc)
    arrived = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
    _seed(db_conn, account_id=aid, suffix="aa",
          date_sent=header_date, internal_date=arrived)
    db_conn.commit()

    out = list_messages(db_conn, allowed_account_ids=[aid], limit=10)
    assert datetime.fromisoformat(out["messages"][0]["date"]) == arrived


def test_wire_date_falls_back_to_date_sent_when_internal_date_null(db_conn) -> None:
    """Legacy/un-backfilled rows have NULL internal_date; the wire `date`
    must fall back to ``date_sent`` so they're not displayed as null."""
    aid = _ensure_account(db_conn)
    header_date = datetime(2022, 1, 1, tzinfo=timezone.utc)
    _seed(db_conn, account_id=aid, suffix="bb",
          date_sent=header_date, internal_date=None)
    db_conn.commit()

    out = list_messages(db_conn, allowed_account_ids=[aid], limit=10)
    assert datetime.fromisoformat(out["messages"][0]["date"]) == header_date


def test_tied_internal_date_paginates_by_id_desc(db_conn) -> None:
    aid = _ensure_account(db_conn)
    ts = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
    a = _seed(db_conn, account_id=aid, suffix="aa", internal_date=ts)
    b = _seed(db_conn, account_id=aid, suffix="bb", internal_date=ts)
    c = _seed(db_conn, account_id=aid, suffix="cc", internal_date=ts)
    db_conn.commit()

    p1 = list_messages(db_conn, allowed_account_ids=[aid], limit=2)
    assert [int(m["message_id"]) for m in p1["messages"]] == [c, b]
    p2 = list_messages(db_conn, allowed_account_ids=[aid], limit=2,
                       cursor=p1["next_cursor"])
    assert [int(m["message_id"]) for m in p2["messages"]] == [a]


def test_empty_allowed_account_ids_returns_empty_page(db_conn) -> None:
    out = list_messages(db_conn, allowed_account_ids=[], limit=10)
    assert out == {"messages": [], "next_cursor": None}


def test_malformed_cursor_raises_validation_failed(db_conn) -> None:
    with pytest.raises(ValidationFailed):
        list_messages(db_conn, allowed_account_ids=[1], limit=10,
                      cursor="not-a-cursor")


def test_null_date_rows_paginate_after_dated_rows(db_conn) -> None:
    aid = _ensure_account(db_conn)
    now = datetime.now(timezone.utc)
    dated = _seed(db_conn, account_id=aid, suffix="aa",
                  internal_date=now - timedelta(hours=1))
    nul_a = _seed(db_conn, account_id=aid, suffix="bb")  # both dates NULL
    nul_b = _seed(db_conn, account_id=aid, suffix="cc")
    db_conn.commit()

    # Dated row first; NULL rows tail in id DESC (so nul_b before nul_a).
    p1 = list_messages(db_conn, allowed_account_ids=[aid], limit=1)
    assert [int(m["message_id"]) for m in p1["messages"]] == [dated]

    p2 = list_messages(db_conn, allowed_account_ids=[aid], limit=1,
                       cursor=p1["next_cursor"])
    assert [int(m["message_id"]) for m in p2["messages"]] == [nul_b]

    p3 = list_messages(db_conn, allowed_account_ids=[aid], limit=1,
                       cursor=p2["next_cursor"])
    assert [int(m["message_id"]) for m in p3["messages"]] == [nul_a]
    assert p3["next_cursor"] is None


def test_account_ids_filter_is_intersected_with_acl(db_conn) -> None:
    aid1 = _ensure_account(db_conn, name="alpha")
    aid2 = _ensure_account(db_conn, name="beta")
    now = datetime.now(timezone.utc)
    m1 = _seed(db_conn, account_id=aid1, suffix="aa", internal_date=now)
    _seed(db_conn, account_id=aid2, suffix="bb", internal_date=now)
    db_conn.commit()

    # Caller asks for both accounts but is only granted aid1.
    out = list_messages(db_conn, allowed_account_ids=[aid1],
                        account_ids=[aid1, aid2], limit=10)
    ids = [int(m["message_id"]) for m in out["messages"]]
    assert ids == [m1]


def test_account_ids_intersection_empty_short_circuits(db_conn) -> None:
    aid_granted = _ensure_account(db_conn, name="alpha")
    aid_other = _ensure_account(db_conn, name="beta")
    now = datetime.now(timezone.utc)
    _seed(db_conn, account_id=aid_granted, suffix="aa", internal_date=now)
    _seed(db_conn, account_id=aid_other, suffix="bb", internal_date=now)
    db_conn.commit()

    out = list_messages(db_conn, allowed_account_ids=[aid_granted],
                        account_ids=[aid_other], limit=10)
    assert out == {"messages": [], "next_cursor": None}


def test_dated_cursor_full_page_does_not_query_null_tail(db_conn) -> None:
    """When the dated portion has more rows past the cursor than the
    requested page, the second NULL-tail top-up query must not fire.

    Verified by asserting that NULL rows seeded *behind* the dated
    cursor never appear in the page — the dated-only predicate
    excludes them — and the page is full from dated rows alone.
    """
    aid = _ensure_account(db_conn)
    now = datetime.now(timezone.utc)
    ids = [
        _seed(db_conn, account_id=aid, suffix=f"d{i:01x}",
              internal_date=now - timedelta(hours=i))
        for i in range(5)
    ]
    _seed(db_conn, account_id=aid, suffix="e0")  # NULL row
    _seed(db_conn, account_id=aid, suffix="e1")  # NULL row
    db_conn.commit()

    p1 = list_messages(db_conn, allowed_account_ids=[aid], limit=2)
    assert [int(m["message_id"]) for m in p1["messages"]] == [ids[0], ids[1]]

    p2 = list_messages(db_conn, allowed_account_ids=[aid], limit=2,
                       cursor=p1["next_cursor"])
    assert [int(m["message_id"]) for m in p2["messages"]] == [ids[2], ids[3]]


def test_dated_cursor_exhausted_tops_up_from_null_tail_in_one_page(
    db_conn,
) -> None:
    """If the dated portion runs out partway through a page, the
    remaining slots must be filled from the NULL-tail in the same
    response — otherwise the user sees a short page when a full one
    was available.
    """
    aid = _ensure_account(db_conn)
    now = datetime.now(timezone.utc)
    d0 = _seed(db_conn, account_id=aid, suffix="aa",
               internal_date=now - timedelta(hours=1))
    d1 = _seed(db_conn, account_id=aid, suffix="bb",
               internal_date=now - timedelta(hours=2))
    d2 = _seed(db_conn, account_id=aid, suffix="cc",
               internal_date=now - timedelta(hours=3))
    n0 = _seed(db_conn, account_id=aid, suffix="dd")  # both dates NULL
    n1 = _seed(db_conn, account_id=aid, suffix="ee")
    db_conn.commit()

    p1 = list_messages(db_conn, allowed_account_ids=[aid], limit=2)
    assert [int(m["message_id"]) for m in p1["messages"]] == [d0, d1]
    p2 = list_messages(db_conn, allowed_account_ids=[aid], limit=3,
                       cursor=p1["next_cursor"])
    # Only one dated row (d2) is past the cursor; the remaining two
    # slots must come from the NULL-tail (highest-id first).
    assert [int(m["message_id"]) for m in p2["messages"]] == [d2, n1, n0]
    assert p2["next_cursor"] is None


def test_dated_cursor_at_boundary_returns_null_tail_only_page(db_conn) -> None:
    """A dated cursor pointing at the *last* dated row must page into
    the NULL-tail on the next request — i.e. the page is filled
    entirely from the NULL-tail and the response cursor is the
    NULL-tail flavor (``ts=None``).
    """
    aid = _ensure_account(db_conn)
    now = datetime.now(timezone.utc)
    last_dated = _seed(db_conn, account_id=aid, suffix="aa",
                       internal_date=now - timedelta(hours=1))
    n0 = _seed(db_conn, account_id=aid, suffix="bb")
    n1 = _seed(db_conn, account_id=aid, suffix="cc")
    n2 = _seed(db_conn, account_id=aid, suffix="dd")
    db_conn.commit()

    p1 = list_messages(db_conn, allowed_account_ids=[aid], limit=1)
    assert [int(m["message_id"]) for m in p1["messages"]] == [last_dated]
    assert p1["next_cursor"] is not None

    p2 = list_messages(db_conn, allowed_account_ids=[aid], limit=2,
                       cursor=p1["next_cursor"])
    assert [int(m["message_id"]) for m in p2["messages"]] == [n2, n1]
    assert p2["next_cursor"] is not None
    # The next cursor must now be the NULL-tail flavor so the next page
    # uses the NULL-tail SQL path, not the dated one.
    assert decode_browse_cursor(p2["next_cursor"]).ts is None

    p3 = list_messages(db_conn, allowed_account_ids=[aid], limit=2,
                       cursor=p2["next_cursor"])
    assert [int(m["message_id"]) for m in p3["messages"]] == [n0]
    assert p3["next_cursor"] is None


def test_folder_ids_filter_restricts_to_labelled_messages(db_conn) -> None:
    aid = _ensure_account(db_conn)
    now = datetime.now(timezone.utc)
    m_in = _seed(db_conn, account_id=aid, suffix="aa", internal_date=now)
    m_out = _seed(db_conn, account_id=aid, suffix="bb", internal_date=now)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO mailboxes (account_id, name, uidvalidity) "
            "VALUES (%s, 'INBOX', 1) RETURNING id", (aid,),
        )
        row = cur.fetchone(); assert row is not None
        mb_id = int(row[0])
        cur.execute(
            "INSERT INTO message_labels (message_id, mailbox_id, uid) "
            "VALUES (%s, %s, 1)", (m_in, mb_id),
        )
    db_conn.commit()

    out = list_messages(db_conn, allowed_account_ids=[aid],
                        folder_ids=[mb_id], limit=10)
    ids = [int(m["message_id"]) for m in out["messages"]]
    assert ids == [m_in]
    assert m_out not in ids


# ---- Pure-function tests for _build_where (#75 regression) ---------------

# These tests pin the WHERE-clause shape so a future refactor cannot
# silently re-introduce the ``OR COALESCE IS NULL`` disjunct that
# defeated the ``messages_recent_idx`` range bound (#75). The dated
# cursor predicate MUST be range-seekable; NULL-tail rows are reached
# via the top-up branch in ``list_messages``, not by widening the
# dated cursor predicate.

def test_build_where_initial_page_has_no_date_predicate() -> None:
    """Cursor=None: the WHERE clause is just the ACL filter so the
    initial-page query can stream the entire ``messages_recent_idx``
    walk (dated rows first, NULLs in the NULLS-LAST tail) via LIMIT.
    """
    where, params = _build_where(
        account_ids=[1, 2], folder_ids=None, cursor=None,
    )
    assert where == "m.account_id = ANY(%s)"
    assert params == [[1, 2]]


def test_build_where_dated_cursor_uses_row_comparison_not_or_disjunction() -> None:
    """Regression for #75: the dated-cursor predicate must use SQL
    row comparison (``ROW(expr, id) < ROW(%s, %s)``), not an
    ``expr < X OR (expr = X AND id < Y)`` disjunction.

    Postgres composes the ROW form as an Index Cond on the
    ``messages_recent_idx`` expression (range-bounded scan starting
    AT the cursor), but treats the equivalent OR form as a post-walk
    Filter — so the OR form walks from the top of the index
    downward, filtering ~N/2 tuples above the cursor on every
    mid-keyset request. Empirically measured 3.7ms vs 0.022ms on a
    50k-row archive.
    """
    ts = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
    where, params = _build_where(
        account_ids=[1], folder_ids=None,
        cursor=BrowseCursor(ts=ts, id=42),
    )
    assert "IS NULL" not in where, (
        f"dated cursor predicate must exclude NULL rows so it's a "
        f"pure range bound; NULL-tail rows are reached via the "
        f"null_tail_only top-up. Got: {where!r}"
    )
    assert "ROW(COALESCE(m.internal_date, m.date_sent), m.id) < ROW(%s, %s)" in where, (
        f"dated cursor predicate must be the SQL row comparison form. "
        f"Got: {where!r}"
    )
    assert " OR " not in where.upper(), (
        f"OR-form dated cursor predicate degrades to a post-walk Filter "
        f"at production scale (#75). Got: {where!r}"
    )
    # Row comparison emits exactly two cursor params (ts, id), not three.
    assert params == [[1], ts, 42]


def test_build_where_null_tail_cursor_uses_id_keyset() -> None:
    """Cursor with ``ts=None`` (already in NULL-tail) uses the
    ``IS NULL AND id < cursor.id`` predicate so subsequent NULL-tail
    pages step strictly by id."""
    where, params = _build_where(
        account_ids=[1], folder_ids=None,
        cursor=BrowseCursor(ts=None, id=99),
    )
    assert "COALESCE(m.internal_date, m.date_sent) IS NULL" in where
    assert "m.id < %s" in where
    assert params == [[1], 99]


def test_build_where_null_tail_topup_has_no_id_predicate() -> None:
    """The top-up step (``null_tail_only=True``, cursor=None) selects
    the head of the NULL-tail — i.e. all NULL rows ordered by id DESC,
    no id lower bound. Used by ``list_messages`` after the dated path
    is exhausted past a dated cursor.
    """
    where, params = _build_where(
        account_ids=[1, 2], folder_ids=None,
        cursor=None, null_tail_only=True,
    )
    assert "COALESCE(m.internal_date, m.date_sent) IS NULL" in where
    assert "m.id < %s" not in where
    assert params == [[1, 2]]


def test_build_where_folder_clause_added_for_all_modes() -> None:
    """``folder_ids`` adds the ``message_labels`` clause regardless of
    cursor mode — verified explicitly so a refactor doesn't drop it
    for the new NULL-tail top-up branch."""
    ts = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
    modes: list[tuple[BrowseCursor | None, bool]] = [
        (None, False),
        (BrowseCursor(ts=ts, id=1), False),
        (BrowseCursor(ts=None, id=1), False),
        (None, True),
    ]
    for cur, null_only in modes:
        where, _ = _build_where(
            account_ids=[1], folder_ids=[7],
            cursor=cur, null_tail_only=null_only,
        )
        assert "ml.mailbox_id = ANY(%s)" in where, (
            f"folder clause missing for cursor={cur!r} null_tail_only={null_only}"
        )


def test_build_where_null_tail_only_with_cursor_raises_value_error() -> None:
    """``null_tail_only=True`` is the top-up branch and is only ever called
    with ``cursor=None``. Passing a cursor is a programming error and must
    raise ``ValueError`` (not a silent ``assert`` that vanishes under
    ``python -O``)."""
    ts = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="null_tail_only"):
        _build_where(
            account_ids=[1], folder_ids=None,
            cursor=BrowseCursor(ts=ts, id=1), null_tail_only=True,
        )


# ---- Query-count contract for the NULL-tail top-up (#75 follow-up) ------

# The top-up branch in ``list_messages`` must fire exactly once on the
# dated→NULL transition and stay quiet on the common case (cursor inside
# dated portion, full page). End-to-end behavioural tests above pin the
# *results*; these tests pin the *cost* so a refactor that hoisted the
# top-up outside the conditional would double the query count silently.

class _CountingCursor:
    """Wraps a real psycopg cursor and increments a shared counter on each
    ``execute()``. Forwards everything else."""

    def __init__(self, inner: Any, counter: list[int]) -> None:
        self._inner = inner
        self._counter = counter

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        self._counter[0] += 1
        return self._inner.execute(*args, **kwargs)

    def __enter__(self) -> "_CountingCursor":
        self._inner.__enter__()
        return self

    def __exit__(self, *exc: Any) -> Any:
        return self._inner.__exit__(*exc)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _CountingConn:
    """Wraps a real psycopg connection and exposes ``execute_count`` via the
    shared counter. ``cursor()`` returns a _CountingCursor."""

    def __init__(self, inner: psycopg.Connection) -> None:
        self._inner = inner
        self.execute_count: list[int] = [0]

    def cursor(self, *args: Any, **kwargs: Any) -> _CountingCursor:
        return _CountingCursor(self._inner.cursor(*args, **kwargs),
                               self.execute_count)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def test_list_messages_runs_one_query_when_dated_page_is_full(db_conn) -> None:
    """Common case: cursor inside dated portion with more dated rows past
    it than the page can hold. The top-up branch must NOT fire — exactly
    one row-fetching query per call. Regression guard against a future
    refactor that runs the top-up unconditionally."""
    aid = _ensure_account(db_conn)
    now = datetime.now(timezone.utc)
    ids = [
        _seed(db_conn, account_id=aid, suffix=f"d{i:01x}",
              internal_date=now - timedelta(hours=i))
        for i in range(5)
    ]
    _seed(db_conn, account_id=aid, suffix="e0")  # NULL row (must stay untouched)
    db_conn.commit()

    p1 = list_messages(db_conn, allowed_account_ids=[aid], limit=2)
    assert [int(m["message_id"]) for m in p1["messages"]] == [ids[0], ids[1]]

    counting = _CountingConn(db_conn)
    p2 = list_messages(counting, allowed_account_ids=[aid], limit=2,  # type: ignore[arg-type]
                       cursor=p1["next_cursor"])
    assert [int(m["message_id"]) for m in p2["messages"]] == [ids[2], ids[3]]
    assert counting.execute_count[0] == 1, (
        f"top-up branch fired unnecessarily: "
        f"expected 1 query, got {counting.execute_count[0]}"
    )


def test_list_messages_runs_two_queries_on_dated_to_null_transition(
    db_conn,
) -> None:
    """Boundary case: dated portion runs short past the cursor. The top-up
    branch must fire exactly once to fill the page from the NULL-tail —
    so the total is exactly two row-fetching queries."""
    aid = _ensure_account(db_conn)
    now = datetime.now(timezone.utc)
    d0 = _seed(db_conn, account_id=aid, suffix="aa",
               internal_date=now - timedelta(hours=1))
    d1 = _seed(db_conn, account_id=aid, suffix="bb",
               internal_date=now - timedelta(hours=2))
    d2 = _seed(db_conn, account_id=aid, suffix="cc",
               internal_date=now - timedelta(hours=3))
    n0 = _seed(db_conn, account_id=aid, suffix="dd")
    n1 = _seed(db_conn, account_id=aid, suffix="ee")
    db_conn.commit()

    p1 = list_messages(db_conn, allowed_account_ids=[aid], limit=2)
    assert [int(m["message_id"]) for m in p1["messages"]] == [d0, d1]

    counting = _CountingConn(db_conn)
    p2 = list_messages(counting, allowed_account_ids=[aid], limit=3,  # type: ignore[arg-type]
                       cursor=p1["next_cursor"])
    assert [int(m["message_id"]) for m in p2["messages"]] == [d2, n1, n0]
    assert counting.execute_count[0] == 2, (
        f"expected 2 queries (dated + NULL-tail top-up), "
        f"got {counting.execute_count[0]}"
    )


def test_list_messages_runs_one_query_for_initial_page(db_conn) -> None:
    """Initial page (cursor=None): the unrestricted query streams dated
    rows first and NULL rows in the NULLS-LAST tail via LIMIT. The top-up
    branch must NOT fire — `cursor is None` short-circuits it. Pinning
    this prevents a future refactor from making the top-up unconditional
    on the assumption that "we always want to fill the page"."""
    aid = _ensure_account(db_conn)
    _seed(db_conn, account_id=aid, suffix="aa",
          internal_date=datetime.now(timezone.utc))
    _seed(db_conn, account_id=aid, suffix="bb")  # NULL row
    db_conn.commit()

    counting = _CountingConn(db_conn)
    out = list_messages(counting, allowed_account_ids=[aid], limit=10)  # type: ignore[arg-type]
    assert len(out["messages"]) == 2
    assert counting.execute_count[0] == 1, (
        f"expected 1 query on initial page, got {counting.execute_count[0]}"
    )


def test_list_messages_runs_one_query_for_null_tail_cursor(db_conn) -> None:
    """When the cursor is already in the NULL-tail (``cursor.ts is None``),
    the predicate is ``IS NULL AND id < %s`` and there is nothing "below"
    the NULL-tail to top up from. The top-up branch must NOT fire."""
    aid = _ensure_account(db_conn)
    # Hex-only suffixes; _seed uses bytes.fromhex(suffix * 32).
    nulls = [
        _seed(db_conn, account_id=aid, suffix=f"a{i:01x}")
        for i in range(4)
    ]
    db_conn.commit()

    p1 = list_messages(db_conn, allowed_account_ids=[aid], limit=2)
    assert decode_browse_cursor(p1["next_cursor"]).ts is None

    counting = _CountingConn(db_conn)
    p2 = list_messages(counting, allowed_account_ids=[aid], limit=10,  # type: ignore[arg-type]
                       cursor=p1["next_cursor"])
    # Two NULL rows remain past the cursor (the first page consumed
    # nulls[3], nulls[2]).
    assert sorted(int(m["message_id"]) for m in p2["messages"]) == sorted(
        [nulls[0], nulls[1]]
    )
    assert counting.execute_count[0] == 1, (
        f"top-up fired on a NULL-tail cursor: "
        f"got {counting.execute_count[0]} queries"
    )
