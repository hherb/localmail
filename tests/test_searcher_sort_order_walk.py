# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Ascending date order is the exact reverse of descending, and pages.

Ascending is spelled ``ASC NULLS FIRST, id ASC`` because that is the exact
reverse of ``messages_recent_idx`` and is therefore served by a backward
index scan. The ``NULLS LAST`` spelling full-sorts the table; an
``IS NOT NULL`` restriction does not rescue it. Both were measured on the
live 128k archive before this was written.

Undated rows therefore sort *first* ascending, which is what makes
``asc == reversed(desc)`` hold as an invariant.
"""
from __future__ import annotations

from datetime import datetime, timezone

from localmail.config import SearchConfig
from localmail.db import open_pool
from localmail.search.searcher import Searcher


class _E:
    name = "s"; model = "s"; dimension = 768
    def embed_documents(self, t): return [[1.0] * 768 for _ in t]
    def embed_query(self, t): return [0.5] * 768
    def health_check(self): pass


#: The day the ``tied=`` rows share, chosen to collide with dated row 3 so
#: the tie group sits *inside* the dated run rather than at either end —
#: a group at the head or tail can be walked correctly by a predicate that
#: has no tiebreaker at all.
_TIE_DAY = datetime(2026, 1, 4, tzinfo=timezone.utc)


def _seed(conn, *, n=7, undated=2, tied=0):
    """n dated messages plus `undated` with no usable date at all.

    ``tied`` adds that many further dated rows all carrying ``_TIE_DAY``,
    which one of the ``n`` already holds — so the archive contains one tie
    group of ``tied + 1`` rows. Every other fixture in this suite gives its
    dated rows distinct timestamps, which is what left the ``id`` half of
    both keyset predicates unexercised (see the tie test below).

    Returns the undated rows' ids, ascending — the ordering the ascending
    walk must reproduce across its undated head.
    """
    undated_ids: list[int] = []
    with conn.cursor() as cur:
        cur.execute("INSERT INTO accounts (name,email_address,imap_host,auth_method)"
                    " VALUES ('a','a@x','h','password') RETURNING id")
        acct = cur.fetchone()[0]
        for i in range(n):
            cur.execute(
                "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
                " body_text, headers, raw_bytes, size_bytes, internal_date)"
                " VALUES (%s,%s,%s,%s,%s,'{}'::jsonb,'r',1,%s)",
                (acct, f"<d{i}>", bytes([i + 1]) * 32, f"dated {i} needle",
                 "body needle", datetime(2026, 1, i + 1, tzinfo=timezone.utc)),
            )
        for k in range(tied):
            cur.execute(
                "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
                " body_text, headers, raw_bytes, size_bytes, internal_date)"
                " VALUES (%s,%s,%s,%s,%s,'{}'::jsonb,'r',1,%s)",
                (acct, f"<t{k}>", bytes([100 + k]) * 32, f"tied {k} needle",
                 "body needle", _TIE_DAY),
            )
        for j in range(undated):
            cur.execute(
                "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
                " body_text, headers, raw_bytes, size_bytes)"
                " VALUES (%s,%s,%s,%s,%s,'{}'::jsonb,'r',1) RETURNING id",
                (acct, f"<u{j}>", bytes([200 + j]) * 32, f"undated {j} needle",
                 "body needle"),
            )
            undated_ids.append(int(cur.fetchone()[0]))
    conn.commit()
    return undated_ids


def _all_pages(searcher, *, order, query="needle", page_size=3):
    """Walk every page, returning the flat list of message ids."""
    ids: list[int] = []
    cursor = None
    for _ in range(50):  # generous bound; the walk must terminate
        page = searcher.search(query, allowed_account_ids=None,
                               page_size=page_size, user_id=1, sort="date",
                               sort_order=order, keyset_cursor=cursor)
        ids.extend(r.message_id for r in page.results)
        if page.next_keyset is None:
            return ids
        cursor = page.next_keyset
    raise AssertionError("walk did not terminate")


def test_ascending_is_exactly_reversed_descending(db_dsn, db_conn):
    """The whole ordering, undated rows included — not just the dated head."""
    _seed(db_conn)
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=SearchConfig(), embeddings=_E(), reranker=None)
        desc = _all_pages(s, order="desc")
        asc = _all_pages(s, order="asc")
    finally:
        pool.close()
    assert len(desc) == 9
    assert asc == list(reversed(desc))


def test_undated_rows_sort_first_ascending(db_dsn, db_conn):
    """NULLS FIRST is not incidental — it is what the backward scan requires."""
    _seed(db_conn, n=7, undated=2)
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=SearchConfig(), embeddings=_E(), reranker=None)
        page = s.search("needle", allowed_account_ids=None, page_size=2,
                        user_id=1, sort="date", sort_order="asc")
    finally:
        pool.close()
    subjects = [r.subject for r in page.results]
    assert all(s_.startswith("undated") for s_ in subjects), subjects


def test_ascending_pages_do_not_overlap_or_skip(db_dsn, db_conn):
    _seed(db_conn)
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=SearchConfig(), embeddings=_E(), reranker=None)
        ids = _all_pages(s, order="asc", page_size=2)
    finally:
        pool.close()
    assert len(ids) == len(set(ids)) == 9


def test_descending_is_unchanged_when_order_is_unstated(db_dsn, db_conn):
    """The default path must be byte-identical to today's behaviour."""
    _seed(db_conn)
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=SearchConfig(), embeddings=_E(), reranker=None)
        stated = _all_pages(s, order="desc")
        unstated_page = s.search("needle", allowed_account_ids=None, page_size=3,
                                 user_id=1, sort="date")
    finally:
        pool.close()
    assert [r.message_id for r in unstated_page.results] == stated[:3]


