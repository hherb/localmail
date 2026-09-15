# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Filters are composed ahead of the free text, so a query cannot swallow them (#367).

``build_query_string`` used to put the caller's free text first and the
filter tokens after it. ``parse_query``'s tokenizer treats both ``'`` and
``"`` as quote openers that run to the end of the string, so an apostrophe
(``O'Brien``, ``don't``, ``bob's``) swallowed every filter token, and the
ACL's ``account_id:`` tokens, into the last free-text token. When the result
still parsed, the request was answered 200 with its filters gone. A folder
chosen in the GUI's tree was ignored, and the swallowed tokens became FTS
terms and polluted the embedding.

Every filter token is self-contained: ids are digits, dates are one ASCII
token, ``lang`` is refused if it carries whitespace or a quote, quoted
values have their ``"`` stripped and may not be empty, and ``has:`` is a
constant. So emitting them first leaves the tokenizer in its initial state
when the free text starts, and no quote in the free text can reach a filter
token. (Its *operators* still can: a ``from:`` in the query out-votes the
``from`` filter, pinned below.)

#366 refused the case where the swallowed text failed to *parse*, naming the
open quote. That refusal is retired with the fix: an unclosed quote at the
end swallows nothing, so those queries are served. The tests that pin the
ordering fail if the composer goes back to free-text-first; the served-case
tests and the early-gate refusals are order-independent on purpose, pinning
that the fix neither over-refuses nor stops refusing.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import MagicMock

import pytest

from localmail.api.errors import ValidationFailed
from localmail.api.search import build_query_string, run_search
from localmail.api.search_cursor import encode_keyset_cursor
from localmail.config import SearchConfig
from localmail.search.query import parse_query
from localmail.search.searcher import KeysetCursor, Searcher


#: Queries whose quote is left open and which set no filter of their own. The
#: first three are ordinary English and are what #367 reports. ``has:"`` is a
#: shape #366 refused; ``from:"`` it served, with the filters swallowed, since
#: ``from:`` takes any value. An operator with no value is free text.
_OPEN_QUOTE_TEXT = [
    "O'Brien contract", "don't forget", "bob's invoice",
    'invoice "unclosed phrase', "'", '"', 'has:"', 'from:"',
]

#: Open-quote queries whose quoted run *is* an operator value. #366 refused
#: these too; they parse on their own, so they must be served.
_OPEN_QUOTE_OPERATORS = ['invoice after:"2024-01-01', "invoice has:'attachment"]

#: A value for every filter token kind, so a token that is swallowed is
#: noticed whichever position it was emitted in. ``after``/``before`` emit the
#: same tokens as their ``date_from``/``date_to`` aliases, and
#: ``has_attachment`` is parametrized by the test. The quoted values carry an
#: apostrophe and an unbalanced ``"``: a filter token is emitted ahead of the
#: free text, so a quote it leaves open would swallow that instead.
_EVERY_FILTER = {
    "account_ids": ["1", "2"], "folder_ids": ["5"],
    "from": "o'brien@example.com", "to": 'bob "the builder',
    "subject": "don't forget", "date_from": "2024-01-01", "date_to": "2024-12-31",
    "lang": "en",
}


@pytest.mark.parametrize("has_attachment", [True, False, None])
@pytest.mark.parametrize("free_text", _OPEN_QUOTE_TEXT)
def test_no_quote_in_the_free_text_can_reach_a_filter_token(
    free_text: str, has_attachment: bool | None,
) -> None:
    """The headline property, over every filter token kind at once.

    Compared against the same filters composed with no free text at all, so
    the assertion does not restate the parser.
    """
    filters = {**_EVERY_FILTER, "has_attachment": has_attachment}
    alone = parse_query(build_query_string(free_text="", filters=filters))
    composed = parse_query(build_query_string(free_text=free_text,
                                              filters=filters))
    assert composed.filters == alone.filters


def test_every_filter_parses_to_its_own_value_with_no_free_text() -> None:
    """The anchor the test above compares against. A quote left open by a
    filter value corrupts that anchor and the composition identically, so
    the comparison alone passes for it."""
    f = parse_query(build_query_string(free_text="", filters=_EVERY_FILTER)).filters
    assert (f.account_ids, f.folder_ids) == ([1, 2], [5])
    assert (f.from_substr, f.to_substr, f.subject_substr) == (
        "o'brien@example.com", "bob the builder", "don't forget")
    assert (f.after, f.before) == (date(2024, 1, 1), date(2024, 12, 31))
    assert f.languages == ["en"]


def test_the_reproduction_from_367_keeps_every_filter() -> None:
    parsed = parse_query(build_query_string(
        free_text="O'Brien contract",
        filters={"has_attachment": True, "date_from": "2024-01-01",
                 "folder_ids": ["5"], "account_ids": ["1"]},
    ))
    assert parsed.filters.has_attachment is True
    assert parsed.filters.after == date(2024, 1, 1)
    assert parsed.filters.folder_ids == [5]
    assert parsed.filters.account_ids == [1]
    # Compared with the query parsed on its own rather than a literal: what
    # the tokenizer does to the apostrophe itself is a separate question.
    assert parsed.free_text == parse_query("O'Brien contract").free_text


def test_has_attachment_false_survives_a_contraction() -> None:
    parsed = parse_query(build_query_string(
        free_text="don't forget", filters={"has_attachment": False},
    ))
    assert parsed.filters.has_attachment is False


def test_the_filter_tokens_lead_the_composed_query() -> None:
    """The structural half, so a reorder is caught here by name rather than
    only through the parser."""
    assert build_query_string(
        free_text="O'Brien", filters={"folder_ids": ["5"], "has_attachment": True},
    ) == "folder_id:5 has:attachment O'Brien"


# --- the precedence consequence, recorded on purpose -----------------------

def test_a_query_operator_now_outvotes_a_conflicting_structured_filter() -> None:
    """A deliberate consequence of the reorder, pinned so it is not later
    rediscovered as a regression.

    ``parse_query`` keeps the **last** value for a scalar operator, so a
    conflict between the query and a structured filter used to go to the
    filter, which was emitted last, and now goes to the query. Both are the
    same silent last-token-wins that #369 exists to end; this fix chose not
    to add machinery preserving one silent winner over the other. ``has:``
    already refuses a conflict, and the list-valued filters are unioned, so
    only these scalars move.
    """
    parsed = parse_query(build_query_string(free_text="from:bob",
                                            filters={"from": "alice"}))
    assert parsed.filters.from_substr == "bob"
    parsed = parse_query(build_query_string(free_text="after:2020-01-01",
                                            filters={"date_from": "2024-06-01"}))
    assert parsed.filters.after == date(2020, 1, 1)


def test_a_conflict_between_two_structured_filters_is_unchanged() -> None:
    """Their relative order is untouched: ``date_from`` and ``date_to`` are
    still emitted after ``after`` and ``before`` and still win. Only
    query-versus-filter moved."""
    parsed = parse_query(build_query_string(
        free_text="x", filters={"after": "2020-01-01", "date_from": "2024-06-01",
                                "before": "2025-01-01", "date_to": "2024-09-01"},
    ))
    assert parsed.filters.after == date(2024, 6, 1)
    assert parsed.filters.before == date(2024, 9, 1)


# --- run_search hands the Searcher the filters -----------------------------

_TEXT_WALK_CURSOR = encode_keyset_cursor(KeysetCursor(
    ts=datetime(2026, 1, 1, tzinfo=timezone.utc), id=5, order="desc", walk="text",
))


def _recording_searcher() -> MagicMock:
    s = MagicMock()
    s.search.side_effect = AssertionError("search reached")
    s.smart_available = False
    return s


@pytest.mark.parametrize("cursor", [None, _TEXT_WALK_CURSOR],
                         ids=["fresh", "keyset"])
def test_both_rowed_branches_hand_the_searcher_every_filter(cursor) -> None:
    """The query the Searcher parses is the ACL-scoped composition, which is
    the one carrying ``account_id:`` tokens. Both of them must survive."""
    s = _recording_searcher()
    with pytest.raises(AssertionError, match="search reached"):
        run_search(searcher=s, free_text="O'Brien contract",
                   filters={"folder_ids": ["5"], "has_attachment": True},
                   limit=5, allowed_account_ids=[3, 7], user_id=1, cursor=cursor)
    parsed = parse_query(s.search.call_args.args[0])
    assert parsed.filters.folder_ids == [5]
    assert parsed.filters.has_attachment is True
    assert parsed.filters.account_ids == [3, 7]


@pytest.mark.parametrize("cursor", [None, _TEXT_WALK_CURSOR],
                         ids=["fresh", "keyset"])
def test_a_parse_error_from_the_searcher_is_still_a_400(cursor) -> None:
    """The backstop for the gate. ``Searcher.search`` parses the ACL-scoped
    composition itself, and ``QueryParseError`` is a bare ``ValueError`` that
    ``serve.app`` would answer as a 500. No input reaches it today, because
    that composition differs from the gate's only by digit tokens, so the
    failure is injected. #366's per-branch re-parse used to be this backstop;
    it went with the open-quote refusal, and nothing noticed."""
    from localmail.search.query import QueryParseError

    s = MagicMock()
    s.smart_available = False
    s.search.side_effect = QueryParseError("has: a divergence the gate missed")
    with pytest.raises(ValidationFailed, match="a divergence the gate missed"):
        run_search(searcher=s, free_text="invoice", filters={}, limit=5,
                   allowed_account_ids=[1], user_id=1, cursor=cursor)
    s.search.assert_called_once()


# --- the shapes #366 refused are served now --------------------------------

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


def _searcher() -> Searcher:
    pool = MagicMock()
    # Reaching this means every guard, including the Searcher's own parse,
    # accepted the query.
    pool.connection.side_effect = AssertionError("retrieval was reached")
    return Searcher(pool=pool, cfg=SearchConfig(), embeddings=_Embeddings(),
                    reranker=None, rewriter=None)


def _served_cases() -> list:
    """Every open-quote query on the fresh branch, and on the keyset branch
    every one that has free text. ``'`` and ``"`` alone are textless, and a
    text-walk cursor refuses a textless query (#326) — correctly, before
    retrieval — so those are not keyset cases at all."""
    queries = _OPEN_QUOTE_TEXT + _OPEN_QUOTE_OPERATORS
    fresh = [pytest.param(q, None, id=f"fresh-{q}") for q in queries]
    keyset = [pytest.param(q, _TEXT_WALK_CURSOR, id=f"keyset-{q}")
              for q in queries if parse_query(q).free_text.strip()]
    return fresh + keyset


@pytest.mark.parametrize("free_text, cursor", _served_cases())
def test_an_open_quote_is_served_rather_than_refused(free_text, cursor) -> None:
    """Driven through a **real** Searcher, so every guard including its own
    ``parse_query`` of the composed query must accept it.

    Before #367, on the fresh branch only three of these were refused
    (``has:"``, ``invoice after:"2024-01-01`` and ``invoice has:'attachment``:
    a 400 from #366's per-branch re-parse), and on the keyset branch
    ``from:"`` too (#326's walk guard, the swallowed tokens leaving no free
    text); the rest were served with the filters silently swallowed. This pins only that they are served, which a
    refusal reintroduced for open quotes would fail. That the filters reach
    the Searcher is ``test_both_rowed_branches_hand_the_searcher_every_filter``.
    """
    with pytest.raises(AssertionError, match="retrieval was reached"):
        run_search(searcher=_searcher(), free_text=free_text,
                   filters={"subject": "x"}, limit=5,
                   allowed_account_ids=[3, 7], user_id=1, cursor=cursor)


# --- what the early gate still refuses -------------------------------------

@pytest.mark.parametrize("allowed", [[1], []])
def test_a_malformed_date_in_the_query_is_still_a_400(allowed) -> None:
    """``[]`` because the gate runs ahead of the empty-ACL short-circuit."""
    with pytest.raises(ValidationFailed, match="after: expected YYYY-MM-DD"):
        run_search(searcher=_searcher(), free_text='invoice after:"2024-13-01',
                   filters={}, limit=5, allowed_account_ids=allowed, user_id=1)


@pytest.mark.parametrize("allowed", [[1], []])
def test_a_has_contradiction_between_query_and_filter_is_still_a_400(
    allowed,
) -> None:
    """It exists only in the composed string, which is why the early gate
    parses the composition rather than the raw field."""
    with pytest.raises(ValidationFailed, match="contradict each other"):
        run_search(searcher=_searcher(), free_text="invoice has:attachment",
                   filters={"has_attachment": False}, limit=5,
                   allowed_account_ids=allowed, user_id=1)


# --- against a real archive ------------------------------------------------
#
# Every test above uses a mock Searcher or one whose pool raises, so none of
# them shows rows being restricted. This does, through both entry points a
# wire caller reaches: `run_search` (HTTP) and the MCP tool wrapper.

class _ConstantEmbeddings:
    name = "s"
    model = "s"
    dimension = 768

    def embed_documents(self, texts):
        return [[1.0] * 768 for _ in texts]

    def embed_query(self, text):
        return [0.5] * 768

    def health_check(self) -> None:
        pass


def _seed_two_folders(conn) -> tuple[int, int, int, int]:
    """One account, folders A and B, one matching message in each.

    The body is spelled ``OBrien``: the tokenizer drops the apostrophe from
    the query (#373), so a body spelled ``O'Brien`` would match neither
    before the fix nor after it.
    """
    with conn.cursor() as cur:
        cur.execute("INSERT INTO accounts (name, email_address, imap_host,"
                    " auth_method) VALUES ('a', 'a@x', 'h', 'password')"
                    " RETURNING id")
        row = cur.fetchone()
        assert row is not None
        acct = row[0]
        ids = []
        for n, folder in enumerate(("A", "B"), start=1):
            cur.execute("INSERT INTO mailboxes (account_id, name) VALUES"
                        " (%s, %s) RETURNING id", (acct, folder))
            row = cur.fetchone()
            assert row is not None
            mailbox = row[0]
            cur.execute(
                "INSERT INTO messages (account_id, message_id, raw_sha256,"
                " raw_bytes, size_bytes, headers, attachments, subject,"
                " body_text, internal_date)"
                " VALUES (%s, %s, %s, 'r', 1, '{}'::jsonb, '[]'::jsonb,"
                " 'contract', 'OBrien contract', %s) RETURNING id",
                (acct, f"<m{n}@x>", bytes([n]) * 32,
                 datetime(2026, 3, n, tzinfo=timezone.utc)))
            row = cur.fetchone()
            assert row is not None
            message = row[0]
            cur.execute("INSERT INTO message_labels (message_id, mailbox_id, uid)"
                        " VALUES (%s, %s, %s)", (message, mailbox, n))
            ids += [mailbox, message]
    conn.commit()
    return acct, ids[0], ids[1], ids[3]


def _run_search(searcher, acct, **kw):
    return run_search(searcher=searcher, allowed_account_ids=[acct], user_id=1,
                      limit=10, **kw)


def _tool_search(searcher, acct, *, free_text, **kw):
    pytest.importorskip("mcp")  # localmail.mcp imports the SDK
    from localmail.mcp.tools import tool_search

    return tool_search(searcher=searcher, allowed_account_ids=[acct], user_id=1,
                       limit=10, query=free_text, **kw)


@pytest.mark.parametrize("sort", [None, "date"])
@pytest.mark.parametrize("entry", [_run_search, _tool_search],
                         ids=["run_search", "mcp"])
def test_a_folder_filter_restricts_the_rows_of_an_apostrophe_query(
    db_dsn, db_conn, entry, sort,
) -> None:
    """#367's report, end to end: a folder chosen in the tree, and a query
    with an apostrophe in it.

    The assertion is equality with a non-empty set, not "B's row is
    absent". Before the fix the swallowed ``folder_id:`` and ``account_id:``
    tokens became FTS terms, so the lexical walk matched **nothing** — an
    absence assertion would have passed against the bug.
    """
    from localmail.db import open_pool

    acct, folder_a, message_a, message_b = _seed_two_folders(db_conn)
    pool = open_pool(db_dsn)
    try:
        searcher = Searcher(pool=pool, cfg=SearchConfig(),
                            embeddings=_ConstantEmbeddings(), reranker=None)
        unfiltered = entry(searcher, acct, free_text="O'Brien contract",
                           filters={}, sort=sort)
        filtered = entry(searcher, acct, free_text="O'Brien contract",
                         filters={"folder_ids": [str(folder_a)]}, sort=sort)
    finally:
        pool.close()
    assert {r["message_id"] for r in filtered["results"]} == {str(message_a)}
    # The control: without the folder both messages match, so the query text
    # itself is not what narrows the result.
    assert {r["message_id"] for r in unfiltered["results"]} == {
        str(message_a), str(message_b)}
