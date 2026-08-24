# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""A text-walk cursor may not be paged with a blank query (#326).

#322 taught the blank-query branch to paginate, and dropped ``free_text``
from ``resolve_cursor_plan`` along with the guard that used to refuse this
pair. The general argument was sound — a keyset cursor identifies a
position, never a query, and changing ``folder_ids`` or the free text
between pages was already undefined. But the relaxation was broader than
the feature needed, and it landed on the **single most likely client
mistake**: ``docs/mcp-usage.md`` tells agents to "re-send the same
``query`` and filters", and forgetting to is now served silently as the
next ``limit`` messages of the entire archive, presented as a continuation
of the search.

The cursor now records which of the two walks minted it, so the check is
back for the one pair that used to be caught by construction — without
forbidding the blank-query paging #322 added, whose cursors say
``archive`` and are unaffected.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from localmail.api.errors import ValidationFailed
from localmail.api.search import run_search
from localmail.api.search_cursor import decode_keyset_cursor, encode_keyset_cursor
from localmail.search.keyset_walk import KeysetWalk, keyset_walk_error, walk_for_text
from localmail.search.searcher import KeysetCursor


def _cursor(walk: KeysetWalk, order: str = "desc") -> tuple[KeysetCursor, str]:
    ks = KeysetCursor(ts=datetime(2026, 5, 21, tzinfo=timezone.utc), id=100,
                      order=order, walk=walk)
    return ks, encode_keyset_cursor(ks)


def _page() -> MagicMock:
    p = MagicMock()
    p.results = []
    p.search_token = None
    p.pool_size = 0
    p.page_size = 2
    p.page = 1
    p.has_more_in_pool = False
    p.can_grow_pool = False
    p.candidates_per_arm = 50
    p.timing_ms = {"total": 1.0}
    p.next_keyset = None
    return p


def _searcher() -> MagicMock:
    s = MagicMock()
    s.config.candidates_per_arm = 50
    s.config.candidates_per_arm_max = 800
    s.search.return_value = _page()
    return s


# ---- The rule, pure -----------------------------------------------------


@pytest.mark.parametrize("text", ["invoice", "  invoice  ", "a"])
def test_free_text_means_a_text_walk(text: str) -> None:
    assert walk_for_text(text) == "text"


@pytest.mark.parametrize("text", ["", "   ", "\t\n"])
def test_blank_text_means_an_archive_walk(text: str) -> None:
    """Whitespace is blank — the same reading the branch predicate uses.

    Both come from this one function, so the cursor cannot record a walk
    the query did not take.
    """
    assert walk_for_text(text) == "archive"


def test_a_text_cursor_with_a_blank_query_is_an_error() -> None:
    assert keyset_walk_error(cursor_walk="text", free_text="") is not None


def test_a_text_cursor_with_its_query_is_fine() -> None:
    assert keyset_walk_error(cursor_walk="text", free_text="invoice") is None


@pytest.mark.parametrize("text", ["", "invoice"])
def test_an_archive_cursor_accepts_either(text: str) -> None:
    """#322's blank-query paging must stay reachable.

    The archive walk has no FTS predicate to rebuild, so nothing about the
    query bears on continuing it. Refusing here would forbid exactly the
    pagination #322 added, which is why the rule is keyed on the cursor's
    own walk rather than on "a cursor plus a blank query".
    """
    assert keyset_walk_error(cursor_walk="archive", free_text=text) is None


def test_the_error_names_the_remedy() -> None:
    """The caller's fix is to re-send the query, so the line has to say so.

    ``docs/mcp-usage.md`` already prescribes it; an agent that skipped that
    line is the reported audience, and a bare "invalid cursor" sends them
    to mint a new one instead — which restarts the walk, the very outcome
    this refusal exists to prevent.
    """
    msg = keyset_walk_error(cursor_walk="text", free_text="")
    assert msg is not None
    assert "query" in msg


# ---- The wire ------------------------------------------------------------


@pytest.mark.parametrize("walk", ["text", "archive"])
@pytest.mark.parametrize("order", ["asc", "desc"])
def test_the_cursor_round_trips_both_axes(walk: KeysetWalk, order: str) -> None:
    ks, raw = _cursor(walk, order)
    assert decode_keyset_cursor(raw) == ks


