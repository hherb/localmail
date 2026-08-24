# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""The cursor decides the continuation mode; a stated ``sort`` may not contradict it (#308).

``/v1/search`` (and the MCP ``search`` tool) defaulted ``sort`` to ``"rank"``
and handed that default to the Searcher *before* the cursor was consulted.
The Searcher picks its retrieval branch from ``(sort, free_text)`` and reads
``keyset_cursor`` only inside the lexical-date branch — so a ``K|…`` cursor
paged back with the documented "call again with that value in ``cursor``"
was dropped without a word, and the caller got page 1 of a differently
ordered search that looked like a continuation.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from localmail.api.errors import SearchCursorExpired, ValidationFailed
from localmail.api.search import run_search
from localmail.api.search_cursor import (
    SearchCursor,
    encode_keyset_cursor,
    encode_search_cursor,
)
from localmail.search.searcher import KeysetCursor, PoolMetadata


def _keyset_cursor(day: int = 21) -> tuple[KeysetCursor, str]:
    ks = KeysetCursor(ts=datetime(2026, 5, day, tzinfo=timezone.utc), id=100)
    return ks, encode_keyset_cursor(ks)


def _page(*, token: str | None = None, next_keyset: KeysetCursor | None = None) -> MagicMock:
    p = MagicMock()
    p.results = []
    p.search_token = token
    p.pool_size = 0
    p.page_size = 2
    p.page = 1
    p.has_more_in_pool = False
    p.can_grow_pool = False
    p.candidates_per_arm = 50
    p.timing_ms = {"total": 1.0}
    p.next_keyset = next_keyset
    return p


def _searcher() -> MagicMock:
    s = MagicMock()
    s.config.candidates_per_arm = 50
    s.config.candidates_per_arm_max = 800
    s.search.return_value = _page()
    s.continue_page.return_value = _page(token="tok-1")
    return s


def test_keyset_cursor_without_a_stated_sort_continues_the_date_walk() -> None:
    """The documented way to page is to send back ``next_cursor`` alone.

    The cursor is the only statement about ordering, so it decides: the
    Searcher must be called with ``sort="date"``, not the request model's
    ``"rank"`` default, which would silently restart as a hybrid search.
    """
    s = _searcher()
    incoming, cursor = _keyset_cursor()
    run_search(searcher=s, free_text="invoice", filters={}, limit=2,
               allowed_account_ids=[1], user_id=99, cursor=cursor)
    s.search.assert_called_once()
    _, kwargs = s.search.call_args
    assert kwargs.get("sort") == "date"
    assert kwargs.get("keyset_cursor") == incoming


def test_keyset_cursor_with_an_explicitly_stated_rank_sort_is_rejected() -> None:
    """A stated sort that the cursor cannot serve is a contradiction, not a hint.

    Coercing it would silently ignore the caller; dropping the cursor is
    the defect. Both are answers to a question the caller must re-ask.
    """
    s = _searcher()
    _, cursor = _keyset_cursor()
    with pytest.raises(ValidationFailed):
        run_search(searcher=s, free_text="invoice", filters={}, limit=2,
                   allowed_account_ids=[1], user_id=99, sort="rank",
                   cursor=cursor)
    s.search.assert_not_called()


def test_keyset_cursor_without_the_original_query_is_rejected() -> None:
    """The keyset walk rebuilds its FTS predicate from the re-sent query.

    Without one there is no walk to continue: the Searcher falls through to
    the empty-query recent-mail list and answers with page 1 of a different
    result set.
    """
    s = _searcher()
    _, cursor = _keyset_cursor()
    with pytest.raises(ValidationFailed):
        run_search(searcher=s, free_text="", filters={}, limit=2,
                   allowed_account_ids=[1], user_id=99, sort="date",
                   cursor=cursor)
    s.search.assert_not_called()


