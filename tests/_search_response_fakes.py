"""A real, minimal /v1/search response for MCP fakes (#358).

The MCP SDK validates tool output against ``SearchResponse``, so a fake
``tool_search`` must return a real ``search_response(...)``, not a partial
dict. A function rather than a constant: each call gets its own ``results``
list, so one test cannot leak a mutation into another.
"""

from __future__ import annotations

from localmail.api.search_response import SearchResponse, search_response


def empty_search_response() -> SearchResponse:
    return search_response(
        results=[], next_cursor=None, took_ms=0.0, sort_applied="date",
        rankable=False, rewrite_status="not_requested", rewrite_note=None,
        rewrite_note_code=None)