def test_a_legacy_cursor_reads_as_an_archive_walk() -> None:
    """``K|`` and ``KA|`` predate the distinction and stay lenient.

    An in-flight cursor minted before this change could have come from
    either walk, so the two readings trade differently: archive leaves
    #326 open for that one paging session, while text would manufacture a
    400 for a caller correctly paging a blank-query walk — breaking a
    feature that shipped hours earlier. The module's standing rule is that
    no cursor in flight changes meaning, and lenient is the reading that
    keeps it.
    """
    ks, raw = _cursor("archive")
    assert raw.startswith("K|"), raw
    assert decode_keyset_cursor(raw).walk == "archive"
    asc_raw = encode_keyset_cursor(replace(ks, order="asc"))
    assert asc_raw.startswith("KA|"), asc_raw
    assert decode_keyset_cursor(asc_raw).walk == "archive"


def test_no_keyset_prefix_is_a_prefix_of_another() -> None:
    """What lets the scan classify a cursor whatever order it runs in.

    Every prefix ends in the ``|`` terminator and contains no other, so a
    shorter one can never match inside a longer one. Adding a fourth
    without that property would silently reclassify cursors, so the
    property is asserted rather than left to the comment beside the table.
    """
    from localmail.api.search_cursor import _KEYSET_PREFIXES

    prefixes = [p for p, _, _ in _KEYSET_PREFIXES]
    assert len(set(prefixes)) == len(prefixes)
    for a in prefixes:
        assert a.endswith("|") and a.count("|") == 1, a
        for b in prefixes:
            if a is not b:
                assert not b.startswith(a), (a, b)


# ---- End to end through run_search --------------------------------------


def test_paging_a_text_cursor_without_the_query_is_refused() -> None:
    """#326's headline: silently walking the whole archive is over."""
    s = _searcher()
    _, cursor = _cursor("text")
    with pytest.raises(ValidationFailed, match="query"):
        run_search(searcher=s, free_text="", filters={}, limit=2,
                   allowed_account_ids=[1], user_id=99, cursor=cursor)
    s.search.assert_not_called()


def test_a_query_of_only_filter_operators_does_not_count_as_the_query() -> None:
    """Measured on ``parse_query(free_text).free_text``, not the raw field.

    ``subject:invoice`` is a non-empty request field that parses down to no
    free text, so the walk would rebuild no FTS predicate from it. Two
    predicates for one rule is what produced the #308 follow-up defect,
    where the api gate and the retrieval branch disagreed about what
    counted as a blank query — they ask the same question here.
    """
    s = _searcher()
    _, cursor = _cursor("text")
    with pytest.raises(ValidationFailed, match="query"):
        run_search(searcher=s, free_text="subject:invoice", filters={}, limit=2,
                   allowed_account_ids=[1], user_id=99, cursor=cursor)
    s.search.assert_not_called()


def test_paging_a_text_cursor_with_the_query_still_advances() -> None:
    s = _searcher()
    incoming, cursor = _cursor("text")
    run_search(searcher=s, free_text="invoice", filters={}, limit=2,
               allowed_account_ids=[1], user_id=99, cursor=cursor)
    _, kwargs = s.search.call_args
    assert kwargs.get("keyset_cursor") == incoming


def test_paging_an_archive_cursor_with_a_blank_query_still_advances() -> None:
    """The positive control for #322's feature.

    Without it a rule matching too broadly would close #326 by reopening
    the thing #322 shipped, and every refusal assertion above would still
    pass.
    """
    s = _searcher()
    incoming, cursor = _cursor("archive")
    run_search(searcher=s, free_text="", filters={}, limit=2,
               allowed_account_ids=[1], user_id=99, cursor=cursor)
    _, kwargs = s.search.call_args
    assert kwargs.get("keyset_cursor") == incoming


def test_the_refusal_precedes_the_empty_acl_short_circuit() -> None:
    """A grant-nothing caller must see the 400, not an empty page.

    That branch answers byte-identically to "you have reached the end of
    your results", so reaching it first would tell a caller their
    contradictory request had succeeded and was complete — the same
    reasoning the sort-mismatch guard beside it is ordered by.
    """
    s = _searcher()
    _, cursor = _cursor("text")
    with pytest.raises(ValidationFailed):
        run_search(searcher=s, free_text="", filters={}, limit=2,
                   allowed_account_ids=[], user_id=99, cursor=cursor)
    s.search.assert_not_called()


# ---- End to end against a real archive ----------------------------------
#
# The mocked tests above prove the *rule*. What they cannot see is whether a
# real walk stamps the cursor with the walk it actually ran — and it does not
# take a subtle mistake to break that: replacing `walk=walk` with a constant
# in `_date_keyset_search` left every test above green.


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


