"""HTTP-friendly wrapper over localmail.search.Searcher.

Filter dicts from the HTTP layer get translated to the DSL query string the
existing Searcher already knows how to parse, plus pagination state is
flattened into a cursor string.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from localmail.api.errors import ValidationFailed
from localmail.search.searcher import SearchPage, SearchResult, Searcher


_SUPPORTED_FILTER_KEYS = frozenset({
    "from", "to", "subject", "after", "before", "has_attachment",
    "account_ids", "folder_ids",
})

# Filter keys that appear in the v1 spec / SearchFiltersModel but are not yet
# wired through to the underlying Searcher. Surfaced as ValidationFailed so
# clients get a clear 400 instead of a silently-incorrect result set.
_KNOWN_UNSUPPORTED_FILTER_KEYS = frozenset({
    "date_from", "date_to", "lang",
})


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
    try:
        if (vs := filters.get("account_ids")):
            for v in vs:
                out.append(f"account_id:{int(v)}")
        if (vs := filters.get("folder_ids")):
            for vs_v in vs:
                out.append(f"folder_id:{int(vs_v)}")
    except (TypeError, ValueError) as exc:
        raise ValidationFailed(
            f"account_ids / folder_ids: each value must be an integer or "
            f"integer-string, got {filters.get('account_ids')!r} / "
            f"{filters.get('folder_ids')!r}"
        ) from exc
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
    cursor: str | None,
) -> dict[str, Any]:
    """Run a search and return the API-shaped response.

    `cursor` is the previous response's `next_cursor` (which is the SearchPage
    token). In v1 the cursor is informational only — the GUI does not paginate
    deep; expanded paging lands with a future grow_pool/continue_page wrapper.
    """
    query = build_query_string(free_text=free_text, filters=filters)
    page: SearchPage = searcher.search(query, page_size=limit)
    return {
        "results": [_to_api_result(r) for r in page.results],
        "next_cursor": page.search_token,
        "total_estimate": None,
        "took_ms": page.timing_ms.get("total", 0.0),
    }


def _to_api_result(r: SearchResult) -> dict[str, Any]:
    """Map an internal SearchResult to the API JSON shape."""
    return {
        "message_id": str(r.message_id),
        "account": {"id": str(r.account_id), "name": None},
        "folder": None,
        "subject": r.subject,
        "from": {"address": r.from_addr, "name": r.from_name},
        "to": [],
        "date": r.date_sent.isoformat() if r.date_sent else None,
        "snippet_html": r.snippet,
        "has_attachments": r.attachment_filename is not None,
        "score": r.score,
        "matched_arms": [r.matched_chunk_table],
    }
