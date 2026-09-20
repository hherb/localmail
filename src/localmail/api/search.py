# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""HTTP-friendly wrapper over localmail.search.Searcher.

Filter dicts from the HTTP layer get translated to the DSL query string the
existing Searcher already knows how to parse, plus pagination state is
flattened into a cursor string.
"""
from __future__ import annotations

import difflib
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from localmail.api.errors import SearchCursorExpired, ValidationFailed
from localmail.api.ids import parse_int_id
from localmail.api.search_cursor import (
    SearchCursor,
    decode_keyset_cursor,
    decode_search_cursor,
    encode_keyset_cursor,
    encode_search_cursor,
    reject_pool_sort_mismatch,
    resolve_cursor_plan,
)
from localmail.api.search_projection import fields_error, project_hit
from localmail.config import SearchConfig
from localmail.search.query import ParsedQuery, QueryParseError, parse_query
from localmail.search.snippet_width import snippet_width_error
from localmail.search.page_cache import CacheMissError, PageOutOfPoolError
from localmail.search.rewrite_status import (
    CONTINUATION_PAGE,
    NOT_ATTEMPTED,
    NOT_CONFIGURED,
    NOT_REQUESTED,
    UNAVAILABLE,
    note_for_code,
    rewrite_skipped_for_status,
)
from localmail.search.argument_errors import SearchArgumentRefused
from localmail.search.sort_axes import (
    is_rankable,
    resolve_sort,
    sort_membership_error,
)
from localmail.search.searcher import (
    SearchPage,
    SearchResult,
    Searcher,
    SortMode,
    SortOrder,
)


_SUPPORTED_FILTER_KEYS = frozenset({
    "from", "to", "subject", "after", "before", "has_attachment",
    "account_ids", "folder_ids",
    "date_from", "date_to", "lang",
})


def filter_key_error(filters: Mapping[str, object]) -> str | None:
    """Name every key that is not a filter, or ``None`` when all of them are.

    Refused by name rather than dropped (#364): an ignored key answers a
    filtered question with unfiltered results and a 200. A key is judged
    whatever its value, ``null`` included. The suggestion is there because a
    likely slip is sending the hit field's name, ``has_attachments``, where
    the filter is ``has_attachment`` (the one #364 reports).
    """
    unknown = sorted(str(k) for k in filters if k not in _SUPPORTED_FILTER_KEYS)
    if not unknown:
        return None
    supported = sorted(_SUPPORTED_FILTER_KEYS)
    described = []
    for key in unknown:
        close = difflib.get_close_matches(key, supported, n=1, cutoff=0.8)
        described.append(f"{key!r} (did you mean {close[0]!r}?)" if close else repr(key))
    noun = "key" if len(unknown) == 1 else "keys"
    return (f"filters: unknown {noun} {', '.join(described)}; "
            f"supported: {', '.join(supported)}")


def build_query_string(*, free_text: str, filters: dict[str, Any]) -> str:
    """Compose filter DSL tokens + `free_text` into a single query string.

    **The filter tokens come first (#367).** `parse_query`'s tokenizer treats
    `'` and `"` as quote openers that run to the end of the string, so with
    the free text first an apostrophe (`O'Brien`, `don't`) swallowed every
    filter token after it, the ACL's `account_id:` tokens included; when the
    swallowed text still parsed, the request was answered 200 with its
    filters gone. Every filter token is self-contained (ids are ASCII digits,
    dates one ASCII token, `has:` a constant, `lang` refused if it
    carries whitespace or a quote, quoted values stripped of `"` and
    non-empty), so the free text starts with the tokenizer in its initial
    state and no quote in it can reach a filter token. Hence
    ``parse_query(composed).free_text == parse_query(free_text).free_text``
    for every input the composition parses, which is what lets `run_search`'s
    gate and `Searcher.search` read one free text.

    That is a claim about tokenization, not precedence: a scalar operator in
    `free_text` (`from:`, `after:`, …) out-votes a conflicting structured
    filter, because `parse_query` keeps the last value and the free text is
    last. That is the silent last-token-wins #369 exists to end; `has:`
    already refuses a conflict, and list-valued filters are unioned.

    Raises `ValidationFailed` for a key that is not a filter
    (`filter_key_error`) and for a malformed value: dates must be YYYY-MM-DD,
    `lang` a non-empty code with no whitespace or quotes, ids strict digit
    strings, `has_attachment` true, false or null, and a quoted value
    non-empty once its quotes are stripped. Unknown keys and malformed values
    are refused rather than dropped (#364). An empty string for `from`, `to`,
    `subject` or a date, and an empty id list, is an absent filter; an empty
    `lang` is refused.
    """
    key_error = filter_key_error(filters)
    if key_error is not None:
        raise ValidationFailed(key_error)
    parts = _filter_tokens(filters)
    if free_text:
        parts.append(free_text)
    return " ".join(parts)


def _filter_tokens(filters: dict[str, Any]) -> list[str]:
    out: list[str] = []
    if (vs := filters.get("account_ids")):
        for v in vs:
            out.append(f"account_id:{parse_int_id(str(v), field='account_id')}")
    if (vs := filters.get("folder_ids")):
        for vs_v in vs:
            out.append(f"folder_id:{parse_int_id(str(vs_v), field='folder_id')}")
    if (v := filters.get("from")):
        out.append(f'from:{_quote_value(v, "from")}')
    if (v := filters.get("to")):
        out.append(f'to:{_quote_value(v, "to")}')
    if (v := filters.get("subject")):
        out.append(f'subject:{_quote_value(v, "subject")}')
    if (v := filters.get("after")):
        _validate_date(v, "after")
        out.append(f"after:{v}")
    if (v := filters.get("before")):
        _validate_date(v, "before")
        out.append(f"before:{v}")
    if (v := filters.get("date_from")):
        _validate_date(v, "date_from")
        out.append(f"after:{v}")
    if (v := filters.get("date_to")):
        _validate_date(v, "date_to")
        out.append(f"before:{v}")
    if "lang" in filters:
        lang_v = filters["lang"]
        if lang_v is not None and lang_v != []:
            values = lang_v if isinstance(lang_v, list) else [lang_v]
            for one in values:
                s = str(one).strip().lower()
                if not s:
                    raise ValidationFailed("lang: empty value not allowed")
                # Emitted unquoted, so whitespace or a quote here re-tokenizes
                # the composed query: `en has:no-attachment` injected a filter,
                # and `en'` would open a quote swallowing every token after
                # it — the `has:` filter and the free text alike.
                if any(ch.isspace() or ch in "\"'" for ch in s):
                    raise ValidationFailed(
                        f"lang: expected a language code such as 'en', got {one!r}"
                    )
                out.append(f"lang:{s}")
    has_attachment = filters.get("has_attachment")
    if has_attachment is True:
        out.append("has:attachment")
    elif has_attachment is False:
        # Dropped before #364, though the MCP tool's description promised it
        # and `_filter_sql` already honoured it.
        out.append("has:no-attachment")
    elif has_attachment is not None:
        raise ValidationFailed(
            f"has_attachment: expected true, false or null, got {has_attachment!r}"
        )
    return out


def _quote_value(v: Any, key: str) -> str:
    """Wrap a free-form filter value in double quotes so the DSL tokenizer
    treats it as a single token.

    Without this, a value like 'alice OR account:other' would tokenize into
    three tokens and inject an extra `account:` operator, bypassing the
    requested scope. Embedded quotes and newlines have no useful meaning for
    substring filters and are stripped — the DSL has no escape syntax.

    A value with nothing left after stripping is refused: `key:""` parses as
    the free-text token `key:`, so the filter would vanish and a filter-only
    request would become a text search.
    """
    s = str(v).replace('"', "").replace("\n", " ").replace("\r", " ")
    if not s:
        raise ValidationFailed(f"{key}: value is empty once its quotes are removed")
    return f'"{s}"'


#: One ASCII token, checked before ``strptime``. A date is emitted unquoted
#: ahead of the free text, so it must be one token (#367), and ``strptime``
#: admits more than that: ``%d`` takes a space (``2020-01- 1``) and ``%Y``
#: non-ASCII digits. Unpadded month and day stay accepted, because the query
#: parser accepts ``after:2020-1-1`` and the desktop client moves a typed date
#: into a structured filter; a stricter rule here would 400 what it accepted.
_DATE_SHAPE = re.compile(r"\d{4}-\d{1,2}-\d{1,2}", re.ASCII)


def _validate_date(value: str, key: str) -> None:
    try:
        if not _DATE_SHAPE.fullmatch(value):
            raise ValueError(value)
        datetime.strptime(value, "%Y-%m-%d")
    except (TypeError, ValueError) as exc:
        raise ValidationFailed(f"{key}: expected YYYY-MM-DD, got {value!r}") from exc


def _gate_query(query: str) -> ParsedQuery:
    """Parse the query composed from the caller's filters, or refuse it.

    ``parse_query`` raises ``QueryParseError`` for a malformed ``after:``/
    ``before:`` date, an empty ``lang:`` value, and (#364) an unknown
    ``has:`` value or a contradictory ``has:`` pair. It is a bare
    ``ValueError``, and ``serve.app`` registers a handler for ``APIError``
    only — so it escaped ``run_search`` as an **unhandled 500 with no
    problem+json body**, and reached the MCP tool as an exception no
    ``ToolError`` mapping covers. ``query="invoice after:last-week"`` is
    exactly the shape an LLM agent emits.

    The **composed** string, not the raw request field, because a ``has:``
    token in the query contradicting the structured ``has_attachment``
    filter exists only there. Its free text is the raw field's
    (``build_query_string`` composes the filters first, #367), so one parse
    serves both purposes.

    The rowed branches do not re-parse. They hand ``Searcher.search`` the
    same composition from the ACL-scoped filters, which differs only in its
    ``account_id:`` tokens. Those are digits and swallow nothing, so that
    parse cannot fail where this one succeeded, and it reads the same free
    text; each branch still maps a ``QueryParseError`` from it to a 400, as a
    backstop.
    """
    try:
        return parse_query(query)
    except QueryParseError as exc:
        raise ValidationFailed(str(exc)) from exc


def run_search(
    *,
    searcher: Searcher,
    free_text: str,
    filters: dict[str, Any],
    limit: int,
    allowed_account_ids: list[int],
    user_id: int,
    sort: SortMode | None = None,
    sort_order: SortOrder | None = None,
    cursor: str | None = None,
    smart: bool = False,
    fields: list[str] | None = None,
    snippet_chars: Any = None,  # pre-gate, so genuinely any JSON value
) -> dict[str, Any]:
    """Run a search (or continue an existing one) and return the API-shaped response.

    ``cursor`` is the opaque ``next_cursor`` returned by a previous call.
    When present, ``searcher.continue_page`` serves the next page from the
    cached rerank pool with zero re-retrieval. If the page index advances
    past the cached pool's end (``PageOutOfPoolError``) and the pool can
    still be grown, the route transparently calls
    ``searcher.grow_pool(token, candidates_per_arm * 2)`` and returns its
    page 1. If the cache has been evicted (``CacheMissError``) — TTL, LRU,
    or cross-user replay — the route raises ``SearchCursorExpired`` (HTTP
    409) so the GUI can run its transparent re-search recovery.

    ``next_cursor`` in the response is ``None`` once the rerank pool is
    exhausted *and* further growth would exceed
    ``searcher.config.candidates_per_arm_max``.

    ``sort`` and ``sort_order`` are ``None`` when the caller stated none.
    With a cursor, the cursor decides both — see
    ``search_cursor.resolve_cursor_plan``, which rejects a stated value the
    cursor cannot serve instead of dropping either. Without one,
    ``sort_order`` falls to its module default and ``sort`` is resolved
    against the **query** (#324): a query with no free text — blank, or only
    filter operators — has nothing for the hybrid pool to rank, so it
    resolves to ``date``, and a *stated* ``"rank"`` for one is a 400 rather
    than a silent drop.

    ``sort_order="asc"`` pairs only with a resolved ``sort="date"``; asking
    for it on the rank path is a 400, not a quietly ignored field. Because
    the sort is resolved from the query, ``sort_order="asc"`` alone on a
    textless query is *honoured* — it used to be refused for naming a rank
    path such a request never takes.

    ``smart`` requests an LLM query rewrite on page 1 (cursor is None) when the
    searcher has a rewriter configured. The response carries ``rewrite_status``
    (a 5-value enum), an optional curated human ``rewrite_note``, and a stable
    machine-readable ``rewrite_note_code`` (``missing_model`` / ``unreachable``
    / ``unparseable`` / ``not_configured`` / ``continuation_page``, or ``None``
    when there is no note). ``rewrite_skipped`` stays True only when a requested
    rewrite did not happen (rewriter unavailable, or the rewrite call failed).

    ``fields`` projects each hit to exactly the named keys (the envelope is
    never projected); ``snippet_chars`` sizes the snippet window, between 1
    and ``search.snippet_max_chars``. Both are refused (400) before the
    empty-ACL short-circuit, for the reason every stated-argument gate is.
    See ``search_projection`` and
    docs/superpowers/specs/2026-09-19-search-hit-projection-design.md.

    ``snippet_chars`` is annotated ``Any``, not ``int | None``: the route
    types the wire field ``Any`` precisely so every JSON value reaches
    ``snippet_width_error`` uncoerced, and this is the parameter it hands
    them to — *before* the gate below narrows it. Everything downstream of
    that gate (``_continue_or_grow``, ``Searcher``) is correctly ``int``.
    """
    # Membership first, ahead of every other gate here (#348). Two reasons,
    # and the second is why this is not merely tidy.
    #
    # It closes the transport hole: `Searcher.search` checks both axes and
    # raises a plain `ValueError`, which `serve/app.py` — a handler for
    # `APIError` only — answers as an unhandled 500. HTTP and MCP both
    # declare `Literal`s so neither can reach it, but that made the property
    # an obligation on every transport rather than one this boundary holds,
    # and a third consumer (#305's CLI flags, a queue, a new route) inherits
    # the 500 with nothing failing at review time.
    #
    # And it is decided before the empty-ACL short-circuit below, which
    # answers with an empty page indistinguishable from "you have reached
    # the end" — so `sort="Date"` from a grant-nothing caller was reported
    # as a *completed* request. Every other gate over a *stated argument* is
    # already placed ahead of that branch for exactly this reason; this axis
    # was the one that was not. (`_check_pool_sort` sits deliberately after
    # it, being a probe of the cached pool rather than of an argument.)
    #
    # Ahead of `resolve_cursor_plan` too, because a value that is not a value
    # cannot meaningfully contradict a cursor: that resolver interpolates the
    # offending string into a sentence asserting what the cursor continues
    # ("this cursor continues a date-sorted search ... (got 'Date')"), which
    # sends a caller who made a typo to their paging logic — and the sentence
    # is a coincidence of which cursor was in hand. `Searcher.search` orders
    # these two the same way, so the layers cannot answer one input
    # differently.
    membership_error = sort_membership_error(sort=sort, sort_order=sort_order)
    if membership_error is not None:
        raise ValidationFailed(membership_error)

    # Slice E's two arguments, ahead of the empty-ACL short-circuit for the
    # reason the membership gate above is: that branch's empty page reads as
    # "no results", so a malformed request from a grant-nothing caller would
    # be reported as a completed one.
    if fields is not None and (error := fields_error(fields)) is not None:
        raise ValidationFailed(error)
    if snippet_chars is not None and (error := snippet_width_error(
            snippet_chars, max_chars=searcher.config.snippet_max_chars)) is not None:
        raise ValidationFailed(error)

    # Filters next, keys and then values, for the reason the gate above
    # gives: ahead of the empty-ACL short-circuit, whose empty page reads as
    # "no results". The value checks used to run only in the
    # `build_query_string` calls below that branch, so a malformed
    # `date_from` from a caller granted nothing was answered 200 (#364).
    #
    # Parsed, not merely composed: a malformed operator in `query` is a 400
    # here rather than a 500 inside `Searcher.search`, and a `has:` token
    # contradicting the structured `has_attachment` filter exists only in
    # the *composed* string — `free_text` and `filters` each look fine read
    # alone (see `_gate_query`).
    #
    # Bound to a local because its free text feeds `resolve_cursor_plan` and
    # the empty-ACL branch's `sort_applied`/`rankable`, both ahead of the
    # Searcher. It is the free text the Searcher's own #324/#326 guards read:
    # `build_query_string` composes the filters first (#367), so this parse
    # and the Searcher's parse of the ACL-scoped composition see identical
    # free text, unbalanced quotes included. Pinned on the composer by
    # `test_api_search.py::test_build_query_string_is_free_text_neutral`.
    # The Searcher's guards stay the authority for callers who never reach
    # this function; this gate exists to answer before any work is done.
    parsed_free_text = _gate_query(
        build_query_string(free_text=free_text, filters=filters)).free_text
    plan = resolve_cursor_plan(cursor=cursor, requested_sort=sort,
                               requested_sort_order=sort_order,
                               free_text=parsed_free_text)
    # Refused here as well as in the Searcher so the caller gets a clean 400
    # before any work; the Searcher's own guard is what covers CLI and
    # library callers, who never reach this function. Ahead of the empty-ACL
    # short-circuit below, which answers with an empty page indistinguishable
    # from "you have reached the end" — a contradictory request must not be
    # reported as a completed one.
    #
    # The mode conjunct keeps this judging what the *caller* stated on a fresh
    # or pool request: a keyset plan's ordering came from the cursor and was
    # validated against it above, so it is not this refusal's to re-litigate.
    # Written out rather than left to `plan.sort == "rank"` to exclude it,
    # which is true only while KEYSET_SORT happens to be "date".
    if plan.mode != "keyset" and plan.sort == "rank" and plan.sort_order == "asc":
        # The remedy differs by mode, so it is branched rather than written
        # as one string covering both. On a **fresh** request — no cursor,
        # which is the commonest way to reach this — "pass sort='date'" is
        # the whole fix, and is word for word what `Searcher.search`'s guard
        # on the same condition already says; two guards for one condition
        # disagreeing about the remedy is the drift. Telling that caller to
        # "run a fresh search" and explaining that a cursor cannot be carried
        # over are both non-sequiturs for a request that *is* fresh and
        # carries no cursor. With a **pool** cursor in hand the shorter
        # remedy is actively wrong: passing sort='date' then contradicts the
        # rank-built pool and yields a different 400, so there the fix really
        # is to start over.
        remedy = (
            "start a fresh sort='date' search for oldest-first (a cursor from "
            "a rank-ordered search cannot be carried over)"
            if plan.mode == "pool"
            else "pass sort='date' for oldest-first"
        )
        raise ValidationFailed(
            "sort_order='asc' is not applicable to sort='rank' (the "
            f"default); {remedy}"
        )

    scoped_filters = _scope_filters_by_acl(filters, allowed_account_ids)
    if scoped_filters is None:
        # total_estimate is "estimate not computed" — uniformly None across
        # every branch (#175). No rewrite was performed, so the empty-ACL
        # short-circuit reports not_requested (#176).
        #
        # `sort_applied` is present here rather than omitted, which is the
        # whole reason this key exists rather than the client inferring the
        # ordering from the returned cursor (#345): this branch returns
        # `next_cursor: None`, so an inference has no signal at all, and its
        # empty page is byte-identical to "you have reached the end".
        #
        # Both fields are resolved from the **same** string, through the same
        # `resolve_sort`/`is_rankable` pair the Searcher uses, so they cannot
        # contradict each other. `sort_applied` used to be `plan.sort` raw,
        # and `resolve_cursor_plan`'s pool arm never consults `is_rankable` —
        # so a pool cursor presented with a textless query reported
        # `sort_applied="rank"` beside `rankable=False`, the one combination
        # `is_rankable`'s docstring calls impossible, and the pair the GUI
        # renders as a checked-*and*-disabled Relevance radio above a note
        # saying there is nothing to rank. `resolve_sort` is idempotent on
        # the other two modes — fresh is already its own resolution, keyset
        # is `KEYSET_SORT`, which it returns for any query — so that pair is
        # the only value this changes.
        #
        # On the **fresh** and **keyset** modes both are what the Searcher
        # would have stamped: `parsed_free_text` is the free text it reads.
        # On **pool** mode both describe a pool this branch never consults
        # (`CursorPlan` says so).
        #
        # That one is accepted because **no rows come back**: nothing is
        # mislabelled, since nothing is labelled. The rowed paths never rely
        # on either value — `page.sort_applied` and `page.rankable` are
        # stamped by the branch that produced the rows (`Searcher.search` for
        # fresh and keyset, `continue_page` from the pool's own metadata).
        return {"results": [], "next_cursor": None, "total_estimate": None,
                "took_ms": 0.0, "rewrite_skipped": False,
                "sort_applied": resolve_sort(requested=plan.sort,
                                             free_text=parsed_free_text),
                # Present for the reason `sort_applied` is — this branch
                # returns `next_cursor: None`, so a client that inferred
                # would have nothing to infer from.
                "rankable": is_rankable(free_text=parsed_free_text),
                "rewrite_status": NOT_REQUESTED, "rewrite_note": None,
                "rewrite_note_code": None}

    cfg = searcher.config
    # smart is a page-1 signal: continuation (cursor present) reuses the
    # cached enriched parse and never re-rewrites. effective_smart guards the
    # Searcher's "no rewriter configured" RuntimeError — when smart is asked
    # for but unavailable, degrade gracefully and report rewrite_status.
    effective_smart = smart and searcher.smart_available

    # Tested on `cursor` rather than `plan.mode == "fresh"` (the resolver's
    # matching verdict) because this is what narrows `cursor` to `str` for the
    # two branches below, which decode it. The two cannot disagree: "fresh" is
    # returned for `cursor is None` and for nothing else.
    if cursor is None:
        query = build_query_string(free_text=free_text, filters=scoped_filters)
        try:
            # The caller's **raw** axes, not `plan`'s resolution of them.
            # `plan.sort` is never None, so an *unstated* sort would arrive
            # looking stated, and the Searcher's guard would refuse a caller
            # who omitted `sort` with "pass sort='date' or omit sort" — a
            # remedy they had already followed (#324's own defect). The gate
            # and the Searcher read the same free text, so the gate refuses
            # first and the two forms answer alike on the wire; forwarding the
            # caller's statement is what keeps the Searcher judging only what
            # the caller wrote, for the day they do not.
            #
            # The Searcher's guard is the authority for CLI and library
            # callers, who never reach this function. The gate is the early
            # refusal that answers before any work and before the empty-ACL
            # short-circuit.
            page = searcher.search(query, page_size=limit, user_id=user_id,
                                   sort=sort, sort_order=sort_order,
                                   smart=effective_smart,
                                   allowed_account_ids=allowed_account_ids,
                                   snippet_chars=snippet_chars)
        except SearchArgumentRefused as exc:
            # The whole family, not the members this branch can name (#344).
            # It used to enumerate `(SortNotApplicable,
            # SortOrderNotApplicable)`, with the keyset branch below naming
            # a different three — so a fifth guard added without widening a
            # tuple was an operator-facing 500, `serve.app` handling only
            # `APIError`. #342 shipped exactly that hole one branch over.
            #
            # A backstop: the gate above reads the same free text and refuses
            # first. Kept so the next divergence is a 400 rather than a 500.
            #
            # Caught by the family, never by bare ValueError — psycopg,
            # datetime and the embedding backends raise that, and
            # relabelling a real outage as a caller error would send them
            # to fix a blameless query.
            raise ValidationFailed(exc.wire_message()) from exc
        except QueryParseError as exc:
            # The Searcher's own parse of this composition, backstopped for
            # the same reason. The class is named, so nothing else is caught.
            raise ValidationFailed(str(exc)) from exc
    elif plan.mode == "keyset":
        # Keyset cursor → date-keyset continuation. The cursor carries only
        # (ts, id) and the direction it was minted in; the query + filters
        # come from the request body (the GUI re-sends them on every
        # loadMore). Both axes come from the plan rather than the caller's
        # raw arguments because the cursor's kind is what selects the date
        # path in Searcher.search — the resolver above has already rejected
        # a stated sort or order that disagrees.
        keyset = decode_keyset_cursor(cursor)
        query = build_query_string(free_text=free_text, filters=scoped_filters)
        try:
            page = searcher.search(query, page_size=limit, user_id=user_id,
                                   sort=plan.sort, sort_order=plan.sort_order,
                                   keyset_cursor=keyset,
                                   allowed_account_ids=allowed_account_ids,
                                   snippet_chars=snippet_chars)
        except SearchArgumentRefused as exc:
            # The same family as the fresh branch (#344). It used to name
            # three members and argue, per member, which were unreachable
            # here — an argument that goes stale silently and had already:
            # `SortNotApplicable` was absent, and the omission was safe only
            # because `KEYSET_SORT is TEXTLESS_SORT`, a decision made in
            # `search_cursor.py`. Catching the family retires the reasoning
            # along with the enumeration.
            #
            # #326's walk guard is the member that could arrive, and it asks
            # what `resolve_cursor_plan` already asked, of the same free text:
            # a backstop, like the fresh branch's.
            #
            # The `cursor:` prefix comes off the *exception*, not off this
            # branch (#331 point 3). Written here, it was applied to
            # everything the branch caught — so a `sort_order` refusal on a
            # request whose cursor was fine would read `cursor: sort_order=
            # 'asc' is not applicable…`, a category error. It stays
            # unreachable, but widening this catch to the whole family put
            # *more members* within reach of the mislabel — the wording rots
            # exactly where nothing can trip over it.
            #
            # Caught by the family, never by bare ValueError — psycopg,
            # datetime and the embedding backends raise that, and
            # relabelling a real outage as a cursor problem would send the
            # caller to re-send a blameless query.
            raise ValidationFailed(exc.wire_message()) from exc
        except QueryParseError as exc:
            # As on the fresh branch.
            raise ValidationFailed(str(exc)) from exc
    else:
        # No `SearchArgumentRefused` catch here, and that is a fact about this
        # branch rather than an omission: it never calls `searcher.search`.
        # `_check_pool_sort` and `_continue_or_grow` reach `get_pool_metadata`
        # / `continue_page` / `grow_pool`, which raise `CacheMissError`,
        # `PageOutOfPoolError` and `APIError` subclasses — no family member can
        # arrive. Route pool continuation through `search()` and this branch
        # needs the catch its two siblings carry, or #344 returns on the one
        # branch its fix did not touch.
        parsed = decode_search_cursor(cursor)
        # The **raw** arguments, not the plan's resolved ones: a resolved
        # default would read as a contradiction against a pool built the
        # other way, which is #312's defect exactly.
        _check_pool_sort(searcher, parsed, requested_sort=sort,
                         requested_sort_order=sort_order, user_id=user_id)
        page = _continue_or_grow(searcher, parsed, user_id=user_id, cfg=cfg,
                                 snippet_chars=snippet_chars)

    next_cursor = _next_cursor(page, cfg=cfg)
    status: str
    note: str | None
    code: str | None
    if cursor is None:
        if smart and not searcher.smart_available:
            status, code = UNAVAILABLE, NOT_CONFIGURED
            note = note_for_code(NOT_CONFIGURED)
        else:
            status = page.rewrite_status
            note = page.rewrite_note
            code = page.rewrite_note_code
    else:
        if smart:
            status, code = NOT_ATTEMPTED, CONTINUATION_PAGE
            note = note_for_code(CONTINUATION_PAGE)
        else:
            status, note, code = NOT_REQUESTED, None, None
    return {
        "results": [
            _to_api_result(r) if fields is None
            else project_hit(_to_api_result(r), fields)
            for r in page.results
        ],
        "next_cursor": next_cursor,
        "total_estimate": None,
        "took_ms": page.timing_ms.get("total", 0.0),
        # Read off the page, never re-derived here: the branch that produced
        # the rows stamped it, so this layer cannot report an ordering the
        # walk did not use (#345). Same call as `_next_cursor`'s direction.
        "sort_applied": page.sort_applied,
        # Read off the page for the same reason, and answering the question
        # `sort_applied` cannot (#353): a stated `date` is honoured for any
        # query, so the ordering alone cannot say whether rank was ever
        # available. The GUI's sort selector inferred it and re-enabled
        # Relevance on queries that have nothing to rank.
        "rankable": page.rankable,
        "rewrite_skipped": rewrite_skipped_for_status(status),
        "rewrite_status": status,
        "rewrite_note": note,
        "rewrite_note_code": code,
    }


def _check_pool_sort(
    searcher: Searcher, parsed: SearchCursor, *,
    requested_sort: SortMode | None, requested_sort_order: SortOrder | None,
    user_id: int,
) -> None:
    """Reject a stated ordering the cached pool cannot serve.

    Only reached when the caller stated one — with nothing to contradict,
    the pool stays the authority and no cache probe is spent. A miss here is
    the same expired cursor ``continue_page`` would report a moment later.
    """
    if requested_sort is None and requested_sort_order is None:
        return
    meta = searcher.get_pool_metadata(parsed.token, user_id=user_id)
    if meta is None:
        raise SearchCursorExpired(f"cursor {parsed.token!r} not found")
    reject_pool_sort_mismatch(requested_sort=requested_sort,
                              requested_sort_order=requested_sort_order,
                              pool_sort=meta.sort,
                              pool_sort_order=meta.sort_order)


def _continue_or_grow(
    searcher: Searcher, parsed: SearchCursor, *, user_id: int, cfg: SearchConfig,
    snippet_chars: int | None,
) -> Any:
    try:
        return searcher.continue_page(parsed.token, parsed.page, user_id=user_id,
                                      snippet_chars=snippet_chars)
    except CacheMissError as exc:
        raise SearchCursorExpired(f"cursor {parsed.token!r} not found") from exc
    except PageOutOfPoolError:
        meta = searcher.get_pool_metadata(parsed.token, user_id=user_id)
        if meta is None:
            raise SearchCursorExpired(f"cursor {parsed.token!r} not found")
        if meta.candidates_per_arm >= cfg.candidates_per_arm_max:
            return _empty_grown_page(parsed.token, page_size=meta.page_size,
                                     sort_applied=meta.sort,
                                     rankable=meta.rankable)
        new_cpa = min(meta.candidates_per_arm * 2, cfg.candidates_per_arm_max)
        return searcher.grow_pool(parsed.token, new_cpa, user_id=user_id,
                                  snippet_chars=snippet_chars)


def _empty_grown_page(
    token: str, *, page_size: int, sort_applied: SortMode, rankable: bool,
) -> Any:
    """Synthetic 'pool exhausted at cap' page so callers see next_cursor=null.

    ``sort_applied`` comes from the exhausted pool's own metadata, not from
    a default: it must report the ordering that pool was built with (#345).
    ``rankable`` comes from there too, and *must* — this page's ``query`` is
    a synthetic ``parse_query("")``, so anything derived from it would
    report the pool unrankable when a pool is rankable by construction.
    Nothing could have served this page — it is reached only once
    ``continue_page`` has raised ``PageOutOfPoolError`` *and* the pool is at
    ``candidates_per_arm_max`` — but it stands in that pool's place, and the
    caller is still paging it.
    """
    return SearchPage(
        results=[], page=1, page_size=page_size, pool_size=0,
        candidates_per_arm=0, has_more_in_pool=False, can_grow_pool=False,
        search_token=token, query=parse_query(""), timing_ms={"total": 0.0},
        sort_applied=sort_applied, rankable=rankable,
    )


def _next_cursor(page: Any, *, cfg: SearchConfig) -> str | None:
    """Compute the cursor for the page after ``page``, or None if exhausted.

    Two cursor kinds:
      * keyset (date-ordered) — driven by ``page.next_keyset``; None
        means the keyset walk hit the end. The direction rides on
        ``next_keyset`` itself, stamped by the walk that produced the
        rows, so this layer cannot supply one the walk did not use.
      * pool (hybrid) — driven by ``search_token`` + page increment;
        None when both the cached pool and ``grow_pool`` are exhausted.
    """
    if page.next_keyset is not None:
        return encode_keyset_cursor(page.next_keyset)
    if page.search_token is None:
        return None
    if page.has_more_in_pool:
        return encode_search_cursor(SearchCursor(token=page.search_token,
                                                 page=page.page + 1))
    if page.can_grow_pool and page.candidates_per_arm < cfg.candidates_per_arm_max:
        return encode_search_cursor(SearchCursor(token=page.search_token,
                                                 page=page.page + 1))
    return None


def _scope_filters_by_acl(
    filters: dict[str, Any], allowed_account_ids: list[int],
) -> dict[str, Any] | None:
    """Return a new filters dict with ``account_ids`` intersected against the ACL.

    Returns ``None`` when the intersection is empty — the caller should
    short-circuit and skip running the underlying search.
    """
    if not allowed_account_ids:
        return None
    caller_ids = filters.get("account_ids")
    if caller_ids:
        caller_set = {parse_int_id(str(v), field="account_id") for v in caller_ids}
        intersection = sorted(caller_set & set(allowed_account_ids))
        if not intersection:
            return None
        return {**filters, "account_ids": [str(a) for a in intersection]}
    return {**filters, "account_ids": [str(a) for a in sorted(allowed_account_ids)]}


def _to_api_result(r: SearchResult) -> dict[str, Any]:
    """Map an internal SearchResult to the API JSON shape.

    The wire ``date`` field is ``COALESCE(internal_date, date_sent)``, the
    same expression every recent-mail / sort=date SQL path uses. Returning
    a different column than the sort key made dates look out of order in
    the GUI whenever the two diverged.

    A key added, removed or **reordered** here must be mirrored in
    ``api.search_projection.HIT_FIELDS``, which a caller's ``fields`` is
    validated against; pinned both ways (set and order) by
    ``test_hit_fields_are_exactly_the_hit_keys_plus_snippet``.
    """
    received = r.internal_date or r.date_sent
    return {
        "message_id": str(r.message_id),
        "account": {"id": str(r.account_id), "name": None},
        "folder": None,
        "subject": r.subject,
        "from": {"address": r.from_addr, "name": r.from_name},
        "to": [],
        "date": received.isoformat() if received else None,
        "snippet_html": r.snippet,
        "has_attachments": r.has_attachments,
        "score": r.score,
        "matched_arms": [r.matched_chunk_table],
    }