def _seed_archive(conn, count: int = 6) -> tuple[int, list[int]]:
    """``count`` matching messages, newest first. Returns (account_id, ids)."""
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
        account_id = int(row[0])
        for i in range(count):
            cur.execute(
                "INSERT INTO messages (account_id, message_id, raw_sha256, subject,"
                " body_text, headers, raw_bytes, size_bytes, internal_date)"
                " VALUES (%s, %s, %s, %s, 'body', '{}'::jsonb, 'r', 1, %s)"
                " RETURNING id",
                (account_id, f"<m{i}>", bytes([i + 1]) * 32,
                 f"e-ticket booking #{i:02d}", now - timedelta(hours=i)),
            )
            row = cur.fetchone()
            assert row is not None
            ids.append(int(row[0]))
    conn.commit()
    return account_id, ids


def _searcher_over(db_dsn):
    from localmail.config import SearchConfig
    from localmail.db import open_pool
    from localmail.search.searcher import Searcher

    pool = open_pool(db_dsn)
    return Searcher(pool=pool, cfg=SearchConfig(), embeddings=_Embedder(),
                    reranker=None, rewriter=None), pool


@pytest.mark.parametrize(
    ("query", "expected"), [("e-ticket", "text"), ("", "archive")],
)
def test_a_real_walk_stamps_the_cursor_with_the_walk_it_ran(
    db_dsn, db_conn, query: str, expected: KeysetWalk,
) -> None:
    """The stamp must come from the branch, not from a constant.

    ``_date_keyset_search`` derives both from one ``walk_for_text`` call so
    they cannot disagree — but nothing checked the derivation itself, and a
    constant in its place kept every mocked test in this file green. Both
    values are asserted from one parametrize so a constant of *either*
    flavour fails.
    """
    account_id, _ = _seed_archive(db_conn)
    searcher, pool = _searcher_over(db_dsn)
    try:
        page = searcher.search(query, allowed_account_ids=[account_id],
                               page_size=3, user_id=1, sort="date")
    finally:
        pool.close()
    assert page.next_keyset is not None, "the walk did not page; nothing was stamped"
    assert page.next_keyset.walk == expected


def test_paging_a_real_text_search_without_the_query_is_refused(
    db_dsn, db_conn,
) -> None:
    """#326 end to end, in the shape a client actually pages in.

    ``docs/mcp-usage.md`` says to call again with ``next_cursor``. A client
    that also drops ``query`` used to be served the next slice of the whole
    archive — here, the same six messages a blank walk would return —
    presented as a continuation of the text search.
    """
    account_id, ids = _seed_archive(db_conn)
    searcher, pool = _searcher_over(db_dsn)
    try:
        first = run_search(searcher=searcher, free_text="e-ticket", filters={},
                           limit=3, allowed_account_ids=[account_id], user_id=1,
                           sort="date")
        assert first["next_cursor"] is not None
        with pytest.raises(ValidationFailed, match="query"):
            run_search(searcher=searcher, free_text="", filters={}, limit=3,
                       allowed_account_ids=[account_id], user_id=1,
                       cursor=first["next_cursor"])
        # And the correct call still advances, so the refusal is not blanket.
        second = run_search(searcher=searcher, free_text="e-ticket", filters={},
                            limit=3, allowed_account_ids=[account_id], user_id=1,
                            cursor=first["next_cursor"])
    finally:
        pool.close()
    assert [r["message_id"] for r in second["results"]] == [str(i) for i in ids[3:]]


def test_a_malformed_payload_is_a_400_even_with_an_empty_acl() -> None:
    """Decoding moved ahead of the grant-nothing short-circuit (#326).

    ``resolve_cursor_plan`` reads the whole cursor now, not just its
    prefix, so a cursor whose *payload* is corrupt is refused where this
    module says a malformed paging request belongs: before the empty-ACL
    branch, which answers with an empty page byte-identical to "you have
    reached the end of your results".

    The positive control matters more than usual here: an empty ACL returns
    an empty page for every well-formed request, so without it this would
    pass against a `run_search` that refused *every* cursor.
    """
    s = _searcher()
    with pytest.raises(ValidationFailed):
        run_search(searcher=s, free_text="invoice", filters={}, limit=2,
                   allowed_account_ids=[], user_id=99,
                   cursor="K|not-valid-base64-payload")
    s.search.assert_not_called()

    # Positive control: a well-formed cursor with the same empty ACL is
    # answered, not refused.
    _, good = _cursor("archive")
    out = run_search(searcher=s, free_text="invoice", filters={}, limit=2,
                     allowed_account_ids=[], user_id=99, cursor=good)
    assert out["results"] == []
    s.search.assert_not_called()
