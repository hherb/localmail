# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""POST /v1/search endpoint."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from localmail.api.acl import allowed_account_ids
from localmail.api.errors import FeatureUnavailable
from localmail.api.search import run_search
from localmail.serve.middleware import get_authenticated_user

router = APIRouter()


class SearchFiltersModel(BaseModel):
    account_ids: list[str] | None = None
    folder_ids: list[str] | None = None
    date_from: str | None = None
    date_to: str | None = None
    has_attachment: bool | None = None
    lang: str | None = None
    from_: str | None = Field(default=None, alias="from")
    to: str | None = None
    subject: str | None = None
    after: str | None = None
    before: str | None = None

    model_config = {"populate_by_name": True, "extra": "ignore"}


SEARCH_LIMIT_MAX = 200


class SearchRequest(BaseModel):
    query: str
    filters: SearchFiltersModel = Field(default_factory=SearchFiltersModel)
    limit: int = Field(default=50, ge=1, le=SEARCH_LIMIT_MAX)
    # "rank" orders by rerank relevance; "date" takes the date-ordered
    # keyset walk directly rather than the hybrid pool, ordering by
    # COALESCE(internal_date, date_sent) DESC NULLS LAST.
    #
    # Omitting it resolves to whichever of those will actually serve the
    # request: "rank" when the query has free text, "date" when it has none
    # (#324). A query with no free text — blank, or only filter operators,
    # since `parse_query` lifts those out — has nothing for the pool to
    # rank, so it takes the date walk and a *stated* "rank" for it is a 400
    # rather than a silent drop.
    #
    # Null rather than "rank" so that omitting it is distinguishable from
    # asking for it: alongside a `cursor` the cursor decides the ordering,
    # and a *stated* sort it cannot serve is a 400 rather than a silently
    # dropped cursor.
    sort: Literal["rank", "date"] | None = None
    # Direction for the sort criterion above. Orthogonal to `sort` so a
    # future criterion inherits it without doubling the `sort` enum.
    # "asc" is rejected for a *resolved* sort="rank": the rank path serves a
    # bounded candidate pool, so reversing it returns the least relevant of
    # the top hits rather than of the archive. It reads the resolved sort, so
    # "asc" alone on a query with no free text is honoured — that query
    # resolves to "date" (#324).
    #
    # Null rather than "desc" for the reason `sort` is null: alongside a
    # `cursor` the cursor decides the direction, and a model default would
    # be a statement the caller never made — contradicting every ascending
    # cursor.
    sort_order: Literal["asc", "desc"] | None = None
    cursor: str | None = None
    # Opt-in LLM query rewrite (Phase 4). Ignored gracefully when the server
    # has no rewriter configured — the response's rewrite_skipped reflects it.
    smart: bool = False


@router.post("")
def search_endpoint(
    req: SearchRequest,
    request: Request,
    user=Depends(get_authenticated_user),
) -> dict[str, Any]:
    searcher = request.app.state.searcher
    if searcher is None:
        raise FeatureUnavailable("search not configured on this server")
    pool = request.app.state.pool
    with pool.connection() as conn:
        allowed = allowed_account_ids(conn, user.id)
    filters_dict = req.filters.model_dump(by_alias=True, exclude_none=True)
    return run_search(
        searcher=searcher,
        free_text=req.query,
        filters=filters_dict,
        limit=req.limit,
        allowed_account_ids=allowed,
        user_id=user.id,
        sort=req.sort,
        sort_order=req.sort_order,
        cursor=req.cursor,
        smart=req.smart,
    )
