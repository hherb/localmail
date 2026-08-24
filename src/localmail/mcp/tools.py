# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""MCP tool bodies. Each is a thin, ACL-scoped wrapper over a localmail.api
accessor. Transport-free and individually unit-testable against a real conn.
"""
from __future__ import annotations

from typing import Any, Literal

import psycopg

from localmail.api.accounts import list_accounts as api_list_accounts
from localmail.api.attachments import (
    get_attachment_metadata,
    get_attachment_text,
)
from localmail.api.browse import list_messages as api_list_messages
from localmail.api.messages import get_message as api_get_message
from localmail.api.search import run_search
from localmail.search.searcher import Searcher

MODE_TEXT = "text"
MODE_METADATA = "metadata"


def tool_search(
    *,
    searcher: Searcher,
    user_id: int,
    allowed_account_ids: list[int],
    query: str,
    sort: Literal["rank", "date"] | None = None,
    sort_order: Literal["asc", "desc"] | None = None,
    limit: int = 50,
    cursor: str | None = None,
    filters: dict[str, Any] | None = None,
    smart: bool = False,
) -> dict[str, Any]:
    """Hybrid search, ACL-scoped. Page forward by passing back `next_cursor`.

    `sort` defaults to `rank` and `sort_order` to `desc`; both are best left
    unset when paging, since the cursor already carries the ordering — on
    both axes — it continues. Send back the same `query` and `filters` with
    it — a `sort=date` cursor walks the archive by date (lexically when the
    query has free text, over every filtered row when it does not) and
    rebuilds that walk from the query. A stated `sort` or `sort_order` the
    cursor cannot serve is a validation error, not a silently restarted
    search: leaving
    `sort_order` unset is exactly what makes an ascending cursor keep
    walking ascending instead of silently reverting to `desc`.
    `sort_order="asc"` only applies to `sort="date"`; pairing it with
    `sort="rank"` (stated or defaulted) is refused, since the rank path
    searches a bounded candidate pool and reversing it would surface the
    least relevant of the top hits rather than of the archive.

    `smart` opts into an LLM query rewrite (page 1 only). The response carries
    `rewrite_status` (one of `applied`, `unavailable`, `failed`,
    `not_attempted`, `not_requested`), an optional curated human `rewrite_note`,
    and a machine-readable `rewrite_note_code` (`missing_model`, `unreachable`,
    `unparseable`, `not_configured`, `continuation_page`, or null);
    `rewrite_skipped` (kept for back-compat) is True only for `unavailable`
    and `failed`. On a continuation page, `smart` is ignored and the status is
    `not_attempted`.
    """
    return run_search(
        searcher=searcher,
        free_text=query,
        filters=filters or {},
        limit=limit,
        allowed_account_ids=allowed_account_ids,
        user_id=user_id,
        sort=sort,
        sort_order=sort_order,
        cursor=cursor,
        smart=smart,
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


def tool_get_attachment(
    conn: psycopg.Connection,
    *,
    sha256: str,
    allowed_account_ids: list[int],
    mode: str = MODE_TEXT,
) -> dict[str, Any]:
    """Extracted attachment text or metadata, ACL-scoped. Never raw bytes.

    `mode="text"` returns extracted text (NotFound if not yet extracted);
    `mode="metadata"` returns blob metadata. Any other mode is a ValueError
    (raw bytes are intentionally HTTP-only, via /v1/attachments).
    """
    if mode == MODE_TEXT:
        text = get_attachment_text(
            conn, sha256, allowed_account_ids=allowed_account_ids)
        return {"mode": MODE_TEXT, "sha256": sha256, "text": text}
    if mode == MODE_METADATA:
        meta = get_attachment_metadata(
            conn, sha256, allowed_account_ids=allowed_account_ids)
        return {"mode": MODE_METADATA, "sha256": sha256, "metadata": meta}
    raise ValueError(
        f"unsupported attachment mode {mode!r}; "
        f"expected {MODE_TEXT!r} or {MODE_METADATA!r}")
