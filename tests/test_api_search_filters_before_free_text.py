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

Every filter token is self-contained: ids are digits, dates are validated,
``lang`` is refused if it carries whitespace or a quote, quoted values have
their quotes stripped and may not be empty, and ``has:`` is a constant. So
emitting them first leaves the tokenizer in its initial state when the free
text starts, and whatever the free text does, it can only affect itself.

#366 refused the case where the swallowed text failed to *parse*, naming the
open quote. That refusal is retired with the fix: an unclosed quote at the
end swallows nothing, so those queries are served. Every test here fails if
the composer goes back to free-text-first.
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
#: first three are ordinary English and are what #367 reports; ``has:"`` and
#: ``from:"`` are shapes #366 refused (an operator with no value is free text).
_OPEN_QUOTE_TEXT = [
    "O'Brien contract", "don't forget", "bob's invoice",
    'invoice "unclosed phrase', "'", '"', 'has:"', 'from:"',
]

#: Open-quote queries whose quoted run *is* an operator value. #366 refused
#: these too; they parse on their own, so they must be served.
_OPEN_QUOTE_OPERATORS = ['invoice after:"2024-01-01', "invoice has:'attachment"]

#: One value for every filter key, so a token that is swallowed is noticed
#: whichever position it was emitted in.
_EVERY_FILTER = {
    "account_ids": ["1", "2"], "folder_ids": ["5"],
    "from": "alice@example.com", "to": "bob@example.com",
    "subject": "q3 report", "date_from": "2024-01-01", "date_to": "2024-12-31",
    "lang": "en",
}


@pytest.mark.parametrize("has_attachment", [True, False, None])
@pytest.mark.parametrize("free_text", _OPEN_QUOTE_TEXT)
def test_the_free_text_cannot_change_what_the_filters_parse_to(
    free_text: str, has_attachment: bool | None,
) -> None:
    """The headline property, over every filter key at once.

    Compared against the same filters composed with no free text at all, so
    the assertion does not restate the parser.
    """
    filters = {**_EVERY_FILTER, "has_attachment": has_attachment}
    alone = parse_query(build_query_string(free_text="", filters=filters))
    composed = parse_query(build_query_string(free_text=free_text,
                                              filters=filters))
    assert composed.filters == alone.filters


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
    """Their relative order is untouched: ``date_from`` is still emitted
    after ``after`` and still wins. Only query-versus-filter moved."""
    parsed = parse_query(build_query_string(
        free_text="x", filters={"after": "2020-01-01", "date_from": "2024-06-01"},
    ))
    assert parsed.filters.after == date(2024, 6, 1)


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
    """Driven through a **real** Searcher, whose own ``parse_query`` of the
    composed query is what used to raise a bare ``QueryParseError`` (a 500)
    before #366 and a named 400 after it.
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
