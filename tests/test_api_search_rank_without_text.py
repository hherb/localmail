# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The api boundary refuses ``sort="rank"`` on a textless query (#324).

Two guards, one rule. This one answers before any work is done and — as
this cluster keeps re-learning — **ahead of the empty-ACL short-circuit**,
which returns an empty page byte-identical to "you have reached the end":
a grant-nothing caller must not be told a contradictory request succeeded.
The Searcher's own guard covers the CLI and library callers who never reach
here. The two used to read different free text across an open quote, which
made the Searcher's refusal reachable from the wire; since #367 composes the
filters ahead of the free text they agree, and ``run_search``'s mapping of
that refusal is a backstop again.

The round trip is what #324 actually is, so it is pinned as a round trip:
page 1 mints a cursor recording ``date``, and re-stating the ``rank`` that
page 1 accepted was a 400 on page 2. Under the fix neither page accepts it.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from localmail.api.errors import ValidationFailed
from localmail.api.search import run_search
from localmail.api.search_cursor import CursorPlan, resolve_cursor_plan
from localmail.search.searcher import KeysetCursor


#: Non-empty request fields that ``parse_query`` reduces to no free text.
#: The client has no cue these are "blank" — which is the whole reason the
#: predicate is applied post-parse, and the trap the #308 follow-up hit.
OPERATOR_ONLY = ("subject:invoice", "has:attachment", "lang:en",
                 'from:"alice@example.com"')


# --- the pure plan resolver ------------------------------------------------

def test_a_fresh_textless_request_resolves_to_date_not_rank() -> None:
    """It used to resolve to ``DEFAULT_SORT``, which is the ordering the
    request would never be served in — and then the cursor it minted
    contradicted it."""
    plan = resolve_cursor_plan(cursor=None, requested_sort=None,
                               requested_sort_order=None, free_text="")
    assert plan == CursorPlan(mode="fresh", sort="date", sort_order="desc")


def test_a_fresh_request_with_free_text_still_defaults_to_rank() -> None:
    plan = resolve_cursor_plan(cursor=None, requested_sort=None,
                               requested_sort_order=None, free_text="invoice")
    assert plan == CursorPlan(mode="fresh", sort="rank", sort_order="desc")


def test_a_stated_rank_on_a_fresh_textless_request_is_refused() -> None:
    with pytest.raises(ValidationFailed, match="no free text"):
        resolve_cursor_plan(cursor=None, requested_sort="rank",
                            requested_sort_order=None, free_text="")


def test_a_stated_date_on_a_fresh_textless_request_is_accepted() -> None:
    plan = resolve_cursor_plan(cursor=None, requested_sort="date",
                               requested_sort_order=None, free_text="")
    assert plan.sort == "date"


def test_a_textless_request_may_now_ask_for_ascending_order() -> None:
    """#324's inverse face at the boundary: with no stated sort the plan
    resolves to ``date``, so ``run_search``'s rank+asc refusal — which reads
    ``plan.sort`` — no longer fires for a path it would never have taken."""
    plan = resolve_cursor_plan(cursor=None, requested_sort=None,
                               requested_sort_order="asc", free_text="")
    assert plan == CursorPlan(mode="fresh", sort="date", sort_order="asc")


def test_a_pool_cursor_is_not_judged_by_the_textless_rule() -> None:
    """A pool cursor is served by ``continue_page`` from the pool it was
    minted against, not by the date walk — so this request's own query does
    not decide its ordering and ``_check_pool_sort`` is what judges it.
    Applying the textless rule here would refuse a stated sort that the
    pool may well serve."""
    plan = resolve_cursor_plan(cursor="tok-1:2", requested_sort="rank",
                               requested_sort_order=None, free_text="")
    assert plan == CursorPlan(mode="pool", sort="rank", sort_order="desc")


def test_a_keyset_cursor_keeps_reporting_the_cursor_as_the_reason() -> None:
    """A stated ``rank`` alongside a keyset cursor is refused by the cursor
    guard, whose message names the cursor. That is the more specific
    diagnosis and must not be displaced by the textless one.

    The rule is stated here as a rule, and until #344 it held **only at
    this boundary**: ``Searcher.search`` ran the textless guard ahead of
    its walk guard, so one shape was diagnosed differently over HTTP than
    from a library call. Both layers apply it now —
    ``tests/test_searcher_guard_precedence.py`` is the other half."""
    from localmail.api.search_cursor import encode_keyset_cursor
    raw = encode_keyset_cursor(
        KeysetCursor(ts=None, id=7, order="desc", walk="archive")
    )
    with pytest.raises(ValidationFailed, match="this cursor continues"):
        resolve_cursor_plan(cursor=raw, requested_sort="rank",
                            requested_sort_order=None, free_text="")