def test_an_ascending_page_boundary_inside_the_undated_head_loses_nothing(
    db_dsn, db_conn,
):
    """Three undated rows, pages of two — the boundary lands *inside* the head.

    This is the shape the ascending NULL-cursor predicate exists for, and
    the only shape that can see it. Page 1 returns two undated rows and
    mints a cursor whose ``ts`` is NULL; page 2 must return the rest of the
    undated block *and then* every dated row, which is why that predicate
    is ``(expr IS NULL AND id > %s) OR expr IS NOT NULL`` rather than
    ``expr IS NOT NULL`` alone. Drop the first disjunct and the third
    undated row vanishes from the archive between pages, silently.

    Every sibling here seeds two undated rows against a page size of two or
    three, so the boundary always falls at the block's *end* — where
    ``id > max_undated_id`` matches nothing either way and the disjunct is
    invisible.
    """
    undated_ids = _seed(db_conn, n=4, undated=3)
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=SearchConfig(), embeddings=_E(), reranker=None)
        first = s.search("needle", allowed_account_ids=None, page_size=2,
                         user_id=1, sort="date", sort_order="asc")
        walked = _all_pages(s, order="asc", page_size=2)
    finally:
        pool.close()
    assert first.next_keyset is not None
    assert first.next_keyset.ts is None, (
        "page 1 did not end inside the undated head, so this test is not "
        "exercising the NULL-cursor predicate at all"
    )
    assert walked[:3] == undated_ids, walked
    assert len(walked) == len(set(walked)) == 7, walked


