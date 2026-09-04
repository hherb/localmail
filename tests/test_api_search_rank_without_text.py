# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The api boundary refuses ``sort="rank"`` on a textless query (#324).

Two guards, one rule. This one answers before any work is done and — as
this cluster keeps re-learning — **ahead of the empty-ACL short-circuit**,
which returns an empty page byte-identical to "you have reached the end":
a grant-nothing caller must not be told a contradictory request succeeded.
The Searcher's own guard covers the CLI and library callers who never reach
here, and the two read different strings, so it is not a dead backstop —
see ``test_api_search_cursor_walk.py`` for the same argument on #326.

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
    diagnosis and must not be displaced by the textless one."""
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
    below a caller who omitted ``sort`` was told to "pass sort='date' or
    omit sort" — a remedy they had already followed. That is #324's own
    defect, a sort the caller never chose reported as their statement,
    reintroduced by #324's fix.

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


def test_a_caller_who_omitted_sort_is_never_told_to_omit_it() -> None:
    """The regression #324's fix introduced, on the class it documented.

    ``parse_query`` is not compositional across an unbalanced quote:
    ``from:"`` leaves ``'from:'`` as free text alone and nothing once the
    ACL's ``account_id:`` token joins it. So the gate reads this query as
    rankable and resolves ``rank``; the Searcher reads it as textless. With
    the gate's resolution forwarded, that ``rank`` was attributed to a
    caller who never wrote it.

    Driven with the **real** Searcher, because the property is that the two
    guards genuinely disagree — a mock would only prove the plumbing.
    """
    from localmail.config import SearchConfig
    from localmail.search.searcher import Searcher

    def _searcher_reaching_retrieval() -> Searcher:
        pool = MagicMock()
        # Reaching this proves the request was not refused: every #324 guard
        # fires before any connection is opened.
        pool.connection.side_effect = AssertionError("retrieval was reached")
        return Searcher(pool=pool, cfg=SearchConfig(), embeddings=_E(),
                        reranker=None, rewriter=None)

    # No `sort` argument at all: there is nothing to refuse.
    with pytest.raises(AssertionError, match="retrieval was reached"):
        run_search(searcher=_searcher_reaching_retrieval(), free_text='from:"',
                   filters={}, limit=5, allowed_account_ids=[1], user_id=1)

    # The divergence runs the other way too, and that is what the widened
    # catch is for: `'"'` is textless to the gate (so its rank+asc refusal
    # does not fire) and text once the ACL token is composed in, so the
    # Searcher resolves `rank`, meets `asc`, and raises
    # `SortOrderNotApplicable`. Forwarding the gate's resolution made that
    # unreachable; forwarding the caller's makes it a clean 400 instead of
    # an uncaught 500. The Searcher's own wording identifies which guard
    # answered.
    with pytest.raises(ValidationFailed, match="bounded candidate pool"):
        run_search(searcher=_searcher_reaching_retrieval(), free_text='"',
                   filters={}, limit=5, allowed_account_ids=[1], user_id=1,
                   sort_order="asc")

    # The positive control: a *stated* rank on the same query is still a 400,
    # so this is not merely a guard that stopped firing.
    with pytest.raises(ValidationFailed, match="no free text"):
        run_search(searcher=_searcher_reaching_retrieval(), free_text='from:"',
                   filters={}, limit=5, allowed_account_ids=[1], user_id=1,
                   sort="rank")


def test_ascending_order_with_free_text_is_still_refused() -> None:
    s = _searcher()
    with pytest.raises(ValidationFailed, match="sort_order='asc'"):
        run_search(searcher=s, free_text="invoice", filters={}, limit=5,
                   allowed_account_ids=[1], user_id=1, sort_order="asc")
    s.search.assert_not_called()


def test_the_searchers_refusal_is_mapped_rather_than_escaping_as_a_500() -> None:
    """The two guards read **different strings**, and the gap is reachable.

    The gate parses the raw request field; the Searcher parses the
    ACL-composed query. ``parse_query`` is not compositional across an
    unbalanced quote — ``from:"`` leaves ``'from:'`` as free text alone and
    nothing once a trailing ``account_id:`` token joins it — so the gate
    reads this query as rankable and the branch reads it as textless.

    Without the catch the Searcher's ``SortNotApplicable`` escapes
    ``run_search``: ``serve.app`` registers a handler for ``APIError``
    only, so it reaches the caller as a 500 with no problem+json body, on
    a query the boundary had already cleared. Pinned with the real
    Searcher guard rather than a mock, because the property under test is
    that the two really do disagree.
    """
    from localmail.config import SearchConfig
    from localmail.search.searcher import Searcher

    class _Embeddings:
        name = "s"
        model = "s"
        dimension = 768

        def embed_documents(self, texts):  # pragma: no cover - never reached
            raise AssertionError("retrieval must not start")

        def embed_query(self, text):  # pragma: no cover - never reached
            raise AssertionError("retrieval must not start")

        def health_check(self) -> None:
            pass

    pool = MagicMock()
    pool.connection.side_effect = AssertionError("no connection may be opened")
    searcher = Searcher(pool=pool, cfg=SearchConfig(), embeddings=_Embeddings(),
                        reranker=None, rewriter=None)
    with pytest.raises(ValidationFailed, match="no free text"):
        run_search(searcher=searcher, free_text='from:"', filters={}, limit=5,
                   allowed_account_ids=[1], user_id=1, sort="rank")
    pool.connection.assert_not_called()


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
