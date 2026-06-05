"""MCP tool bodies. Each is a thin, ACL-scoped wrapper over a localmail.api
accessor. Transport-free and individually unit-testable against a real conn.
"""
from __future__ import annotations

from typing import Any, Literal

import psycopg

from localmail.api.accounts import list_accounts as api_list_accounts
from localmail.api.browse import list_messages as api_list_messages
from localmail.api.messages import get_message as api_get_message
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


def tool_get_message(
    conn: psycopg.Connection,
    *,
    message_id: int,
    allowed_account_ids: list[int],
    full_headers: bool = False,
) -> dict[str, Any]:
    """One message (headers, body, attachment list), ACL-scoped.

    Raises localmail.api.errors.NotFound when the message is absent OR the
    caller lacks a grant on its account (indistinguishable by design).
    """
    return api_get_message(
        conn, message_id,
        allowed_account_ids=allowed_account_ids,
        full_headers=full_headers,
    )


def tool_list_messages(
    conn: psycopg.Connection,
    *,
    allowed_account_ids: list[int],
    account_ids: list[int] | None = None,
    folder_ids: list[int] | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Keyset date-ordered browse page, ACL-scoped."""
    return api_list_messages(
        conn,
        allowed_account_ids=allowed_account_ids,
        account_ids=account_ids,
        folder_ids=folder_ids,
        limit=limit,
        cursor=cursor,
    )


def tool_list_accounts(
    conn: psycopg.Connection,
    *,
    allowed_account_ids: list[int],
) -> list[dict[str, Any]]:
    """The accounts this caller may read."""
    return api_list_accounts(conn, allowed_account_ids=allowed_account_ids)