# --- run_search, where the ordering against the ACL branch matters ---------

def _searcher() -> MagicMock:
    """A searcher that fails loudly if any retrieval is attempted."""
    s = MagicMock()
    s.search.side_effect = AssertionError("no search may run")
    s.smart_available = False
    return s


@pytest.mark.parametrize("free_text", ["", "   ", *OPERATOR_ONLY])
def test_run_search_refuses_a_stated_rank_without_free_text(
    free_text: str,
) -> None:
    s = _searcher()
    with pytest.raises(ValidationFailed, match="no free text"):
        run_search(searcher=s, free_text=free_text, filters={}, limit=5,
                   allowed_account_ids=[1], user_id=1, sort="rank")
    s.search.assert_not_called()


@pytest.mark.parametrize("free_text", ["", *OPERATOR_ONLY])
def test_the_refusal_precedes_the_empty_acl_short_circuit(
    free_text: str,
) -> None:
    """That branch answers with an empty page, indistinguishable from "you
    have reached the end" — so a grant-nothing caller would be told a
    contradictory request had succeeded and was complete."""
    s = _searcher()
    with pytest.raises(ValidationFailed, match="no free text"):
        run_search(searcher=s, free_text=free_text, filters={}, limit=5,
                   allowed_account_ids=[], user_id=1, sort="rank")


def test_a_stated_rank_with_free_text_reaches_the_searcher() -> None:
    """The positive control. A gate matching too broadly would refuse every
    search the GUI issues and every assertion above would still pass."""
    s = _searcher()
    with pytest.raises(AssertionError, match="no search may run"):
        run_search(searcher=s, free_text="invoice", filters={}, limit=5,
                   allowed_account_ids=[1], user_id=1, sort="rank")


@pytest.mark.parametrize("free_text", ["", *OPERATOR_ONLY])
def test_an_unstated_sort_without_free_text_reaches_the_searcher(
    free_text: str,
) -> None:
    """The half that keeps every filter-only search working: the GUI issues
    exactly this shape whenever the search box is empty and a chip is set.

    The forwarded ``sort`` is ``None``, not the gate's resolution of it —
    see ``test_the_caller_s_axes_are_forwarded_verbatim`` below for why that
    distinction is the whole of the fix. What this pins is that the request
    is *not* refused: it reaches retrieval.
    """
    s = _searcher()
    with pytest.raises(AssertionError, match="no search may run"):
        run_search(searcher=s, free_text=free_text, filters={}, limit=5,
                   allowed_account_ids=[1], user_id=1)
    assert s.search.call_args.kwargs["sort"] is None


def test_ascending_order_without_free_text_reaches_the_searcher() -> None:
    s = _searcher()
    with pytest.raises(AssertionError, match="no search may run"):
        run_search(searcher=s, free_text="", filters={}, limit=5,
                   allowed_account_ids=[1], user_id=1, sort_order="asc")
    kwargs = s.search.call_args.kwargs
    assert (kwargs["sort"], kwargs["sort_order"]) == (None, "asc")


def test_the_caller_s_axes_are_forwarded_verbatim() -> None:
    """The fresh branch hands the Searcher what the *caller* stated.

    Forwarding the gate's resolution instead destroys the distinction the
    Searcher's guard turns on. ``plan.sort`` is never ``None``, so an
    unstated sort arrived looking stated, and on the divergent-parse class
    (an open quote, before #367) a caller who omitted ``sort`` was told to
    "pass sort='date' or omit sort" — a remedy they had already followed.
    That is #324's own defect, a sort the caller never chose reported as
    their statement, reintroduced by #324's fix.

    Pinned on both axes and in both directions, because a mutation that
    forwards ``plan`` for one of them is otherwise invisible: the gate's
    resolution and the caller's statement agree for every *ordinary* query,
    which is what let this ship.
    """
    s = _searcher()
    with pytest.raises(AssertionError, match="no search may run"):
        run_search(searcher=s, free_text="invoice", filters={}, limit=5,
                   allowed_account_ids=[1], user_id=1)
    kwargs = s.search.call_args.kwargs
    assert (kwargs["sort"], kwargs["sort_order"]) == (None, None)

    s = _searcher()
    with pytest.raises(AssertionError, match="no search may run"):
        run_search(searcher=s, free_text="invoice", filters={}, limit=5,
                   allowed_account_ids=[1], user_id=1, sort="rank",
                   sort_order="desc")
    kwargs = s.search.call_args.kwargs
    assert (kwargs["sort"], kwargs["sort_order"]) == ("rank", "desc")