def test_a_descending_page_straddling_the_undated_tail_is_topped_up(
    db_dsn, db_conn,
):
    """#323: the dated→undated transition happens *within* one response.

    The descending dated predicate is a row comparison, so it cannot admit
    the NULLS-LAST undated tail — ``ROW(NULL, id) < ROW(…)`` is NULL. Those
    rows come from a second top-up statement issued in the same call, which
    is what ``api.browse.list_messages`` has done for #75 since before this
    walk existed.

    Two failure modes, and this is the only test in the file that separates
    them. Drop the top-up entirely and the undated rows vanish from the
    archive between pages — caught by the sibling walk tests' row counts.
    Defer it to the *next* page instead and every count still holds, while
    the caller gets a short page followed by a stub one: correct, and one
    wasted round trip per walk at exactly the boundary the cursor was
    minted for. Asserting the page is full is what tells the two apart.

    Seven dated rows and two undated, pages of three, puts the boundary
    strictly inside page three: one dated row left, two undated to fill it.
    """
    undated_ids = _seed(db_conn, n=7, undated=2)
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=SearchConfig(), embeddings=_E(), reranker=None)
        pages = []
        cursor = None
        for _ in range(5):
            page = s.search("needle", allowed_account_ids=None, page_size=3,
                            user_id=1, sort="date", sort_order="desc",
                            keyset_cursor=cursor)
            pages.append(page)
            if page.next_keyset is None:
                break
            cursor = page.next_keyset
    finally:
        pool.close()
    assert len(pages) == 3, [len(p_.results) for p_ in pages]
    straddling = pages[-1]
    assert len(straddling.results) == 3, (
        "the page straddling the undated tail came back short: the top-up "
        "either did not run or ran on the following page instead"
    )
    ids = [r.message_id for r in straddling.results]
    assert ids[1:] == list(reversed(undated_ids)), ids
    assert straddling.next_keyset is None, (
        "the walk did not end at the undated tail"
    )


def _all_ids_in_sql_order(conn, *, order):
    """Ground truth straight from the ORDER BY the walk claims to reproduce.

    Composed here rather than imported so a rewrite of
    ``_DATE_ORDER_BY_SQL`` that broke the ordering could not silently
    rewrite the expectation with it.
    """
    direction = ("ASC NULLS FIRST, m.id ASC" if order == "asc"
                 else "DESC NULLS LAST, m.id DESC")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT m.id FROM messages m"
            f" ORDER BY COALESCE(m.internal_date, m.date_sent) {direction}"
        )
        return [int(r[0]) for r in cur.fetchall()]


def _walk_with_cursors(searcher, *, order, page_size):
    """Like ``_all_pages`` but also returns each page's outgoing cursor, so
    a test can assert *where* the boundaries fell rather than trust that
    the page size put one where it wanted it."""
    ids: list[int] = []
    cursors: list = []
    cursor = None
    for _ in range(50):
        page = searcher.search("needle", allowed_account_ids=None,
                               page_size=page_size, user_id=1, sort="date",
                               sort_order=order, keyset_cursor=cursor)
        ids.extend(r.message_id for r in page.results)
        if page.next_keyset is None:
            return ids, cursors
        cursors.append(page.next_keyset)
        cursor = page.next_keyset
    raise AssertionError("walk did not terminate")


def test_a_tie_group_straddling_a_page_boundary_loses_nothing(db_dsn, db_conn):
    """The ``id`` tiebreaker in both keyset predicates is what this pins.

    Every other fixture in this suite gives its dated rows distinct
    timestamps, so ``ROW(expr, m.id) > ROW(%s, %s)`` selects exactly what
    the tiebreaker-less ``expr > %s`` would, and the descending
    ``expr = ts AND m.id < %s`` disjunct is equally inert. Dropping either
    therefore left the whole suite green — 150 and 174 focused tests
    respectively — while silently truncating any tie group that straddles a
    page boundary: the walk jumps past the rest of the group and every row
    in it is lost.

    Ties are ordinary here rather than exotic. Bulk sends share ``date_sent``
    to the second, and archive imports derive ``internal_date`` from the mbox
    ``From_`` envelope line or a maildir file mtime, so a large group sharing
    one timestamp is exactly the shape that loses many rows at once.

    ``page_size=2`` against a 4-row tie group should put a boundary inside
    it, but that is a property of the seed arithmetic rather than of
    anything asserted — so both walks check it directly: a cursor whose
    ``ts`` is ``_TIE_DAY`` means a page really did stop mid-group. Without
    that check a future change to ``n`` or ``tied`` could slide every
    boundary clear of the group and this test would keep passing while
    testing nothing.
    """
    _seed(db_conn, n=7, undated=2, tied=3)
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=SearchConfig(), embeddings=_E(), reranker=None)
        asc, asc_cursors = _walk_with_cursors(s, order="asc", page_size=2)
        desc, desc_cursors = _walk_with_cursors(s, order="desc", page_size=2)
        expected_asc = _all_ids_in_sql_order(db_conn, order="asc")
        expected_desc = _all_ids_in_sql_order(db_conn, order="desc")
    finally:
        pool.close()
    # 7 dated + 3 tied + 2 undated; the tie group is 4 rows at _TIE_DAY.
    assert len(expected_asc) == 12
    assert any(c.ts == _TIE_DAY for c in asc_cursors), (
        "no ascending page ended inside the tie group, so the ascending "
        "tiebreaker was never exercised"
    )
    assert any(c.ts == _TIE_DAY for c in desc_cursors), (
        "no descending page ended inside the tie group, so the descending "
        "tiebreaker was never exercised"
    )
    assert asc == expected_asc, "ascending walk lost or reordered tied rows"
    assert desc == expected_desc, "descending walk lost or reordered tied rows"
    assert asc == list(reversed(desc))


