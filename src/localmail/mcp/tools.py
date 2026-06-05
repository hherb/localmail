"""MCP tool bodies. Each is a thin, ACL-scoped wrapper over a localmail.api
accessor. Transport-free and individually unit-testable against a real conn.
"""
from __future__ import annotations

from typing import Any, Literal

from localmail.api.search import run_search
from localmail.search.searcher import Searcher


def tool_search(
    *,
    searcher: Searcher,
    user_id: int,
    allowed_account_ids: list[int],
    query: str,
    sort: Literal["rank", "date"] = "rank",
    limit: int = 50,
    cursor: str | None = None,
    filters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Hybrid search, ACL-scoped. Page forward by passing back `next_cursor`."""
    return run_search(
        searcher=searcher,
        free_text=query,
        filters=filters or {},
        limit=limit,
        allowed_account_ids=allowed_account_ids,
        user_id=user_id,
        sort=sort,
        cursor=cursor,
    )
