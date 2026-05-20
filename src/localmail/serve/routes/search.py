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
    # "rank" (default) orders by rerank relevance; "date" keeps the same
    # candidate pool but orders the page by COALESCE(internal_date,
    # date_sent) DESC NULLS LAST. The empty-query branch is already
    # date-ordered so this is a no-op there.
    sort: Literal["rank", "date"] = "rank"
    cursor: str | None = None


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
        cursor=req.cursor,
    )