def _searcher_reaching_retrieval():
    """A real Searcher whose pool fails loudly once every guard has passed."""
    from localmail.config import SearchConfig
    from localmail.search.searcher import Searcher

    pool = MagicMock()
    # Reaching this proves the request was not refused: every #324 guard
    # fires before any connection is opened.
    pool.connection.side_effect = AssertionError("retrieval was reached")
    return Searcher(pool=pool, cfg=SearchConfig(), embeddings=_E(),
                    reranker=None, rewriter=None)


@pytest.mark.parametrize("free_text, sort, sort_order", [
    # Free text `from:` on both readings, so rankable, so a stated rank runs.
    ('from:"', "rank", None),
    ('from:"', None, None),
    # No free text on both readings, so it resolves to date and asc is honoured.
    ('"', None, "asc"),
])
def test_the_gate_and_the_searcher_now_read_an_open_quote_alike(
    free_text: str, sort, sort_order,
) -> None:
    """The class #324's review found the two layers disagreeing on, served.

    Before #367 the gate parsed the raw request field and the Searcher the
    ACL-composed query, whose ``account_id:`` tokens followed the free text,
    so an open quote swallowed them: ``from:"`` was text to the gate and
    textless to the Searcher, and ``'"'`` the reverse. A stated rank on the
    first was refused by the Searcher, and ``asc`` on the second met a
    ``rank`` the gate had not resolved, both reaching the caller as 400s
    only because ``run_search`` mapped the Searcher's refusal.

    The gate now parses the composition from the caller's filters, and the
    filters lead it, so both readings see the same free text and each
    request is served. Driven with the **real** Searcher, because the
    property is that its own resolution agrees.
    """
    with pytest.raises(AssertionError, match="retrieval was reached"):
        run_search(searcher=_searcher_reaching_retrieval(), free_text=free_text,
                   filters={}, limit=5, allowed_account_ids=[1], user_id=1,
                   sort=sort, sort_order=sort_order)


def test_a_query_textless_to_both_readings_is_refused_by_the_gate() -> None:
    """The positive control, so the test above is not merely a guard that
    stopped firing. ``subject:"invoice`` leaves its quote open and no free
    text on either reading, so a stated rank is refused — by the gate, which
    is why the Searcher here is a mock that must never be called. (A real
    Searcher would not tell the two apart: its own #324 guard also fires
    before any connection is opened.)"""
    s = _searcher()
    with pytest.raises(ValidationFailed, match="no free text"):
        run_search(searcher=s, free_text='subject:"invoice', filters={},
                   limit=5, allowed_account_ids=[1], user_id=1, sort="rank")
    s.search.assert_not_called()


def test_ascending_order_with_free_text_is_still_refused() -> None:
    s = _searcher()
    with pytest.raises(ValidationFailed, match="sort_order='asc'"):
        run_search(searcher=s, free_text="invoice", filters={}, limit=5,
                   allowed_account_ids=[1], user_id=1, sort_order="asc")
    s.search.assert_not_called()


# --- the round trip, against a real archive --------------------------------
#
# The unit pins above judge each half; this is the shape #324 actually
# reports — a request accepted on page 1 and its own cursor refused on
# page 2 — so it is pinned end to end rather than inferred from the two.

class _E:
    name = "s"
    model = "s"
    dimension = 768

    def embed_documents(self, texts):
        return [[1.0] * 768 for _ in texts]

    def embed_query(self, text):
        return [0.5] * 768

    def health_check(self) -> None:
        pass