def test_keyset_cursor_without_query_or_sort_is_rejected() -> None:
    """The same rejection on the shape the docs actually prescribe.

    Its sibling above states ``sort="date"``; this states nothing, which is
    what ``docs/mcp-usage.md`` tells a client to send. Narrowing the guard
    to ``requested_sort is not None and not free_text.strip()`` leaves that
    sibling green, so without this the check is pinned only on the path
    where the caller happened to say something.
    """
    s = _searcher()
    _, cursor = _keyset_cursor()
    with pytest.raises(ValidationFailed):
        run_search(searcher=s, free_text="", filters={}, limit=2,
                   allowed_account_ids=[1], user_id=99, cursor=cursor)
    s.search.assert_not_called()


@pytest.mark.parametrize("query", ["subject:invoice", "from:bob@example.com",
                                   "lang:en", "has:attachment"])
def test_a_query_of_only_filter_operators_counts_as_no_query(query: str) -> None:
    """The gate must measure the text the Searcher will dispatch on.

    ``parse_query`` lifts every filter operator out of the free text, so
    ``"subject:invoice"`` is non-blank as a request field and blank by the
    time the lexical branch tests it. Reading the raw string here let the
    request through as a keyset continuation, the branch declined the
    cursor, and the Searcher's guard raised a bare ``ValueError`` — a 500
    for what is a caller error, on the very input class this check exists
    to catch.
    """
    s = _searcher()
    _, cursor = _keyset_cursor()
    with pytest.raises(ValidationFailed):
        run_search(searcher=s, free_text=query, filters={}, limit=2,
                   allowed_account_ids=[1], user_id=99, cursor=cursor)
    s.search.assert_not_called()


def test_a_malformed_paging_request_is_rejected_even_with_an_empty_acl() -> None:
    """Validation precedes the grant-nothing short-circuit.

    That branch answers with an empty page, which is byte-identical to
    "you have reached the end of your results" — so a caller with no grants
    would be told their contradictory request succeeded and was complete.
    """
    s = _searcher()
    _, cursor = _keyset_cursor()
    with pytest.raises(ValidationFailed):
        run_search(searcher=s, free_text="invoice", filters={}, limit=2,
                   allowed_account_ids=[], user_id=99, sort="rank",
                   cursor=cursor)
    s.search.assert_not_called()


def test_pool_cursor_with_a_sort_the_pool_was_not_built_with_is_rejected() -> None:
    """The mirror image: ``continue_page`` serves the sort the pool was minted
    with, so a stated ``sort="date"`` on a rank pool is silently ignored."""
    s = _searcher()
    s.get_pool_metadata.return_value = PoolMetadata(
        candidates_per_arm=50, page_size=2, rerank_pool_size=20, pool_size=20,
        sort="rank", sort_order="desc",
    )
    cursor = encode_search_cursor(SearchCursor(token="tok-1", page=2))
    with pytest.raises(ValidationFailed):
        run_search(searcher=s, free_text="invoice", filters={}, limit=2,
                   allowed_account_ids=[1], user_id=99, sort="date",
                   cursor=cursor)
    s.continue_page.assert_not_called()


def test_pool_cursor_with_the_sort_the_pool_was_built_with_continues() -> None:
    """The GUI re-sends its sort on every loadMore; that must keep working."""
    s = _searcher()
    s.get_pool_metadata.return_value = PoolMetadata(
        candidates_per_arm=50, page_size=2, rerank_pool_size=20, pool_size=20,
        sort="rank", sort_order="desc",
    )
    cursor = encode_search_cursor(SearchCursor(token="tok-1", page=2))
    run_search(searcher=s, free_text="invoice", filters={}, limit=2,
               allowed_account_ids=[1], user_id=99, sort="rank", cursor=cursor)
    s.continue_page.assert_called_once_with("tok-1", 2, user_id=99)


def test_pool_cursor_whose_pool_is_gone_reports_an_expired_cursor() -> None:
    """The sort check probes the cache, so it meets eviction first. It must
    hand back the 409 ``continue_page`` would have raised a moment later —
    the GUI's transparent re-search hangs off that — and not trip over the
    missing metadata."""
    s = _searcher()
    s.get_pool_metadata.return_value = None
    cursor = encode_search_cursor(SearchCursor(token="tok-1", page=2))
    with pytest.raises(SearchCursorExpired):
        run_search(searcher=s, free_text="invoice", filters={}, limit=2,
                   allowed_account_ids=[1], user_id=99, sort="rank",
                   cursor=cursor)


