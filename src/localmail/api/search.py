"""HTTP-friendly wrapper over localmail.search.Searcher.

Filter dicts from the HTTP layer get translated to the DSL query string the
existing Searcher already knows how to parse, plus pagination state is
flattened into a cursor string.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from localmail.api.errors import SearchCursorExpired, ValidationFailed
from localmail.api.ids import parse_int_id
from localmail.api.search_cursor import (
    SearchCursor,
    decode_keyset_cursor,
    decode_search_cursor,
    encode_keyset_cursor,
    encode_search_cursor,
    is_keyset_cursor,
)
from localmail.config import SearchConfig
from localmail.search.page_cache import CacheMissError, PageOutOfPoolError
from localmail.search.searcher import SearchPage, SearchResult, Searcher


_SUPPORTED_FILTER_KEYS = frozenset({
    "from", "to", "subject", "after", "before", "has_attachment",
    "account_ids", "folder_ids",
    "date_from", "date_to", "lang",
})

# Empty: every v1 spec filter key now wires through to the Searcher.
# Kept as a frozenset so the existing "unsupported key" check keeps working
# without special-casing an empty case at call sites.
_KNOWN_UNSUPPORTED_FILTER_KEYS: frozenset[str] = frozenset()


def build_query_string(*, free_text: str, filters: dict[str, Any]) -> str:
    """Compose `free_text` + filter DSL tokens into a single query string.

    Date filters are validated to YYYY-MM-DD. Keys in
    `_KNOWN_UNSUPPORTED_FILTER_KEYS` raise `ValidationFailed` so the caller
    sees a clear 400. Other unknown keys are silently ignored (forward
    compatibility with future filter additions).
    """
    for key in _KNOWN_UNSUPPORTED_FILTER_KEYS:
        if filters.get(key) not in (None, [], "", False):
            supported = ", ".join(sorted(_SUPPORTED_FILTER_KEYS))
            raise ValidationFailed(
                f"filter {key!r} is accepted by the API schema but not yet "
                f"wired through to the search backend. Supported filters: {supported}"
            )
    parts: list[str] = []
    if free_text:
        parts.append(free_text)
    for token in _filter_tokens(filters):
        parts.append(token)
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
        out.append(f'from:{_quote_value(v)}')
    if (v := filters.get("to")):
        out.append(f'to:{_quote_value(v)}')
    if (v := filters.get("subject")):
        out.append(f'subject:{_quote_value(v)}')
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
                out.append(f"lang:{s}")
    if filters.get("has_attachment") is True:
        out.append("has:attachment")
    return out


def _quote_value(v: Any) -> str:
    """Wrap a free-form filter value in double quotes so the DSL tokenizer
    treats it as a single token.

    Without this, a value like 'alice OR account:other' would tokenize into
    three tokens and inject an extra `account:` operator, bypassing the
    requested scope. Embedded quotes and newlines have no useful meaning for
    substring filters and are stripped — the DSL has no escape syntax.
    """
    s = str(v).replace('"', "").replace("\n", " ").replace("\r", " ")
    return f'"{s}"'


def _validate_date(value: str, key: str) -> None:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except (TypeError, ValueError) as exc:
        raise ValidationFailed(f"{key}: expected YYYY-MM-DD, got {value!r}") from exc


def run_search(
    *,
    searcher: Searcher,
    free_text: str,
    filters: dict[str, Any],
    limit: int,
    allowed_account_ids: list[int],
    user_id: int,
    sort: Literal["rank", "date"] = "rank",
    cursor: str | None = None,
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
    """
    scoped_filters = _scope_filters_by_acl(filters, allowed_account_ids)
    if scoped_filters is None:
        return {"results": [], "next_cursor": None, "total_estimate": 0, "took_ms": 0.0}

    cfg = searcher.config
    if cursor is None:
        query = build_query_string(free_text=free_text, filters=scoped_filters)
        page = searcher.search(query, page_size=limit, user_id=user_id, sort=sort)
    elif is_keyset_cursor(cursor):
        # Keyset cursor → lexical-date continuation. The cursor carries
        # only (ts, id); the query + filters come from the request body
        # (the GUI re-sends them on every loadMore). Searcher decides on
        # the lexical path because sort=date + non-empty free_text fires
        # the dispatch in Searcher.search.
        keyset = decode_keyset_cursor(cursor)
        query = build_query_string(free_text=free_text, filters=scoped_filters)
        page = searcher.search(query, page_size=limit, user_id=user_id,
                               sort=sort, keyset_cursor=keyset)
    else:
        parsed = decode_search_cursor(cursor)
        page = _continue_or_grow(searcher, parsed, user_id=user_id, cfg=cfg)

    next_cursor = _next_cursor(page, cfg=cfg)
    return {
        "results": [_to_api_result(r) for r in page.results],
        "next_cursor": next_cursor,
        "total_estimate": None,
        "took_ms": page.timing_ms.get("total", 0.0),
    }


def _continue_or_grow(
    searcher: Searcher, parsed: SearchCursor, *, user_id: int, cfg: SearchConfig,
) -> Any:
    try:
        return searcher.continue_page(parsed.token, parsed.page, user_id=user_id)
    except CacheMissError as exc:
        raise SearchCursorExpired(f"cursor {parsed.token!r} not found") from exc
    except PageOutOfPoolError:
        meta = searcher.get_pool_metadata(parsed.token, user_id=user_id)
        if meta is None:
            raise SearchCursorExpired(f"cursor {parsed.token!r} not found")
        if meta.candidates_per_arm >= cfg.candidates_per_arm_max:
            return _empty_grown_page(parsed.token, page_size=meta.page_size)
        new_cpa = min(meta.candidates_per_arm * 2, cfg.candidates_per_arm_max)
        return searcher.grow_pool(parsed.token, new_cpa, user_id=user_id)


def _empty_grown_page(token: str, *, page_size: int) -> Any:
    """Synthetic 'pool exhausted at cap' page so callers see next_cursor=null."""
    from localmail.search.query import parse_query
    return SearchPage(
        results=[], page=1, page_size=page_size, pool_size=0,
        candidates_per_arm=0, has_more_in_pool=False, can_grow_pool=False,
        search_token=token, query=parse_query(""), timing_ms={"total": 0.0},
    )


def _next_cursor(page: Any, *, cfg: SearchConfig) -> str | None:
    """Compute the cursor for the page after ``page``, or None if exhausted.

    Two cursor kinds:
      * keyset (lexical-date) — driven by ``page.next_keyset``; None
        means the keyset walk hit the end.
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
        "has_attachments": r.attachment_filename is not None,
        "score": r.score,
        "matched_arms": [r.matched_chunk_table],
    }