def _seed(conn, n: int = 7) -> int:
    from datetime import datetime, timezone
    with conn.cursor() as cur:
        cur.execute("INSERT INTO accounts (name,email_address,imap_host,"
                    "auth_method) VALUES ('a','a@x','h','password') RETURNING id")
        row = cur.fetchone()
        assert row is not None
        acct = row[0]
        for i in range(n):
            cur.execute(
                "INSERT INTO messages (account_id, message_id, raw_sha256,"
                " subject, body_text, headers, raw_bytes, size_bytes,"
                " internal_date)"
                " VALUES (%s,%s,%s,%s,'body','{}'::jsonb,'r',1,%s)",
                (acct, f"<m{i}>", bytes([i + 1]) * 32, f"Subject {i}",
                 datetime(2026, 3, i + 1, tzinfo=timezone.utc)),
            )
    conn.commit()
    return acct


def test_a_textless_search_pages_without_stating_a_sort(db_dsn, db_conn) -> None:
    """The documented contract, end to end: state nothing, follow the cursor.

    This is what #324's refusal must not break — and the reason the fix
    resolves an *unstated* sort to ``date`` instead of refusing every
    textless request.
    """
    from localmail.config import SearchConfig
    from localmail.db import open_pool
    from localmail.search.searcher import Searcher

    acct = _seed(db_conn, n=7)
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=SearchConfig(), embeddings=_E(),
                     reranker=None)
        first = run_search(searcher=s, free_text="", filters={}, limit=3,
                           allowed_account_ids=[acct], user_id=1)
        assert len(first["results"]) == 3
        assert first["next_cursor"] is not None
        second = run_search(searcher=s, free_text="", filters={}, limit=3,
                            allowed_account_ids=[acct], user_id=1,
                            cursor=first["next_cursor"])
    finally:
        pool.close()
    page1 = [r["message_id"] for r in first["results"]]
    page2 = [r["message_id"] for r in second["results"]]
    assert page2 and not set(page1) & set(page2), (
        "page 2 must advance past page 1, not restart the walk"
    )


def test_the_page_one_that_used_to_mint_a_contradicting_cursor_is_refused(
    db_dsn, db_conn,
) -> None:
    """#324 itself.

    Before the fix this call returned 200 with a ``K|`` cursor recording
    ``date`` — so echoing the same ``sort='rank'`` back alongside that
    cursor was a 400, one page later. The request is refused at page 1
    now, where the caller can still act on it.
    """
    from localmail.config import SearchConfig
    from localmail.db import open_pool
    from localmail.search.searcher import Searcher

    acct = _seed(db_conn, n=7)
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=SearchConfig(), embeddings=_E(),
                     reranker=None)
        with pytest.raises(ValidationFailed, match="no free text"):
            run_search(searcher=s, free_text="", filters={}, limit=3,
                       allowed_account_ids=[acct], user_id=1, sort="rank")
    finally:
        pool.close()


def test_a_textless_search_may_now_walk_oldest_first(db_dsn, db_conn) -> None:
    """The inverse face, end to end: ``sort_order='asc'`` alone used to be a
    400 naming ``sort='rank'``. It walks the archive oldest-first now, and
    its cursor continues ascending."""
    from localmail.config import SearchConfig
    from localmail.db import open_pool
    from localmail.search.searcher import Searcher

    acct = _seed(db_conn, n=7)
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=SearchConfig(), embeddings=_E(),
                     reranker=None)
        asc = run_search(searcher=s, free_text="", filters={}, limit=3,
                         allowed_account_ids=[acct], user_id=1,
                         sort_order="asc")
        desc = run_search(searcher=s, free_text="", filters={}, limit=3,
                          allowed_account_ids=[acct], user_id=1)
    finally:
        pool.close()
    assert asc["next_cursor"].startswith("KA|"), (
        "the cursor must carry the direction the walk ran in"
    )
    assert [r["date"] for r in asc["results"]] == sorted(
        r["date"] for r in asc["results"]
    )
    # The *endpoints*, not merely "ascending and different from descending":
    # ``_seed`` writes rows 1..n with strictly increasing ``internal_date``,
    # so oldest-first must open at row 1 and newest-first at row n. An
    # arbitrary ascending window — a walk that started in the middle, or one
    # that ordered a truncated pool — satisfies the two looser assertions
    # above while getting the whole point of the feature wrong.
    assert asc["results"][0]["message_id"] == "1"
    assert desc["results"][0]["message_id"] == "7"