def test_pool_cursor_without_a_stated_sort_costs_no_metadata_probe() -> None:
    """Nothing to contradict, so nothing to check — the pool stays the authority."""
    s = _searcher()
    cursor = encode_search_cursor(SearchCursor(token="tok-1", page=2))
    run_search(searcher=s, free_text="invoice", filters={}, limit=2,
               allowed_account_ids=[1], user_id=99, cursor=cursor)
    s.continue_page.assert_called_once_with("tok-1", 2, user_id=99)
    s.get_pool_metadata.assert_not_called()


def test_a_stated_sort_still_governs_when_there_is_no_cursor() -> None:
    s = _searcher()
    run_search(searcher=s, free_text="invoice", filters={}, limit=2,
               allowed_account_ids=[1], user_id=99, sort="date")
    _, kwargs = s.search.call_args
    assert kwargs.get("sort") == "date"


def test_an_omitted_sort_still_means_rank_when_there_is_no_cursor() -> None:
    s = _searcher()
    run_search(searcher=s, free_text="invoice", filters={}, limit=2,
               allowed_account_ids=[1], user_id=99)
    _, kwargs = s.search.call_args
    assert kwargs.get("sort") == "rank"


# --- end to end, against a real archive -------------------------------------


class _Embedder:
    name = "s"
    model = "s"
    dimension = 768

    def embed_documents(self, texts):
        return [[1.0] * 768 for _ in texts]

    def embed_query(self, text):
        return [0.5] * 768

    def health_check(self) -> None:
        pass


def _seed(conn, count: int) -> tuple[int, list[int]]:
    """`count` matching messages, newest first. Returns (account_id, message_ids)."""
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    ids: list[int] = []
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (name, email_address, imap_host, auth_method)"
            " VALUES ('a', 'a@x', 'h', 'password') RETURNING id"
        )
        row = cur.fetchone()
        assert row is not None
        account_id = row[0]
        for i in range(count):
            cur.execute(
                "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
                " body_text, headers, raw_bytes, size_bytes, internal_date)"
                " VALUES (%s, %s, %s, %s, 'body', '{}'::jsonb, 'r', 1, %s) RETURNING id",
                (account_id, f"<m{i}>", bytes([i + 1]) * 32,
                 f"e-ticket booking #{i:02d}", now - timedelta(hours=i)),
            )
            row = cur.fetchone()
            assert row is not None
            ids.append(row[0])
    conn.commit()
    return account_id, ids


def test_paging_a_date_sorted_search_with_the_cursor_alone_advances(db_dsn, db_conn):
    """The whole defect, end to end, in the shape a client actually pages in.

    ``docs/mcp-usage.md`` says to page by calling again with ``next_cursor``.
    Doing exactly that used to hand the Searcher the model's ``sort="rank"``
    default, which reads its retrieval branch from ``(sort, query)`` and
    never looks at a keyset cursor outside the lexical one — so page 2 came
    back as page 1 of a hybrid search, and the walk never advanced.
    """
    from localmail.config import SearchConfig
    from localmail.db import open_pool
    from localmail.search.searcher import Searcher

    account_id, ids = _seed(db_conn, count=6)
    pool = open_pool(db_dsn)
    try:
        searcher = Searcher(pool=pool, cfg=SearchConfig(), embeddings=_Embedder(),
                            reranker=None, rewriter=None)
        first = run_search(searcher=searcher, free_text="e-ticket", filters={},
                           limit=3, allowed_account_ids=[account_id], user_id=1,
                           sort="date")
        assert [r["message_id"] for r in first["results"]] == [str(i) for i in ids[:3]]
        assert first["next_cursor"] is not None

        # Exactly what the docs tell a client to send: the cursor, no sort.
        second = run_search(searcher=searcher, free_text="e-ticket", filters={},
                            limit=3, allowed_account_ids=[account_id], user_id=1,
                            cursor=first["next_cursor"])
    finally:
        pool.close()
    assert [r["message_id"] for r in second["results"]] == [str(i) for i in ids[3:]]