def _walk_restating_order_only_on_page_one(searcher, *, order, page_size=2):
    """Page the way a library caller does: state the order once, then send
    only the cursor back — the idiom ``tests/test_searcher.py`` itself uses
    and ``docs/mcp-usage.md`` prescribes for every other client.
    """
    page = searcher.search("needle", allowed_account_ids=None,
                           page_size=page_size, user_id=1, sort="date",
                           sort_order=order)
    ids = [r.message_id for r in page.results]
    for _ in range(50):
        if page.next_keyset is None:
            return ids
        page = searcher.search("needle", allowed_account_ids=None,
                               page_size=page_size, user_id=1, sort="date",
                               keyset_cursor=page.next_keyset)
        ids.extend(r.message_id for r in page.results)
    raise AssertionError("walk did not terminate")


def test_an_ascending_walk_paged_without_restating_the_order_keeps_ascending(
    db_dsn, db_conn,
):
    """A continuation must not silently reverse when ``sort_order`` is omitted.

    ``encode_keyset_cursor`` takes a required ``order`` precisely so a
    forgotten argument cannot mint a descending cursor for an ascending
    walk. The reading side carried the symmetric hazard: ``KeysetCursor``
    held only ``(ts, id)``, so ``Searcher.search`` paired a directionless
    cursor with a ``sort_order`` that defaults to ``"desc"`` — and page 2
    re-emitted a row the caller already held, then walked backwards off the
    end. No exception, no log line: it reads as a data problem, not a
    call-site one.

    HTTP and MCP were safe only because ``run_search`` happens to pass
    ``plan.sort_order`` on every hop, which is a property of one call site
    rather than of the signature. The direction now rides on the cursor, so
    the pairing cannot be formed.
    """
    _seed(db_conn, n=7, undated=2)
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=SearchConfig(), embeddings=_E(), reranker=None)
        walked = _walk_restating_order_only_on_page_one(s, order="asc")
        expected = _all_ids_in_sql_order(db_conn, order="asc")
    finally:
        pool.close()
    assert walked == expected, (
        "the continuation reversed: the cursor's own direction lost to the "
        "unstated sort_order's default"
    )


def test_a_descending_walk_paged_the_same_way_is_unchanged(db_dsn, db_conn):
    """The negative control: descending is what the old default silently gave.

    Without this, making the cursor's direction win could serve every walk
    ascending and the test above would still pass.
    """
    _seed(db_conn, n=7, undated=2)
    pool = open_pool(db_dsn)
    try:
        s = Searcher(pool=pool, cfg=SearchConfig(), embeddings=_E(), reranker=None)
        walked = _walk_restating_order_only_on_page_one(s, order="desc")
        expected = _all_ids_in_sql_order(db_conn, order="desc")
    finally:
        pool.close()
    assert walked == expected
